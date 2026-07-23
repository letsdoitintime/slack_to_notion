#!/usr/bin/env python3
"""Backfill Slack history into ``slack_messages``.

Two gaps this closes:

1. Until 2026-07-22 the bot skipped every bot-authored message in two places,
   so the table has a hole exactly where the integrations post.
2. Anything sent while the bot was down, or before it joined a channel, was
   never seen at all.

Both are gone from the socket stream but still readable through
``conversations.history`` / ``conversations.replies``.

Everything goes through ``DatabaseManager.save_message``, so the write path, the
``INSERT OR IGNORE`` de-duplication and the derived columns are exactly the live
ones. Messages already in the table are left alone — this only inserts.

Each backfilled row is stamped ``"_backfilled": true`` inside ``raw_event`` so it
stays distinguishable from a live-captured event: the envelope is reconstructed
from a history message, not received from Slack.

Names are resolved the way the live handler resolves them — ``users.info`` per
author (cached, so one call per distinct person), and ``bot_profile.name`` →
``username`` for bot posts, which have no user id to look up. Without that,
backfilled rows would land nameless and be *worse* than the rows already there.

Threads
-------
``conversations.history`` returns top-level messages ONLY, and ~91% of this table
is thread replies — so a run without ``--include-threads`` covers a small
minority of the corpus by design. The thread pass costs one API call per parent
that has replies; that parent count is reported either way, so a dry run tells
you the price before you pay it.

Window
------
By default each channel is walked back only as far as its own earliest row, i.e.
the window the bot was already watching. ``--oldest 0`` pulls everything Slack
still retains, including messages predating the bot.

Usage
-----
    python scripts/backfill_history.py                          # dry run
    python scripts/backfill_history.py --include-threads        # dry run, full depth
    python scripts/backfill_history.py --oldest 0               # dry run, all retained
    python scripts/backfill_history.py --bots-only              # the 2026-07-22 gap alone
    python scripts/backfill_history.py --apply --include-threads

Needs ``channels:history`` / ``groups:history`` and ``users:read`` — scopes the
bot already uses. Back the DB up first; the bot writes to it continuously::

    sqlite3 slack_to_notion.db ".backup 'backups/pre-backfill.db'"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv                                   # noqa: E402
from slack_sdk import WebClient                                  # noqa: E402
from slack_sdk.errors import SlackApiError                       # noqa: E402

from src.db.database import DatabaseManager                      # noqa: E402
from src.slack.client import SlackClient                         # noqa: E402
from src.utils.config_loader import load_config                  # noqa: E402

# conversations.history / .replies are Tier 3 (~50 req/min). Stay under it.
_PAUSE_SECONDS = 1.2
_PAGE = 200


def _is_bot_message(msg: dict) -> bool:
    return bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message"


def _bot_name(msg: dict) -> str | None:
    return (msg.get("bot_profile") or {}).get("name") or msg.get("username")


def _as_event(msg: dict, channel: str) -> dict:
    """Rebuild the event envelope save_message expects from a history message.

    A history message carries no ``channel`` and no ``type``; everything else
    (ts, thread_ts, user, bot_id, subtype, text, files) is already in the shape
    the live handler sees.
    """
    return {**msg, "type": "message", "channel": channel, "_backfilled": True}


class _Names:
    """users.info per distinct author, cached — the same names the live handler
    writes. A bot post has no user id to look up, so its name comes off the
    event itself."""

    def __init__(self, slack: SlackClient) -> None:
        self.slack = slack
        self._cache: dict[str, dict] = {}
        self.lookups = 0

    def resolve(self, msg: dict) -> tuple[str | None, str | None]:
        user_id = msg.get("user")
        if not user_id:
            return _bot_name(msg), None
        if user_id not in self._cache:
            self.lookups += 1
            self._cache[user_id] = self.slack.get_user_info(user_id)
        info = self._cache[user_id]
        return info.get("name"), info.get("email")


def _call(fn, **kwargs):
    """One Slack call, retrying once on an explicit rate-limit response."""
    try:
        return fn(**kwargs)
    except SlackApiError as exc:
        if exc.response.status_code != 429:
            raise
        wait = int(exc.response.headers.get("Retry-After", 30))
        print(f"    rate limited — sleeping {wait}s", flush=True)
        time.sleep(wait)
        return fn(**kwargs)


def _history(client: WebClient, channel: str, oldest: str) -> list[dict]:
    messages: list[dict] = []
    cursor = ""
    while True:
        kwargs = {"channel": channel, "limit": _PAGE, "oldest": oldest}
        if cursor:
            kwargs["cursor"] = cursor
        resp = _call(client.conversations_history, **kwargs)
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return messages
        time.sleep(_PAUSE_SECONDS)


def _bot_channels(client: WebClient) -> list[str]:
    """Every conversation the bot is a member of — not just the ones it has
    already recorded something in. A channel the bot joined but that has been
    quiet since is invisible to the DB and would otherwise never be walked."""
    ids: list[str] = []
    cursor = ""
    while True:
        kwargs = {
            "types": "public_channel,private_channel",
            "limit": 200,
            "exclude_archived": False,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = _call(client.users_conversations, **kwargs)
        ids.extend(c["id"] for c in resp.get("channels", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return ids
        time.sleep(_PAUSE_SECONDS)


def _replies(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    resp = _call(client.conversations_replies, channel=channel, ts=thread_ts, limit=_PAGE)
    # The first element is the parent, already seen in history.
    return resp.get("messages", [])[1:]


def _selftest() -> int:
    """Check the pure helpers. The envelope rebuild is the risky one — a wrong
    `channel` would file rows under the wrong conversation silently."""
    assert _is_bot_message({"bot_id": "B1"})
    assert _is_bot_message({"subtype": "bot_message"})
    assert not _is_bot_message({"user": "U1", "text": "hi"})
    assert not _is_bot_message({"subtype": "channel_join", "user": "U1"})

    assert _bot_name({"bot_profile": {"name": "Zapier"}, "username": "z"}) == "Zapier"
    assert _bot_name({"username": "Jenkins"}) == "Jenkins"
    assert _bot_name({"bot_profile": {}, "username": "Jenkins"}) == "Jenkins"
    assert _bot_name({"bot_id": "B1"}) is None

    event = _as_event({"ts": "1.0", "bot_id": "B1", "text": "hi"}, "C_TARGET")
    assert event["channel"] == "C_TARGET"      # history messages carry no channel
    assert event["type"] == "message"          # nor a type
    assert event["_backfilled"] is True        # provenance marker
    assert event["ts"] == "1.0" and event["bot_id"] == "B1"
    # The source message must not be mutated — the caller reuses it.
    source = {"ts": "2.0", "bot_id": "B2"}
    _as_event(source, "C1")
    assert "channel" not in source

    class _StubSlack:
        def __init__(self) -> None:
            self.calls = 0

        def get_user_info(self, user_id: str) -> dict:
            self.calls += 1
            return {"id": user_id, "name": "Alice", "email": "a@example.com"}

    stub = _StubSlack()
    names = _Names(stub)
    assert names.resolve({"user": "U1"}) == ("Alice", "a@example.com")
    assert names.resolve({"user": "U1"}) == ("Alice", "a@example.com")
    assert stub.calls == 1                     # cached — one call per distinct person
    assert names.resolve({"bot_id": "B1", "username": "Jenkins"}) == ("Jenkins", None)
    assert stub.calls == 1                     # a bot post costs no lookup

    print("selftest OK")
    return 0


async def _assert_fix_present() -> None:
    """Abort if save_message still drops bot messages.

    Running this from a checkout without the 2026-07-22 fix would walk every
    channel, find plenty, and write nothing — a silent no-op that reads as a
    successful run.
    """
    db = DatabaseManager(":memory:")
    await db.migrate()
    await db.save_message(
        {"type": "message", "subtype": "bot_message", "channel": "C_PROBE",
         "ts": "1.0", "bot_id": "B_PROBE", "text": "probe"}
    )
    conn = db._conn_or_raise()
    async with conn.execute("SELECT COUNT(*) FROM slack_messages") as cur:
        saved = (await cur.fetchone())[0]
    await db.close()
    if not saved:
        sys.exit(
            "ABORT: save_message still skips bot messages. This checkout predates "
            "the 2026-07-22 fix — the backfill would silently write nothing."
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(_ROOT / "slack_to_notion.db"))
    parser.add_argument("--config", default=str(_ROOT / "config" / "config.yaml"))
    parser.add_argument("--channels", nargs="*", help="default: every channel in the DB")
    parser.add_argument(
        "--oldest",
        help="unix ts floor; default: per-channel earliest row. 0 = all retained history",
    )
    parser.add_argument("--include-threads", action="store_true")
    parser.add_argument(
        "--bots-only", action="store_true", help="only bot-authored messages"
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="only channels already in the table (default: ask Slack what the bot is in)",
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--selftest", action="store_true", help="check helpers, exit")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    load_dotenv(_ROOT / ".env")
    config = load_config(args.config)
    token = config["slack"]["bot_token"]
    client = WebClient(token=token)
    names = _Names(SlackClient(token))

    db = DatabaseManager(args.db)
    await db.migrate()
    conn = db._conn_or_raise()

    # Per-channel floor: where the table already starts. Absent for a channel
    # the bot has recorded nothing in, which correctly means "from the top".
    async with conn.execute(
        "SELECT slack_channel, MIN(slack_ts) FROM slack_messages GROUP BY slack_channel"
    ) as cur:
        floors = {r[0]: r[1] for r in await cur.fetchall()}

    def _floor(channel: str) -> str:
        return args.oldest if args.oldest is not None else floors.get(channel, "0")

    if args.channels:
        ids = args.channels
    elif args.from_db:
        ids = sorted(floors, key=lambda c: floors[c])
    else:
        ids = await asyncio.to_thread(_bot_channels, client)
        unseen = [c for c in ids if c not in floors]
        print(f"Discovered {len(ids)} channel(s) the bot is in"
              f"{f' — {len(unseen)} with nothing recorded yet' if unseen else ''}.")
    channels = [(c, _floor(c)) for c in ids]

    if args.apply:
        await _assert_fix_present()

    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}"
        f"{' +threads' if args.include_threads else ''}"
        f"{' bots-only' if args.bots_only else ''}"
        f" — {len(channels)} channel(s)\n"
    )

    totals = {"scanned": 0, "bot": 0, "threads": 0, "new": 0}
    for channel, oldest in channels:
        try:
            messages = await asyncio.to_thread(_history, client, channel, oldest)
        except SlackApiError as exc:
            print(f"  {channel:<14} SKIPPED — {exc.response.get('error')}")
            continue

        # Counted even without --include-threads: reply_count rides along in the
        # history payload, so this is the cost estimate for a threads run, free.
        parents = [m for m in messages if m.get("reply_count")]

        # A thread whose replies are all already recorded needs no API call —
        # one local SELECT instead. Most threads inside the window the bot was
        # already watching fall out here, which is the difference between a
        # multi-hour run and a manageable one.
        async with conn.execute(
            "SELECT slack_thread_ts, COUNT(*) FROM slack_messages "
            "WHERE slack_channel=? AND slack_thread_ts IS NOT NULL "
            "AND slack_ts != slack_thread_ts GROUP BY slack_thread_ts",
            (channel,),
        ) as cur:
            recorded = {r[0]: r[1] for r in await cur.fetchall()}
        parents = [
            p for p in parents if recorded.get(p["ts"], 0) < p.get("reply_count", 0)
        ]
        totals["threads"] += len(parents)

        if args.include_threads:
            for parent in parents:
                try:
                    messages.extend(
                        await asyncio.to_thread(_replies, client, channel, parent["ts"])
                    )
                except SlackApiError as exc:
                    print(f"  {channel:<14} thread {parent['ts']} — "
                          f"{exc.response.get('error')}")
                await asyncio.to_thread(time.sleep, _PAUSE_SECONDS)

        wanted = [m for m in messages if not args.bots_only or _is_bot_message(m)]
        totals["scanned"] += len(messages)
        totals["bot"] += sum(1 for m in wanted if _is_bot_message(m))

        # Which of those are genuinely absent — the number that matters.
        new = 0
        channel_name = None
        for msg in wanted:
            async with conn.execute(
                "SELECT 1 FROM slack_messages WHERE slack_channel=? AND slack_ts=?",
                (channel, msg.get("ts", "")),
            ) as cur:
                if await cur.fetchone():
                    continue
            new += 1
            if not args.apply:
                continue
            if channel_name is None:
                channel_name = await asyncio.to_thread(
                    names.slack.get_channel_name, channel
                )
            user_name, user_email = await asyncio.to_thread(names.resolve, msg)
            await db.save_message(
                _as_event(msg, channel),
                channel_name=channel_name,
                user_name=user_name,
                user_email=user_email,
            )
        totals["new"] += new

        print(f"  {channel:<14} scanned {len(messages):>6}  "
              f"{'wrote' if args.apply else 'missing'} {new:>5}")
        await asyncio.to_thread(time.sleep, _PAUSE_SECONDS)

    await db.close()
    print(
        f"\nscanned {totals['scanned']}  of which bot {totals['bot']}  "
        f"threads with replies {totals['threads']}  "
        f"users looked up {names.lookups}  "
        f"{'written' if args.apply else 'missing'} {totals['new']}"
    )
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
