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
    python scripts/backfill_history.py --bots-only --include-threads   # bot gap only
    python scripts/backfill_history.py --apply --include-threads

Needs ``channels:history`` / ``groups:history`` and ``users:read`` — scopes the
bot already uses. Back the DB up first; the bot writes to it continuously::

    sqlite3 slack_to_notion.db ".backup 'backups/pre-backfill.db'"
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
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
_RATE_LIMIT_RETRIES = 5

# The bot is writing to this database the whole time we are. It is in rollback-
# journal mode, so a writer locks the file outright — and the live handler wraps
# its save in a `logger.exception`, meaning a lock it cannot acquire drops a real
# message and only leaves a log line. Wait a long time rather than let that
# happen: our writes are milliseconds each, so this ceiling should never be
# approached, and if it is, being slow beats making the bot lose messages.
_BUSY_TIMEOUT_MS = 30_000


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


def _configured_db(config: dict) -> Path:
    """The database the bot actually uses.

    ``main.py`` reads ``database.path`` relative to its cwd, which supervisor
    pins to the repo root. Hard-coding the filename here would silently target a
    different file the moment a deployment changes that setting — with
    ``--apply`` that means creating, migrating and populating a side database
    while the real one stays exactly as broken as it was.
    """
    path = Path((config.get("database") or {}).get("path") or "slack_to_notion.db")
    return path if path.is_absolute() else _ROOT / path


class _NameLookupFailed(Exception):
    """users.info failed for a real author, so the message is left unwritten.

    ``SlackClient.get_user_info`` substitutes the user id for the name when the
    call fails. That is right for the live bot — a task with an odd reporter name
    beats no task — but poison here: written to a row it is indistinguishable
    from data, and a re-run will not repair it because that ``(channel, ts)``
    already exists. Skipping leaves the message for the next run instead.
    """


class _Names:
    """users.info per distinct author, cached — the same names the live handler
    writes. A bot post has no user id to look up, so its name comes off the
    event itself."""

    def __init__(self, client: WebClient) -> None:
        self._client = client
        self._cache: dict[str, dict] = {}
        self.lookups = 0

    def resolve(self, msg: dict) -> tuple[str | None, str | None]:
        user_id = msg.get("user")
        if not user_id:
            return _bot_name(msg), None
        if user_id not in self._cache:
            self.lookups += 1
            try:
                # Through _call, so rate limits are waited out rather than
                # turning into a cached wrong name.
                user = _call(self._client.users_info, user=user_id)["user"]
            except SlackApiError as exc:
                raise _NameLookupFailed(
                    f"{user_id}: {exc.response.get('error')}"
                ) from exc
            self._cache[user_id] = {
                "name": user.get("real_name") or user.get("name") or user_id,
                "email": (user.get("profile") or {}).get("email"),
            }
        info = self._cache[user_id]
        return info["name"], info["email"]


def _call(fn, **kwargs):
    """One Slack call, waiting out rate limits.

    Retries several times, not once: a full run is hours long and a single
    unlucky 429 near the end would otherwise throw away the walk in progress.
    """
    for attempt in range(_RATE_LIMIT_RETRIES):
        try:
            return fn(**kwargs)
        except SlackApiError as exc:
            if exc.response.status_code != 429 or attempt == _RATE_LIMIT_RETRIES - 1:
                raise
            wait = int(exc.response.headers.get("Retry-After", 30))
            print(f"    rate limited — sleeping {wait}s", flush=True)
            time.sleep(wait)


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


def _dedupe_by_ts(messages: list[dict]) -> list[dict]:
    """One entry per ts, preferring the thread reply over its channel pointer.

    A reply broadcast to the channel arrives twice under ``--include-threads``:
    once from ``conversations.history`` and once from ``conversations.replies``.
    Measured on a real channel here: 1234 fetched, 1210 distinct ts, 24 duplicated.

    The cost is the count. ``(channel, ts)`` is one row, so the second copy was
    never going to be written — but a dry run counts before it writes, so every
    broadcast inflated the number of messages the run claimed were missing.

    Slack documents ``thread_broadcast`` as a channel reference to a message
    living in the thread, so the tie-break prefers the thread's copy. In this
    workspace that never fires: both copies come back carrying
    ``subtype: thread_broadcast`` and compare byte-identical. Kept anyway — it
    costs four lines and covers the case where they are not.
    """
    best: dict[str, dict] = {}
    for msg in messages:
        ts = msg.get("ts", "")
        current = best.get(ts)
        if current is None or (
            current.get("subtype") == "thread_broadcast"
            and msg.get("subtype") != "thread_broadcast"
        ):
            best[ts] = msg      # dict keeps the first insertion's position
    return list(best.values())


def _threads_to_walk(
    parents: list[dict], recorded: dict[str, int], fetched_ts: set
) -> list[dict]:
    """Which threads still need a ``conversations.replies`` call.

    Two sources, and the second is easy to miss:

    - Parents returned by history whose recorded reply count falls short of
      ``reply_count``. A thread already fully recorded needs no API call — one
      local ``SELECT`` answers it.
    - Threads the table has replies for whose parent was never fetched, because
      it predates the window floor. Someone reviving an old thread after the bot
      was installed leaves exactly that shape: replies recorded, parent absent.
      History anchored at the floor never returns that parent, so without this
      the thread is never walked and its dropped bot replies stay missing.
      Their true ``reply_count`` is unknowable from here, so they are always
      walked rather than guessed at.
    """
    todo = [p for p in parents if recorded.get(p["ts"], 0) < p.get("reply_count", 0)]
    todo.extend({"ts": ts} for ts in sorted(recorded) if ts not in fetched_ts)
    return todo


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


def _replies(
    client: WebClient, channel: str, thread_ts: str, oldest: str
) -> list[dict]:
    """Every reply in a thread at or after *oldest*, following pagination.

    The floor is forwarded here, not just applied to the history walk. A revived
    thread has a parent older than the floor, and asking for that thread
    unqualified returns replies from before the window too — which the write
    loop, checking only whether a message is absent, would happily insert. A
    default run would then quietly contain pre-window data from exactly the
    threads that happen to have been revived, while telling the operator that
    reaching before the window needs ``--oldest 0``.

    Trimming is also what the orphan-thread walk wants: the replies worth
    recovering there are the ones the bot should have captured and dropped,
    which are by definition inside the window.

    Threads longer than one page are uncommon but not hypothetical, and skipping
    the cursor would leave them permanently incomplete in a way that hides
    itself: the parent's ``reply_count`` would never match what is recorded, so
    the already-complete check in the caller could never fire and every future
    run would re-fetch the same first page and report success.

    The parent is filtered by ts rather than by position — it is returned as the
    first message and we have already seen it in history.
    """
    messages: list[dict] = []
    cursor = ""
    while True:
        kwargs = {"channel": channel, "ts": thread_ts, "limit": _PAGE,
                  "oldest": oldest}
        if cursor:
            kwargs["cursor"] = cursor
        resp = _call(client.conversations_replies, **kwargs)
        messages.extend(
            m for m in resp.get("messages", []) if m.get("ts") != thread_ts
        )
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return messages
        time.sleep(_PAUSE_SECONDS)


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

        def users_info(self, **kwargs) -> dict:
            self.calls += 1
            return {"user": {"real_name": "Alice",
                             "profile": {"email": "a@example.com"}}}

    # A broadcast reply arrives twice — pointer from history, real message from
    # the thread. Same ts, so one row: keep the thread's copy, and count it once.
    deduped = _dedupe_by_ts([
        {"ts": "1", "subtype": "thread_broadcast", "text": "pointer"},
        {"ts": "1", "text": "the real reply"},
        {"ts": "2", "text": "unrelated"},
    ])
    assert [m["ts"] for m in deduped] == ["1", "2"], deduped
    assert deduped[0]["text"] == "the real reply"
    # Order-independent: the pointer must lose whichever way round they arrive.
    reversed_order = _dedupe_by_ts([
        {"ts": "1", "text": "the real reply"},
        {"ts": "1", "subtype": "thread_broadcast", "text": "pointer"},
    ])
    assert reversed_order[0]["text"] == "the real reply", reversed_order

    # Which threads need walking. A fully-recorded thread costs a SELECT, not a
    # call; a thread whose parent predates the window is invisible to history and
    # would otherwise never be walked at all.
    parents = [{"ts": "done", "reply_count": 2}, {"ts": "short", "reply_count": 5}]
    recorded = {"done": 2, "short": 1, "orphan": 3}
    todo = _threads_to_walk(parents, recorded, {"done", "short"})
    assert [p["ts"] for p in todo] == ["short", "orphan"], todo
    # An unfetched parent is walked even though replies are already recorded —
    # its real reply_count cannot be known from here.
    assert _threads_to_walk([], {"orphan": 9}, set()) == [{"ts": "orphan"}]
    assert _threads_to_walk([], {"orphan": 9}, {"orphan"}) == []

    # Thread pagination — this shipped broken: one page fetched, cursor dropped.
    # It hid itself, because an incomplete thread can never satisfy the
    # already-complete check, so every later run re-fetched page one and passed.
    class _PagedClient:
        def __init__(self) -> None:
            self.cursors: list = []

        def conversations_replies(self, **kwargs):
            self.cursors.append(kwargs.get("cursor"))
            self.oldest = kwargs.get("oldest")
            if not kwargs.get("cursor"):
                return {"messages": [{"ts": "T"}, {"ts": "r1"}, {"ts": "r2"}],
                        "response_metadata": {"next_cursor": "page2"}}
            # Parent repeated on the later page — filtered by ts, not position.
            return {"messages": [{"ts": "T"}, {"ts": "r3"}]}

    global _PAUSE_SECONDS
    saved, _PAUSE_SECONDS = _PAUSE_SECONDS, 0
    try:
        paged = _PagedClient()
        replies = _replies(paged, "C1", "T", "1700000000.0")
    finally:
        _PAUSE_SECONDS = saved
    assert [m["ts"] for m in replies] == ["r1", "r2", "r3"], replies
    assert paged.cursors == [None, "page2"]    # the cursor was actually followed
    # The window floor reaches the thread call too, or a default run quietly
    # pulls pre-window replies out of revived threads.
    assert paged.oldest == "1700000000.0"

    stub = _StubSlack()
    names = _Names(stub)
    assert names.resolve({"user": "U1"}) == ("Alice", "a@example.com")
    assert names.resolve({"user": "U1"}) == ("Alice", "a@example.com")
    assert stub.calls == 1                     # cached — one call per distinct person
    assert names.resolve({"bot_id": "B1", "username": "Jenkins"}) == ("Jenkins", None)
    assert stub.calls == 1                     # a bot post costs no lookup

    # A failed lookup must never become a name. The live client's fallback is
    # the user id, which written to a row is indistinguishable from data.
    class _Resp:
        status_code = 404
        headers: dict = {}

        def get(self, key, default=None):
            return {"error": "user_not_found"}.get(key, default)

    class _FailingUsers:
        def users_info(self, **kwargs):
            raise SlackApiError("nope", _Resp())

    failing = _Names(_FailingUsers())
    try:
        failing.resolve({"user": "U9"})
    except _NameLookupFailed:
        pass
    else:
        raise AssertionError("a failed users.info must not yield a name")
    assert "U9" not in failing._cache          # and must not be cached as one

    # Config-driven db path, so a deployment that moves the file is followed.
    assert _configured_db({"database": {"path": "/tmp/other.db"}}) == Path("/tmp/other.db")
    assert _configured_db({"database": {"path": "x.db"}}) == _ROOT / "x.db"
    assert _configured_db({}) == _ROOT / "slack_to_notion.db"

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
    parser.add_argument("--db", help="default: database.path from the config")
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
    names = _Names(client)
    # Channel names still go through SlackClient: its fallback is the channel id,
    # which is truthful, unlike substituting a user id for a person's name.
    slack = SlackClient(token)

    db_path = args.db or str(_configured_db(config))
    print(f"database: {db_path}")
    db = DatabaseManager(db_path)

    # migrate() opens the connection itself and writes before we get a handle to
    # set busy_timeout on it, so that one bootstrap write runs on sqlite3's ~5s
    # default rather than the 30s the rest of this relies on. Retry rather than
    # reach into DatabaseManager: a startup crash here is loud and safe to rerun,
    # but a 3.8h unattended job should not fall over on a five-second lock.
    for attempt in range(_RATE_LIMIT_RETRIES):
        try:
            await db.migrate()
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _RATE_LIMIT_RETRIES - 1:
                raise
            print(f"    database locked on open — retrying ({exc})", flush=True)
            await asyncio.sleep(5)

    conn = db._conn_or_raise()
    await conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

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

    # One class of thread cannot be discovered from a floored run at all: parent
    # older than the floor, and its only later activity was bot replies. History
    # anchored at the floor never returns the parent, and because those replies
    # were the ones being dropped, `recorded` has no row for it either. Nothing
    # local knows it exists. Say so rather than let the run look exhaustive.
    # ponytail: warn, don't engineer. Finding them needs a full history walk per
    # channel filtered on `latest_reply` — worth building only if the floored
    # run ever becomes the mode people actually use.
    if args.include_threads and any(str(f) != "0" for _, f in channels):
        print(
            "NOTE: --include-threads with a per-channel floor cannot discover a\n"
            "      thread whose parent predates the floor and whose only later\n"
            "      activity was bot replies — nothing was recorded, so nothing\n"
            "      points at it. Use --oldest 0 for an exhaustive pass.\n"
        )

    totals = {"scanned": 0, "bot": 0, "threads": 0, "new": 0}
    # A partial run that exits 0 is indistinguishable from a complete one. Over a
    # multi-hour unattended walk, one channel dying on missing_scope prints a
    # single line mid-scroll and would otherwise still report success.
    failures: list[str] = []
    for channel, oldest in channels:
        try:
            messages = await asyncio.to_thread(_history, client, channel, oldest)
        except SlackApiError as exc:
            failures.append(f"{channel} — history: {exc.response.get('error')}")
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
        parents = _threads_to_walk(parents, recorded, {m.get("ts") for m in messages})
        totals["threads"] += len(parents)

        if args.include_threads:
            for parent in parents:
                try:
                    messages.extend(
                        await asyncio.to_thread(
                            _replies, client, channel, parent["ts"], oldest
                        )
                    )
                except SlackApiError as exc:
                    failures.append(
                        f"{channel} — thread {parent['ts']}: "
                        f"{exc.response.get('error')}"
                    )
                    print(f"  {channel:<14} thread {parent['ts']} — "
                          f"{exc.response.get('error')}")
                await asyncio.to_thread(time.sleep, _PAUSE_SECONDS)

        messages = _dedupe_by_ts(messages)
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
                channel_name = await asyncio.to_thread(slack.get_channel_name, channel)
            try:
                user_name, user_email = await asyncio.to_thread(names.resolve, msg)
            except _NameLookupFailed as exc:
                # Leave it for the next run rather than write a name we invented.
                failures.append(f"{channel} — {msg.get('ts')}: users.info {exc}")
                new -= 1
                continue
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
    if not args.include_threads:
        # Says this on every threadless run, not just the one whose usage example
        # used to overclaim. Most of this table is thread replies, so a summary
        # ending in "written N" without this line reads as a complete pass when
        # it covered top-level messages only.
        print(
            f"NOTE: thread replies were NOT scanned. {totals['threads']} thread(s) "
            "here have replies;\n      most of this table is replies, so this pass "
            "covered top-level messages only. Add --include-threads."
        )

    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")

    if failures:
        print(f"\nINCOMPLETE — {len(failures)} failure(s), work above was still done:")
        for failure in failures:
            print(f"  {failure}")
        print("Re-run to retry; anything already written is skipped.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
