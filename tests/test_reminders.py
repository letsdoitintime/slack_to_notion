"""Tests for src/slack/reminders.py and its DB + config plumbing."""

from __future__ import annotations

import pytest

from src.db.database import DatabaseManager
from src.slack.reminders import (
    DEFAULT_REMINDER_TEMPLATE,
    _MAX_ATTEMPTS,
    _fire_due,
    _match_rule,
    _send_one,
    compute_non_reactors,
    schedule_for_event,
)
from src.utils.config_loader import _validate_reaction_reminders


# ── compute_non_reactors (the core diff) ──────────────────────────────────────


def test_non_reactors_excludes_reactors_poster_and_bot() -> None:
    members = ["U_A", "U_B", "U_C", "U_POSTER", "U_BOT"]
    reactors = {"U_B"}
    result = compute_non_reactors(members, reactors, ["U_POSTER", "U_BOT"])
    assert result == ["U_A", "U_C"]  # order preserved, reactor/poster/bot dropped


def test_non_reactors_empty_when_everyone_reacted() -> None:
    members = ["U_A", "U_B"]
    assert compute_non_reactors(members, {"U_A", "U_B"}, []) == []


def test_non_reactors_dedupes_and_ignores_blank_excludes() -> None:
    members = ["U_A", "U_A", "U_B"]
    assert compute_non_reactors(members, set(), ["", "U_B"]) == ["U_A"]


# ── _match_rule ───────────────────────────────────────────────────────────────


def _config() -> dict:
    return {
        "reaction_reminders": [
            {
                "channels": ["C1"],
                "trigger_emoji": "hmm_parrot",
                "reminders": [{"after_minutes": 60}],
            }
        ]
    }


def test_match_rule_hits_on_channel_and_emoji() -> None:
    rule = _match_rule(_config(), "C1", "hmm_parrot")
    assert rule is not None and rule["trigger_emoji"] == "hmm_parrot"


def test_match_rule_misses_wrong_channel_or_emoji() -> None:
    assert _match_rule(_config(), "C2", "hmm_parrot") is None
    assert _match_rule(_config(), "C1", "thumbsup") is None


# ── DB round-trip: schedule → due → mark sent ─────────────────────────────────


@pytest.fixture
async def db() -> DatabaseManager:
    manager = DatabaseManager(":memory:")
    await manager.migrate()
    yield manager
    await manager.close()


async def test_schedule_and_fetch_due(db: DatabaseManager) -> None:
    await db.schedule_reminders(
        channel="C1",
        ts="111.222",
        trigger_emoji="hmm_parrot",
        message_template="hi {mentions}",
        rows=[(60.0, "2020-01-01T00:00:00+00:00")],  # in the past → due
    )
    due = await db.due_reminders("2030-01-01T00:00:00+00:00")
    assert len(due) == 1
    assert due[0]["slack_channel"] == "C1"
    assert due[0]["message_template"] == "hi {mentions}"

    await db.mark_reminder_sent(due[0]["id"])
    assert await db.due_reminders("2030-01-01T00:00:00+00:00") == []


async def test_future_reminder_not_due(db: DatabaseManager) -> None:
    await db.schedule_reminders(
        channel="C1", ts="1.2", trigger_emoji="hmm_parrot",
        message_template=None, rows=[(60.0, "2030-01-01T00:00:00+00:00")],
    )
    assert await db.due_reminders("2020-01-01T00:00:00+00:00") == []


async def test_schedule_is_idempotent_per_delay(db: DatabaseManager) -> None:
    """Re-adding the same emoji must not duplicate a reminder for the same delay."""
    for _ in range(2):
        await db.schedule_reminders(
            channel="C1", ts="1.2", trigger_emoji="hmm_parrot",
            message_template=None, rows=[(60.0, "2030-01-01T00:00:00+00:00")],
        )
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM reaction_reminders") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_schedule_for_event_writes_one_row_per_reminder(db: DatabaseManager) -> None:
    config = {
        "reaction_reminders": [
            {
                "channels": ["C1"],
                "trigger_emoji": "hmm_parrot",
                "reminders": [{"after_minutes": 60}, {"after_minutes": 180}],
            }
        ]
    }
    event = {"reaction": "hmm_parrot", "item": {"channel": "C1", "ts": "1.2"}}
    await schedule_for_event(event, config, db)

    conn = db._conn_or_raise()
    async with conn.execute("SELECT after_minutes, message_template FROM reaction_reminders") as cur:
        rows = await cur.fetchall()
    assert sorted(r["after_minutes"] for r in rows) == [60.0, 180.0]
    # No explicit template → the default is stored.
    assert all(r["message_template"] == DEFAULT_REMINDER_TEMPLATE for r in rows)


async def test_schedule_for_event_ignores_unmatched(db: DatabaseManager) -> None:
    config = _config()
    event = {"reaction": "thumbsup", "item": {"channel": "C1", "ts": "1.2"}}
    await schedule_for_event(event, config, db)
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM reaction_reminders") as cur:
        assert (await cur.fetchone())[0] == 0


# ── _send_one: pings real people only ─────────────────────────────────────────


class _FakeSlack:
    """Minimal stand-in for SlackClient used by _send_one."""

    def __init__(self, members, reactors, humans, poster=None, bot_id="U_BOT", post_ok=True):
        self._members = members
        self._reactors = reactors
        self._humans = set(humans)      # ids considered real, active people
        self._poster = poster
        self._bot_id = bot_id
        self._post_ok = post_ok         # False → simulate a failing Slack post
        self.posted: list = []          # records every post attempt (ok or not)

    def get_channel_members(self, channel):
        return self._members

    def get_reactors(self, channel, ts):
        return self._reactors

    def get_message(self, channel, ts):
        return {"user": self._poster} if self._poster else {}

    def get_bot_user_id(self):
        return self._bot_id

    def is_human(self, user_id):
        return user_id in self._humans

    def post_message(self, channel, text, thread_ts=None, broadcast=False):
        self.posted.append((channel, text, thread_ts))
        return self._post_ok


async def test_send_one_pings_only_humans() -> None:
    slack = _FakeSlack(
        members=["U_HUMAN", "U_BOTAPP", "U_POSTER", "U_BOT"],
        reactors=set(),
        humans={"U_HUMAN", "U_POSTER"},   # U_BOTAPP is a bot/app
        poster="U_POSTER",
    )
    reminder = {
        "id": 1, "slack_channel": "C1", "slack_ts": "1.2",
        "message_template": "hey {mentions}",
    }
    await _send_one(slack, reminder)

    assert len(slack.posted) == 1
    _, text, thread_ts = slack.posted[0]
    assert "<@U_HUMAN>" in text
    assert "U_BOTAPP" not in text   # bot filtered out
    assert "U_POSTER" not in text   # poster excluded
    assert "U_BOT" not in text      # bot itself excluded
    assert thread_ts == "1.2"


async def test_send_one_skips_when_only_bots_left() -> None:
    slack = _FakeSlack(
        members=["U_BOT1", "U_BOT2"], reactors=set(), humans=set()
    )
    reminder = {
        "id": 2, "slack_channel": "C1", "slack_ts": "1.2",
        "message_template": "x {mentions}",
    }
    await _send_one(slack, reminder)
    assert slack.posted == []   # nothing posted — no humans to ping


# ── _fire_due: bounded retry on a failing Slack post ──────────────────────────


_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2030-01-01T00:00:00+00:00"


async def _schedule_due(db: DatabaseManager) -> None:
    await db.schedule_reminders(
        channel="C1", ts="1.2", trigger_emoji="hmm_parrot",
        message_template="x {mentions}", rows=[(1.0, _PAST)],
    )


async def test_failed_post_is_retried_then_given_up(db: DatabaseManager) -> None:
    await _schedule_due(db)
    slack = _FakeSlack(members=["U_H"], reactors=set(), humans={"U_H"}, post_ok=False)

    # Each _fire_due is one poll cycle. A failing post keeps the reminder pending…
    for _ in range(_MAX_ATTEMPTS - 1):
        await _fire_due(slack, db)
        assert len(await db.due_reminders(_FUTURE)) == 1     # still pending → will retry

    await _fire_due(slack, db)                               # _MAX_ATTEMPTS-th failure
    assert await db.due_reminders(_FUTURE) == []             # given up → marked sent
    assert len(slack.posted) == _MAX_ATTEMPTS                # tried every cycle


async def test_post_recovers_on_retry(db: DatabaseManager) -> None:
    await _schedule_due(db)
    slack = _FakeSlack(members=["U_H"], reactors=set(), humans={"U_H"}, post_ok=False)

    await _fire_due(slack, db)                               # fails once
    assert len(await db.due_reminders(_FUTURE)) == 1         # still pending

    slack._post_ok = True                                    # Slack recovers
    await _fire_due(slack, db)                               # succeeds
    assert await db.due_reminders(_FUTURE) == []             # marked sent, no more retries
    assert len(slack.posted) == 2


# ── handler wiring: the single reaction_added listener schedules reminders ────


class _FakeApp:
    """Captures functions registered via @app.event(name) without a real Bolt app."""

    def __init__(self) -> None:
        self.handlers: dict = {}

    def event(self, name):
        def deco(fn):
            self.handlers.setdefault(name, []).append(fn)
            return fn
        return deco


async def test_reaction_added_handler_schedules_for_non_processor_emoji(
    db: DatabaseManager,
) -> None:
    """A trigger emoji that is NOT in emoji_mappings must still be scheduled.

    Regression: reminders were a second @app.event('reaction_added') listener,
    which Bolt never reached (it stops after the first). Scheduling now lives
    inside the single handler, before the emoji_mappings early-return.
    """
    from src.slack.event_handler import register_handlers

    config = {
        "emoji_mappings": [
            {"emoji": "face_with_monocle", "notion_db": "x", "processor": "TaskProcessor"}
        ],
        "reaction_reminders": [
            {"channels": ["C1"], "trigger_emoji": "hmm_parrot",
             "reminders": [{"after_minutes": 60}]}
        ],
    }
    app = _FakeApp()
    register_handlers(app, config, processors={}, slack_client=None, db=db)

    handler = app.handlers["reaction_added"][0]
    await handler(
        {"reaction": "hmm_parrot", "user": "U1", "item": {"channel": "C1", "ts": "1.2"}}
    )

    conn = db._conn_or_raise()
    async with conn.execute("SELECT count(*) FROM reaction_reminders") as cur:
        assert (await cur.fetchone())[0] == 1   # row written despite no emoji mapping


# ── config validation ─────────────────────────────────────────────────────────


def test_validate_accepts_none_and_valid() -> None:
    _validate_reaction_reminders(None)  # feature off — no error
    _validate_reaction_reminders(_config()["reaction_reminders"])


@pytest.mark.parametrize(
    "bad",
    [
        [{"channels": ["C1"], "reminders": [{"after_minutes": 60}]}],  # no trigger_emoji
        [{"trigger_emoji": "x", "reminders": [{"after_minutes": 60}]}],  # no channels
        [{"trigger_emoji": "x", "channels": [], "reminders": [{"after_minutes": 60}]}],  # empty channels
        [{"trigger_emoji": "x", "channels": ["C1"], "reminders": []}],  # empty reminders
        [{"trigger_emoji": "x", "channels": ["C1"], "reminders": [{"after_minutes": 0}]}],  # non-positive
        [{"trigger_emoji": "x", "channels": ["C1"], "reminders": [{"after_minutes": True}]}],  # bool
    ],
)
def test_validate_rejects_malformed(bad: list) -> None:
    with pytest.raises(ValueError):
        _validate_reaction_reminders(bad)


# ── wire contract for the Slack calls this feature adds ──────────────────────
# `get_channel_members` and `get_reactors` exist only for reminders, and both
# decide *who gets pinged*: a wrong endpoint or a renamed parameter silently
# produces an empty member list or an empty reactor set, and an empty reactor
# set means everyone in the channel gets @mentioned. That failure is loud in
# Slack and silent in the test suite, so the requests are asserted directly.
# Captured at slack_sdk's send point — nothing here touches the network.


def _capture_wire(call) -> dict:
    from unittest.mock import patch

    from slack_sdk.web.base_client import BaseClient

    cap: dict = {}

    def fake_send(_self, url, req):
        cap["url"] = url
        cap["method"] = req.get_method()
        cap["body"] = req.data.decode() if req.data else None
        return {"status": 200, "headers": {}, "body": '{"ok": true}'}

    with patch.object(BaseClient, "_perform_urllib_http_request_internal", fake_send):
        try:
            call()
        except Exception:
            # The request is captured before the stub's empty response is parsed.
            # Only swallow once something was captured — anything failing before
            # the wire is a real error and must surface as itself.
            if not cap:
                raise
    return cap


def test_get_channel_members_wire() -> None:
    from urllib.parse import parse_qs

    from src.slack.client import SlackClient

    cap = _capture_wire(lambda: SlackClient("xoxb-test").get_channel_members("C1"))

    assert cap["url"] == "https://slack.com/api/conversations.members"
    body = parse_qs(cap["body"])
    assert body["channel"] == ["C1"]
    assert body["limit"] == ["200"]   # paginated; cursor is added on later pages


def test_get_reactors_wire() -> None:
    from urllib.parse import parse_qs

    from src.slack.client import SlackClient

    cap = _capture_wire(lambda: SlackClient("xoxb-test").get_reactors("C1", "1.2"))

    assert cap["url"] == "https://slack.com/api/reactions.get"
    body = parse_qs(cap["body"])
    assert body["channel"] == ["C1"]
    # `timestamp`, not `ts` — a rename here empties the reactor set, which would
    # ping the whole channel instead of nobody.
    assert body["timestamp"] == ["1.2"]
    assert body["full"] == ["1"]      # required, else `reactions[].users` is absent


# ── failed Slack lookups must retry, not guess ───────────────────────────────


class _FailingSlack:
    """A SlackClient whose lookups fail the way the real one does on API errors."""

    def __init__(self, *, members=None, reactors=None) -> None:
        self._members = members
        self._reactors = reactors
        self.posted: list[tuple] = []

    def get_channel_members(self, channel):  # noqa: D102
        return self._members

    def get_reactors(self, channel, ts):     # noqa: D102
        return self._reactors

    def get_message(self, channel, ts):      # noqa: D102
        return {"user": "U_POSTER"}

    def get_bot_user_id(self):               # noqa: D102
        return "U_BOT"

    def is_human(self, user_id):             # noqa: D102
        return True

    def post_message(self, channel, text, thread_ts=None, broadcast=False):
        self.posted.append((channel, text, thread_ts))
        return True


_REMINDER_ROW = {
    "id": 1,
    "slack_channel": "C1",
    "slack_ts": "1.2",
    "message_template": "Waiting on {mentions}",
}


async def test_failed_reactor_lookup_does_not_mention_everyone() -> None:
    """The dangerous direction: a failed reactions.get must not read as 'nobody reacted'.

    Members minus reactors is the ping list, so an empty set from a *failed*
    lookup would @mention the entire channel.
    """
    slack = _FailingSlack(members=["U_A", "U_B", "U_C"], reactors=None)

    handled = await _send_one(slack, _REMINDER_ROW)

    assert handled is False, "must retry, not mark the reminder handled"
    assert slack.posted == [], "posted a reminder from a failed lookup"


async def test_failed_member_lookup_does_not_silently_drop_the_reminder() -> None:
    """The quiet direction: an unknown member list must not read as 'nobody left'."""
    slack = _FailingSlack(members=None, reactors={"U_A"})

    handled = await _send_one(slack, _REMINDER_ROW)

    assert handled is False
    assert slack.posted == []


async def test_genuinely_empty_reactor_set_still_sends() -> None:
    """Guard the other side — a real 'nobody has reacted' must still nudge."""
    slack = _FailingSlack(members=["U_A", "U_B", "U_POSTER"], reactors=set())

    handled = await _send_one(slack, _REMINDER_ROW)

    assert handled is True
    assert len(slack.posted) == 1
    text = slack.posted[0][1]
    assert "<@U_A>" in text and "<@U_B>" in text
    assert "U_POSTER" not in text   # the poster is never nudged


async def test_migration_replays_safely_after_an_interrupted_run(tmp_path) -> None:
    """v3 is an ALTER TABLE; replaying it must not wedge startup.

    Simulates a crash between the schema change and its schema_migrations row:
    the column exists but the version was never recorded, so the next startup
    re-runs the migration. Applying it in one transaction is what makes that
    impossible; this asserts the DB still opens if it ever happened.
    """
    db_path = str(tmp_path / "replay.db")
    db = DatabaseManager(db_path)
    await db.migrate()

    # Roll the recorded version back without touching the schema.
    conn = db._conn_or_raise()
    await conn.execute("DELETE FROM schema_migrations WHERE version = 3")
    await conn.commit()
    await db.close()

    reopened = DatabaseManager(db_path)
    await reopened.migrate()   # must not raise "duplicate column name: attempts"
    conn = reopened._conn_or_raise()
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        assert [r[0] for r in await cur.fetchall()] == [1, 2, 3]
    await reopened.close()
