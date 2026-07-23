"""Async SQLite database manager for SlackToNotion.

Tables
------
schema_migrations  — tracks which migration versions have been applied.
processed_tasks    — deduplication: one row per (channel, ts, emoji) triple;
                     prevents creating duplicate Notion tasks when the same emoji
                     is removed and re-added to a message.
slack_messages     — full log of every Slack message/thread post the bot sees,
                     with edit and soft-delete tracking.
reaction_reminders — one row per scheduled reminder; a background loop fires the
                     due ones, re-computing non-reactors at send time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
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

_V2 = """\
CREATE TABLE IF NOT EXISTS reaction_reminders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    slack_channel    TEXT    NOT NULL,
    slack_ts         TEXT    NOT NULL,
    trigger_emoji    TEXT    NOT NULL,
    after_minutes    REAL    NOT NULL,
    message_template TEXT,
    remind_at        TIMESTAMP NOT NULL,
    sent_at          TIMESTAMP,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(slack_channel, slack_ts, trigger_emoji, after_minutes)
);
"""

# Adds a retry counter to reaction_reminders so a failed Slack post is retried
# on later poll cycles and eventually given up on (bounded retry, no storm).
_V3 = """\
ALTER TABLE reaction_reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
"""

# List of (version, sql) pairs — applied in ascending version order.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
    (2, _V2),
    (3, _V3),
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
            # Each statement plus its schema_migrations row goes in ONE transaction.
            # executescript() cannot be used here: it COMMITs before running, so a
            # crash between the schema change and the version row would leave the
            # migration applied but unrecorded — and the next startup would replay
            # it. That is survivable for `CREATE TABLE IF NOT EXISTS`, but v3 is an
            # `ALTER TABLE ADD COLUMN`, which SQLite rejects as a duplicate column
            # on the second run and would stop the bot from starting at all.
            # ponytail: naive `;` split — fine for these migrations, none of which
            # contain a semicolon inside a string or trigger body. If one ever does,
            # switch to a real statement splitter.
            for statement in filter(None, (s.strip() for s in sql.split(";"))):
                try:
                    await self._conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    # Belt and braces for the same failure the transaction above
                    # prevents: if a DB somehow already has the column (applied by
                    # an older build, or restored from a backup taken mid-upgrade),
                    # replaying ADD COLUMN would otherwise stop the bot from
                    # starting at all. Already-present is the desired end state.
                    if "duplicate column name" not in str(exc).lower():
                        raise
                    logger.info(
                        "Migration v%d: %s — already applied, continuing.",
                        version,
                        exc,
                    )
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
        - ``None`` / ``"file_share"`` / ``"bot_message"`` → INSERT OR IGNORE (new message)
        - ``"message_changed"``          → UPDATE text, bump edit_count, set edited_at
        - ``"message_deleted"``          → soft-delete (is_deleted = 1)

        Bot-originated messages are stored like any other, with ``bot_id`` in
        ``slack_bot_id`` and (usually) no ``slack_user_id``. They were dropped
        here until 2026-07-22; the caller in ``event_handler`` had its own,
        separate skip for the same thing, so both had to go.
        """
        subtype: str | None = event.get("subtype")
        bot_id: str | None = event.get("bot_id")

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
                # A bot's post carries `bot_id` on the NESTED message, never on
                # the wrapper — read it from there or the fallback row ends up
                # with no author at all, which is what the pre-2026-07-22 rows
                # in this table look like.
                msg_bot_id: str | None = msg.get("bot_id")
                # The name is inline on the same nested message. Without it the
                # row is not merely nameless, it is unrepairable: the repair
                # script matches rows whose slack_bot_id is NULL, which this one
                # no longer is, so nothing would ever come back for it.
                msg_bot_name: str | None = (
                    (msg.get("bot_profile") or {}).get("name") or msg.get("username")
                )
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO slack_messages
                        (event_type, event_subtype,
                         slack_channel, slack_ts, slack_thread_ts,
                         slack_user_id, slack_user_name, slack_bot_id, message_text,
                         is_thread_reply, raw_event)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "message", "message_changed",
                        channel, original_ts, msg.get("thread_ts"),
                        msg_user, msg_bot_name, msg_bot_id, new_text,
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

    # ── reaction_reminders ────────────────────────────────────────────────────

    async def schedule_reminders(
        self,
        *,
        channel: str,
        ts: str,
        trigger_emoji: str,
        message_template: str | None,
        rows: list[tuple[float, str]],
    ) -> None:
        """Insert one reminder row per (after_minutes, remind_at_iso) in *rows*.

        Idempotent per (channel, ts, emoji, after_minutes): re-adding the same
        trigger emoji to a message does not duplicate its reminders.
        """
        conn = self._conn_or_raise()
        await conn.executemany(
            """
            INSERT OR IGNORE INTO reaction_reminders
                (slack_channel, slack_ts, trigger_emoji,
                 after_minutes, message_template, remind_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (channel, ts, trigger_emoji, minutes, message_template, remind_at)
                for minutes, remind_at in rows
            ],
        )
        await conn.commit()

    async def due_reminders(self, now_iso: str) -> list[aiosqlite.Row]:
        """Return unsent reminders whose remind_at has passed, oldest first."""
        conn = self._conn_or_raise()
        async with conn.execute(
            "SELECT * FROM reaction_reminders "
            "WHERE sent_at IS NULL AND remind_at <= ? ORDER BY remind_at",
            (now_iso,),
        ) as cur:
            return await cur.fetchall()

    async def mark_reminder_sent(self, reminder_id: int) -> None:
        """Stamp a reminder as sent so it is not fired again."""
        conn = self._conn_or_raise()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "UPDATE reaction_reminders SET sent_at=? WHERE id=?", (now, reminder_id)
        )
        await conn.commit()

    async def bump_reminder_attempt(self, reminder_id: int) -> int:
        """Increment a reminder's failed-attempt counter and return the new count.

        A reminder stays unsent (so ``due_reminders`` re-selects it) until it
        either succeeds or the caller gives up after too many attempts.
        """
        conn = self._conn_or_raise()
        await conn.execute(
            "UPDATE reaction_reminders SET attempts = attempts + 1 WHERE id=?",
            (reminder_id,),
        )
        await conn.commit()
        async with conn.execute(
            "SELECT attempts FROM reaction_reminders WHERE id=?", (reminder_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
