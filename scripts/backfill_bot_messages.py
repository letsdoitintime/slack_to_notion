#!/usr/bin/env python3
"""Backfill bot-authored messages that were dropped before 2026-07-22.

Until then the bot skipped every bot-authored message in two places, so
``slack_messages`` has a hole exactly where the integrations post. Those events
are gone from the socket stream but still readable through
``conversations.history``, which is what this pulls.

Scope
-----
Bot messages only. Human messages are already recorded, and re-inserting them
here would land rows with no resolved user name — worse than the rows already
there. Everything goes through ``DatabaseManager.save_message``, so the write
path, the ``INSERT OR IGNORE`` de-duplication and the derived columns are
exactly the live ones.

Each backfilled row is stamped ``"_backfilled": true`` inside ``raw_event`` so
it stays distinguishable from a live-captured event — the envelope is
reconstructed from a history message, not received from Slack.

By default each channel is walked back only as far as its own earliest row,
i.e. the window this bot was actually watching. ``--oldest`` overrides.

``conversations.history`` returns top-level messages only. ``--include-threads``
additionally walks ``conversations.replies`` for every parent with replies,
which costs one extra API call per thread.

Usage
-----
    python scripts/backfill_bot_messages.py                    # dry run (default)
    python scripts/backfill_bot_messages.py --include-threads  # dry run, with threads
    python scripts/backfill_bot_messages.py --apply            # write

Needs ``channels:history`` / ``groups:history`` — the same scopes the bot
already uses to fetch a reacted message. Back the DB up first; the bot writes
to it continuously.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv                                   # noqa: E402
from slack_sdk import WebClient                                  # noqa: E402
from slack_sdk.errors import SlackApiError                       # noqa: E402

from src.db.database import DatabaseManager                      # noqa: E402
from src.utils.config_loader import load_config                  # noqa: E402

# conversations.history / .replies are Tier 3 (~50 req/min). Stay well under.
_PAUSE_SECONDS = 1.2
_PAGE = 200


def _is_bot_message(msg: dict) -> bool:
    return bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message"


def _bot_name(msg: dict) -> str | None:
    return (msg.get("bot_profile") or {}).get("name") or msg.get("username")


def _as_event(msg: dict, channel: str) -> dict:
    """Rebuild the event envelope save_message expects from a history message.

    A history message carries no ``channel`` and no ``type``; everything else
    (ts, thread_ts, bot_id, subtype, text, files) is already in the shape the
    live handler sees.
    """
    return {**msg, "type": "message", "channel": channel, "_backfilled": True}


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


def _replies(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    resp = _call(client.conversations_replies, channel=channel, ts=thread_ts, limit=_PAGE)
    # The first element is the parent, already seen in history.
    return resp.get("messages", [])[1:]


def _selftest() -> int:
    """Check the three pure helpers. The envelope rebuild is the risky one —
    a wrong `channel` would file rows under the wrong conversation silently."""
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
    # The source message must not be mutated — it is reused by the caller.
    source = {"ts": "2.0", "bot_id": "B2"}
    _as_event(source, "C1")
    assert "channel" not in source

    print("selftest OK")
    return 0


async def _assert_fix_present(probe: dict) -> None:
    """Abort if save_message still drops bot messages.

    Running this from a checkout without the 2026-07-22 fix would walk every
    channel, find plenty, and write nothing — a silent no-op that looks like a
    successful run.
    """
    db = DatabaseManager(":memory:")
    await db.migrate()
    await db.save_message(probe)
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
    parser.add_argument("--oldest", help="unix ts floor; default: per-channel earliest row")
    parser.add_argument("--include-threads", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--selftest", action="store_true", help="check helpers, exit")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    load_dotenv(_ROOT / ".env")
    config = load_config(args.config)
    client = WebClient(token=config["slack"]["bot_token"])

    db = DatabaseManager(args.db)
    await db.migrate()
    conn = db._conn_or_raise()

    if args.channels:
        channels = [(c, args.oldest or "0") for c in args.channels]
    else:
        async with conn.execute(
            "SELECT slack_channel, MIN(slack_ts) FROM slack_messages "
            "GROUP BY slack_channel ORDER BY COUNT(*) DESC"
        ) as cur:
            channels = [(r[0], args.oldest or r[1]) for r in await cur.fetchall()]

    if args.apply:
        probe = {"type": "message", "subtype": "bot_message", "channel": "C_PROBE",
                 "ts": "1.0", "bot_id": "B_PROBE", "text": "probe"}
        await _assert_fix_present(probe)

    mode = "APPLY" if args.apply else "DRY RUN"
    threads = " +threads" if args.include_threads else ""
    print(f"{mode}{threads} — {len(channels)} channel(s)\n")

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

        bots = [m for m in messages if _is_bot_message(m)]
        totals["scanned"] += len(messages)
        totals["bot"] += len(bots)

        # Which of those are genuinely absent — the number that matters.
        new = 0
        channel_name = None
        for msg in bots:
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
                try:
                    resp = await asyncio.to_thread(
                        _call, client.conversations_info, channel=channel
                    )
                    channel_name = resp["channel"].get("name", channel)
                except SlackApiError:
                    channel_name = channel
            await db.save_message(
                _as_event(msg, channel),
                channel_name=channel_name,
                user_name=_bot_name(msg),
            )
        totals["new"] += new

        print(f"  {channel:<14} scanned {len(messages):>5}  bot {len(bots):>4}  "
              f"{'wrote' if args.apply else 'missing'} {new:>4}")
        await asyncio.to_thread(time.sleep, _PAUSE_SECONDS)

    await db.close()
    print(
        f"\nscanned {totals['scanned']}  bot messages {totals['bot']}  "
        f"threads walked {totals['threads']}  "
        f"{'written' if args.apply else 'missing'} {totals['new']}"
    )
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
