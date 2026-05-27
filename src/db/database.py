"""Async SQLite database manager for SlackToNotion.

Tables
------
schema_migrations  — tracks which migration versions have been applied.
processed_tasks    — deduplication: one row per (channel, ts, emoji) triple;
                     prevents creating duplicate Notion tasks when the same emoji
                     is removed and re-added to a message.
slack_messages     — full log of every Slack message/thread post the bot sees,
                     with edit and soft-delete tracking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

# ── Migration SQL ─────────────────────────────────────────────────────────────
# The schema_migrations table itself is bootstrapped by DatabaseManager.migrate()
# before any migration runs, so it must NOT be created here.
# Add new entries by appending — never modify existing ones.

_V1 = """\
CREATE TABLE IF NOT EXISTS processed_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    slack_channel      TEXT    NOT NULL,
    slack_ts           TEXT    NOT NULL,
    slack_emoji        TEXT    NOT NULL,
    reactor_slack_id   TEXT,
    reactor_slack_name TEXT,
    slack_message_url  TEXT,
    notion_page_id     TEXT,
    notion_page_url    TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(slack_channel, slack_ts, slack_emoji)
);

CREATE TABLE IF NOT EXISTS slack_messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type         TEXT,
    event_subtype      TEXT,
    slack_channel      TEXT    NOT NULL,
    slack_channel_name TEXT,
    slack_channel_type TEXT,
    slack_ts           TEXT    NOT NULL,
    slack_thread_ts    TEXT,
    slack_user_id      TEXT,
    slack_user_name    TEXT,
    slack_user_email   TEXT,
    slack_bot_id       TEXT,
    message_text       TEXT,
    is_thread_reply    INTEGER DEFAULT 0,
    has_files          INTEGER DEFAULT 0,
    is_deleted         INTEGER DEFAULT 0,
    edit_count         INTEGER DEFAULT 0,
    edited_at          TIMESTAMP,
    raw_event          TEXT,
    received_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(slack_channel, slack_ts)
);
"""

# List of (version, sql) pairs — applied in ascending version order.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
]


class DatabaseManager:
    """Async SQLite database manager.

    Usage::

        db = DatabaseManager("slack_to_notion.db")
        await db.migrate()   # call once at startup
        ...
        await db.close()
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def migrate(self) -> None:
        """Open the DB connection, bootstrap the migrations table, and apply
        any pending migrations in version order."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        # Bootstrap: create schema_migrations if it doesn't exist yet.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self._conn.commit()

        # Determine the highest applied version.
        async with self._conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ) as cur:
            row = await cur.fetchone()
            current_version: int = row[0] or 0

        pending = [(v, sql) for v, sql in sorted(MIGRATIONS) if v > current_version]
        if not pending:
            logger.info("DB schema is up to date (version %d).", current_version)
            return

        for version, sql in pending:
            logger.info("Applying DB migration v%d …", version)
            # executescript() commits any open transaction automatically.
            await self._conn.executescript(sql)
            await self._conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            await self._conn.commit()
            logger.info("DB migration v%d applied.", version)

        logger.info(
            "DB migrations complete — schema now at version %d.", pending[-1][0]
        )

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn_or_raise(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "DatabaseManager.migrate() must be called before any DB operations."
            )
        return self._conn

    # ── processed_tasks ───────────────────────────────────────────────────────

    async def is_task_processed(self, channel: str, ts: str, emoji: str) -> bool:
        """Return True if this (channel, ts, emoji) triple was already processed."""
        conn = self._conn_or_raise()
        async with conn.execute(
            "SELECT 1 FROM processed_tasks "
            "WHERE slack_channel=? AND slack_ts=? AND slack_emoji=?",
            (channel, ts, emoji),
        ) as cur:
            return await cur.fetchone() is not None

    async def save_processed_task(
        self,
        *,
        channel: str,
        ts: str,
        emoji: str,
        reactor_slack_id: str | None = None,
        reactor_slack_name: str | None = None,
        slack_message_url: str | None = None,
        notion_page_id: str | None = None,
        notion_page_url: str | None = None,
    ) -> None:
        """Insert a processed-task record. Silently skips on a UNIQUE conflict."""
        conn = self._conn_or_raise()
        await conn.execute(
            """
            INSERT OR IGNORE INTO processed_tasks
                (slack_channel, slack_ts, slack_emoji,
                 reactor_slack_id, reactor_slack_name,
                 slack_message_url, notion_page_id, notion_page_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel, ts, emoji,
                reactor_slack_id, reactor_slack_name,
                slack_message_url, notion_page_id, notion_page_url,
            ),
        )
        await conn.commit()

    # ── slack_messages ────────────────────────────────────────────────────────

    async def save_message(
        self,
        event: dict,
        *,
        channel_name: str | None = None,
        user_name: str | None = None,
        user_email: str | None = None,
    ) -> None:
        """Persist a Slack message event, routing on ``event["subtype"]``.

        Routing:
        - ``None`` / ``"file_share"``   → INSERT OR IGNORE (new message)
        - ``"message_changed"``          → UPDATE text, bump edit_count, set edited_at
        - ``"message_deleted"``          → soft-delete (is_deleted = 1)
        - ``"bot_message"`` / has bot_id → skip
        """
        subtype: str | None = event.get("subtype")
        bot_id: str | None = event.get("bot_id")

        # Skip bot-originated messages.
        if subtype == "bot_message" or bot_id:
            return

        conn = self._conn_or_raise()
        channel: str = event.get("channel", "")

        if subtype == "message_deleted":
            deleted_ts: str = event.get("deleted_ts") or event.get("ts", "")
            await conn.execute(
                "UPDATE slack_messages SET is_deleted=1 "
                "WHERE slack_channel=? AND slack_ts=?",
                (channel, deleted_ts),
            )
            await conn.commit()
            return

        if subtype == "message_changed":
            msg: dict = event.get("message", {})
            new_text: str = msg.get("text", "")
            original_ts: str = msg.get("ts", event.get("ts", ""))
            now = datetime.now(timezone.utc).isoformat()
            cursor = await conn.execute(
                """
                UPDATE slack_messages
                SET message_text=?, edit_count=edit_count+1, edited_at=?
                WHERE slack_channel=? AND slack_ts=?
                """,
                (new_text, now, channel, original_ts),
            )
            if cursor.rowcount == 0:
                # The original message was never saved (e.g. sent before the bot
                # connected). Insert a best-effort row so the record isn't lost.
                logger.info(
                    "message_changed: original ts=%s not in DB — inserting fallback row.",
                    original_ts,
                )
                msg_user: str | None = msg.get("user")
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO slack_messages
                        (event_type, event_subtype,
                         slack_channel, slack_ts, slack_thread_ts,
                         slack_user_id, message_text,
                         is_thread_reply, raw_event)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "message", "message_changed",
                        channel, original_ts, msg.get("thread_ts"),
                        msg_user, new_text,
                        1 if (msg.get("thread_ts") and msg.get("thread_ts") != original_ts) else 0,
                        json.dumps(event),
                    ),
                )
            await conn.commit()
            return

        # New message (subtype None or "file_share").
        ts: str = event.get("ts", "")
        thread_ts: str | None = event.get("thread_ts")
        is_thread_reply: int = 1 if (thread_ts and thread_ts != ts) else 0

        await conn.execute(
            """
            INSERT OR IGNORE INTO slack_messages
                (event_type, event_subtype,
                 slack_channel, slack_channel_name, slack_channel_type,
                 slack_ts, slack_thread_ts,
                 slack_user_id, slack_user_name, slack_user_email,
                 slack_bot_id, message_text,
                 is_thread_reply, has_files, raw_event)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("type", "message"),
                subtype,
                channel,
                channel_name,
                event.get("channel_type"),
                ts,
                thread_ts,
                event.get("user"),
                user_name,
                user_email,
                bot_id,
                event.get("text", ""),
                is_thread_reply,
                1 if event.get("files") else 0,
                json.dumps(event),
            ),
        )
        await conn.commit()
