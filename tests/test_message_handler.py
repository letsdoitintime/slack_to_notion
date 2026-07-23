"""Tests for the `message` listener in src/slack/event_handler.py.

The listener had no coverage at all until 2026-07-22, which is how it kept a
`return` that dropped every bot-authored message on the floor while the schema
had a `slack_bot_id` column waiting for them.
"""

from __future__ import annotations

import pytest

from src.db.database import DatabaseManager
from src.slack.event_handler import register_handlers

# The listener-capturing stand-in for Bolt already exists — reuse it.
from .test_reminders import _FakeApp


@pytest.fixture
async def db() -> DatabaseManager:
    manager = DatabaseManager(":memory:")
    await manager.migrate()
    yield manager
    await manager.close()


class _FakeSlackClient:
    """Sync stand-in — the handler calls these through asyncio.to_thread."""

    def __init__(self) -> None:
        self.user_lookups: list[str] = []

    def get_channel_name(self, channel: str) -> str:
        return "general"

    def get_user_info(self, user_id: str) -> dict:
        self.user_lookups.append(user_id)
        return {"id": user_id, "name": "Alice", "email": "alice@example.com"}


async def _handle(db: DatabaseManager, event: dict, slack=None) -> None:
    app = _FakeApp()
    register_handlers(app, {"emoji_mappings": []}, processors={},
                      slack_client=slack, db=db)
    await app.handlers["message"][0](body={"event": event}, event=event)


async def _row(db: DatabaseManager, ts: str):
    conn = db._conn_or_raise()
    async with conn.execute(
        "SELECT * FROM slack_messages WHERE slack_ts=?", (ts,)
    ) as cur:
        return await cur.fetchone()


# ── bot messages reach the DB ────────────────────────────────────────────────


async def test_bot_message_is_saved(db: DatabaseManager) -> None:
    """The bug as reported: a message from another bot never reached the DB."""
    await _handle(db, {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C1",
        "ts": "100.000",
        "bot_id": "B_OTHER",
        "text": "Nightly sync finished",
    })
    row = await _row(db, "100.000")
    assert row is not None, "bot message was dropped before reaching save_message"
    assert row["slack_bot_id"] == "B_OTHER"
    assert row["message_text"] == "Nightly sync finished"


async def test_app_post_with_both_user_and_bot_id_is_saved(db: DatabaseManager) -> None:
    """An app posting with a bot token sends `user` AND `bot_id`, no subtype."""
    slack = _FakeSlackClient()
    await _handle(db, {
        "type": "message",
        "channel": "C1",
        "ts": "101.000",
        "user": "U_APP",
        "bot_id": "B_APP",
        "text": "Deploy started",
    }, slack=slack)
    row = await _row(db, "101.000")
    assert row is not None
    assert row["slack_bot_id"] == "B_APP"
    assert row["slack_user_id"] == "U_APP"
    assert slack.user_lookups == ["U_APP"]   # took the normal users.info path


async def test_own_bot_messages_are_saved_too(db: DatabaseManager) -> None:
    """Deliberate: this bot's own posts are archived, not filtered out.

    They are a normal part of the channel record and saving one posts nothing,
    so there is no echo loop. Filter on a specific bot_id here if that ever
    becomes noise.
    """
    await _handle(db, {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C1",
        "ts": "102.000",
        "bot_id": "B_SELF",
        "text": "Reminder: <@U1> haven't reacted yet.",
    })
    assert await _row(db, "102.000") is not None


# ── naming a bot without a users.info call ───────────────────────────────────


async def test_bot_name_comes_from_bot_profile(db: DatabaseManager) -> None:
    slack = _FakeSlackClient()
    await _handle(db, {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C1",
        "ts": "103.000",
        "bot_id": "B_OTHER",
        "bot_profile": {"id": "B_OTHER", "name": "Zapier"},
        "username": "ignored-when-profile-present",
        "text": "hi",
    }, slack=slack)
    row = await _row(db, "103.000")
    assert row["slack_user_name"] == "Zapier"
    assert slack.user_lookups == []    # no users.info call — there is no user id


async def test_bot_name_falls_back_to_username(db: DatabaseManager) -> None:
    await _handle(db, {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C1",
        "ts": "104.000",
        "bot_id": "B_OTHER",
        "username": "Jenkins",
        "text": "hi",
    }, slack=_FakeSlackClient())
    row = await _row(db, "104.000")
    assert row["slack_user_name"] == "Jenkins"


async def test_nameless_bot_still_saves(db: DatabaseManager) -> None:
    """No bot_profile, no username — the row is worth keeping regardless."""
    await _handle(db, {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C1",
        "ts": "105.000",
        "bot_id": "B_OTHER",
        "text": "hi",
    }, slack=_FakeSlackClient())
    row = await _row(db, "105.000")
    assert row is not None
    assert row["slack_user_name"] is None


# ── regression guards: human messages unchanged ──────────────────────────────


async def test_human_message_still_resolves_via_users_info(db: DatabaseManager) -> None:
    slack = _FakeSlackClient()
    await _handle(db, {
        "type": "message",
        "channel": "C1",
        "ts": "200.000",
        "user": "U_HUMAN",
        "text": "morning",
    }, slack=slack)
    row = await _row(db, "200.000")
    assert row["slack_user_id"] == "U_HUMAN"
    assert row["slack_user_name"] == "Alice"
    assert row["slack_user_email"] == "alice@example.com"
    assert row["slack_channel_name"] == "general"
    assert row["slack_bot_id"] is None
    assert slack.user_lookups == ["U_HUMAN"]


async def test_no_db_configured_is_a_no_op(db: DatabaseManager) -> None:
    """db=None must not raise now that the bot-message branch runs further."""
    app = _FakeApp()
    register_handlers(app, {"emoji_mappings": []}, processors={},
                      slack_client=None, db=None)
    event = {"type": "message", "subtype": "bot_message",
             "channel": "C1", "ts": "300.000", "bot_id": "B1", "text": "hi"}
    await app.handlers["message"][0](body={"event": event}, event=event)
