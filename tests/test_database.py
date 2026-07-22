"""Tests for src/db/database.py — async SQLite persistence layer."""

from __future__ import annotations

import pytest

from src.db.database import DatabaseManager


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def db() -> DatabaseManager:
    """In-memory DatabaseManager with schema applied."""
    manager = DatabaseManager(":memory:")
    await manager.migrate()
    yield manager
    await manager.close()


# ── Migration ─────────────────────────────────────────────────────────────────


async def test_migrate_creates_tables(db: DatabaseManager) -> None:
    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        tables = {row[0] for row in await cur.fetchall()}
    # sqlite_sequence is auto-created by SQLite for AUTOINCREMENT tables — filter it out.
    user_tables = {t for t in tables if not t.startswith("sqlite_")}
    assert user_tables == {
        "processed_tasks",
        "reaction_reminders",
        "schema_migrations",
        "slack_messages",
    }


async def test_migrate_idempotent() -> None:
    """Running migrate() twice must not raise or duplicate migration records."""
    db = DatabaseManager(":memory:")
    await db.migrate()
    await db.migrate()   # second call — should be a no-op
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
        count = (await cur.fetchone())[0]
    assert count == 3   # migrations v1 + v2 + v3 each recorded exactly once
    await db.close()


async def test_migrate_records_version(db: DatabaseManager) -> None:
    conn = db._conn_or_raise()
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        versions = [row[0] for row in await cur.fetchall()]
    assert versions == [1, 2, 3]


# ── processed_tasks — basic deduplication ─────────────────────────────────────


async def test_is_task_processed_returns_false_for_new(db: DatabaseManager) -> None:
    result = await db.is_task_processed("C001", "12345.678", "eyes")
    assert result is False


async def test_save_and_check_processed_task(db: DatabaseManager) -> None:
    await db.save_processed_task(
        channel="C001",
        ts="12345.678",
        emoji="eyes",
        reactor_slack_id="U001",
        reactor_slack_name="Alice",
        slack_message_url="https://slack.com/archives/C001/p12345678",
        notion_page_id="page-uuid-1",
        notion_page_url="https://notion.so/page-uuid-1",
    )
    assert await db.is_task_processed("C001", "12345.678", "eyes") is True


async def test_different_emoji_same_message_not_duplicate(db: DatabaseManager) -> None:
    await db.save_processed_task(channel="C001", ts="12345.678", emoji="eyes")
    # Same message, different emoji — should NOT be flagged as already processed.
    assert await db.is_task_processed("C001", "12345.678", "fire") is False


async def test_save_processed_task_duplicate_is_silent(db: DatabaseManager) -> None:
    """INSERT OR IGNORE on a duplicate must not raise."""
    kwargs = dict(channel="C001", ts="12345.678", emoji="eyes")
    await db.save_processed_task(**kwargs)
    await db.save_processed_task(**kwargs)   # second insert — should be silently ignored


async def test_save_processed_task_stores_reactor(db: DatabaseManager) -> None:
    await db.save_processed_task(
        channel="C002",
        ts="99999.000",
        emoji="thumbsup",
        reactor_slack_id="U007",
        reactor_slack_name="Bond",
    )
    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT reactor_slack_id, reactor_slack_name "
        "FROM processed_tasks WHERE slack_channel='C002'"
    ) as cur:
        row = await cur.fetchone()
    assert row["reactor_slack_id"] == "U007"
    assert row["reactor_slack_name"] == "Bond"


# ── slack_messages — new messages ─────────────────────────────────────────────

_NEW_EVENT = {
    "type": "message",
    "channel": "C100",
    "ts": "11111.000",
    "user": "U100",
    "text": "Hello world",
    "channel_type": "channel",
}


async def test_save_new_message(db: DatabaseManager) -> None:
    await db.save_message(
        _NEW_EVENT,
        channel_name="general",
        user_name="Alice",
        user_email="alice@example.com",
    )
    conn = db._conn_or_raise()
    async with conn.execute("SELECT * FROM slack_messages WHERE slack_channel='C100'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["slack_ts"] == "11111.000"
    assert row["slack_user_id"] == "U100"
    assert row["slack_user_name"] == "Alice"
    assert row["slack_user_email"] == "alice@example.com"
    assert row["slack_channel_name"] == "general"
    assert row["message_text"] == "Hello world"
    assert row["is_deleted"] == 0
    assert row["edit_count"] == 0
    assert row["is_thread_reply"] == 0


async def test_save_message_duplicate_is_silent(db: DatabaseManager) -> None:
    """INSERT OR IGNORE must silently discard a duplicate (same channel + ts)."""
    await db.save_message(_NEW_EVENT)
    await db.save_message(_NEW_EVENT)  # second insert — should be silently ignored
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM slack_messages") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1


async def test_save_thread_reply_sets_flag(db: DatabaseManager) -> None:
    event = {
        "type": "message",
        "channel": "C100",
        "ts": "22222.000",
        "thread_ts": "11111.000",   # different from ts → it's a reply
        "user": "U101",
        "text": "Thread reply",
    }
    await db.save_message(event)
    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT is_thread_reply FROM slack_messages WHERE slack_ts='22222.000'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_thread_reply"] == 1


async def test_save_message_with_files_sets_flag(db: DatabaseManager) -> None:
    event = {**_NEW_EVENT, "ts": "33333.000", "files": [{"id": "F001"}]}
    await db.save_message(event)
    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT has_files FROM slack_messages WHERE slack_ts='33333.000'"
    ) as cur:
        row = await cur.fetchone()
    assert row["has_files"] == 1


async def test_bot_message_skipped(db: DatabaseManager) -> None:
    event = {**_NEW_EVENT, "ts": "44444.000", "bot_id": "B001"}
    await db.save_message(event)
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM slack_messages") as cur:
        count = (await cur.fetchone())[0]
    assert count == 0


async def test_bot_message_subtype_skipped(db: DatabaseManager) -> None:
    event = {**_NEW_EVENT, "ts": "55555.000", "subtype": "bot_message"}
    await db.save_message(event)
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM slack_messages") as cur:
        count = (await cur.fetchone())[0]
    assert count == 0


# ── slack_messages — edits ────────────────────────────────────────────────────


async def test_message_changed_updates_text_and_count(db: DatabaseManager) -> None:
    # First save the original message.
    await db.save_message(_NEW_EVENT)

    # Then send a message_changed event.
    edit_event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C100",
        "ts": "11111.001",   # event ts (different from message ts)
        "message": {
            "type": "message",
            "ts": "11111.000",   # original message ts
            "text": "Hello world (edited)",
        },
    }
    await db.save_message(edit_event)

    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT message_text, edit_count, edited_at "
        "FROM slack_messages WHERE slack_ts='11111.000'"
    ) as cur:
        row = await cur.fetchone()

    assert row["message_text"] == "Hello world (edited)"
    assert row["edit_count"] == 1
    assert row["edited_at"] is not None


async def test_message_changed_increments_edit_count_each_time(db: DatabaseManager) -> None:
    await db.save_message(_NEW_EVENT)

    for _ in range(3):
        edit_event = {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C100",
            "ts": "11111.001",
            "message": {"ts": "11111.000", "text": "edited again"},
        }
        await db.save_message(edit_event)

    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT edit_count FROM slack_messages WHERE slack_ts='11111.000'"
    ) as cur:
        row = await cur.fetchone()
    assert row["edit_count"] == 3


# ── slack_messages — deletes ──────────────────────────────────────────────────


async def test_message_deleted_sets_flag(db: DatabaseManager) -> None:
    await db.save_message(_NEW_EVENT)

    delete_event = {
        "type": "message",
        "subtype": "message_deleted",
        "channel": "C100",
        "ts": "11111.002",      # event ts
        "deleted_ts": "11111.000",  # original message ts
    }
    await db.save_message(delete_event)

    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT is_deleted FROM slack_messages WHERE slack_ts='11111.000'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_deleted"] == 1


async def test_message_deleted_falls_back_to_ts_when_no_deleted_ts(db: DatabaseManager) -> None:
    """Some Slack clients omit deleted_ts; fall back to ts in that case."""
    await db.save_message(_NEW_EVENT)

    delete_event = {
        "type": "message",
        "subtype": "message_deleted",
        "channel": "C100",
        "ts": "11111.000",   # only ts present, no deleted_ts
    }
    await db.save_message(delete_event)

    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT is_deleted FROM slack_messages WHERE slack_ts='11111.000'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_deleted"] == 1
