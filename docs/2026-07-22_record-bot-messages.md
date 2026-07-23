# 2026-07-22 — Record messages from bots

Messages posted by other bots and integrations were never written to
`slack_messages`. Reported as "the bot is not recording messages from other bots,
but when I react it can see it".

## Root cause: two independent skips

Both had to go — removing either alone changes nothing.

| where | code |
|---|---|
| `src/slack/event_handler.py` | `if event.get("bot_id") or subtype == "bot_message": return` |
| `src/db/database.py` (`save_message`) | `if subtype == "bot_message" or bot_id: return` |

The events were always arriving — 153 `bot_message` events in the current log,
mostly two channels. The skip logged at DEBUG while the root logger is INFO, so
the trail ends at the "Message event —" line and looks like a delivery problem.

## Why reacting worked anyway

The reaction path never reads the message text from the DB.
`TaskProcessor.process` fetches it live from Slack (`slack.get_message`), and uses
the DB only as an idempotency ledger. A missing row costs it nothing — which is
why this went unnoticed.

## The half that *was* landing

`message_changed` is handled **above** the skip in the handler, and a
`message_changed` wrapper carries no top-level `bot_id` (it sits on the nested
`event["message"]`), so `save_message`'s guard missed it too. The UPDATE found no
original row and the fallback INSERT fired.

Net effect before this change: originals absent, edits present as authorless
rows. In prod, 128 of 171 `message_changed` rows have no user — and 0 of 16582
rows had a non-null `slack_bot_id`, a column that has existed since v1.

## Changes

- Both skips deleted. No migration — the schema already had `slack_bot_id`.
- The `message_changed` fallback INSERT now reads `bot_id` from the nested
  message, so an edit to a not-yet-saved bot post keeps its author.
- Bot display name resolved inline from `bot_profile.name` → `username`. A
  legacy/webhook post has no `user` id at all, so `users.info` cannot name it;
  apps posting with a bot token do send `user` and take the normal lookup path.

**This bot's own posts are archived too, deliberately** — they are part of the
channel record, and saving a message posts nothing, so there is no echo loop.
Pinned by a test; filter on a specific `bot_id` in the handler if it ever
becomes noise.

## Tests

`tests/test_message_handler.py` (new, 8) — the `message` listener had no coverage
at all, which is how a `return` this load-bearing survived. Plus 3 in
`tests/test_database.py`, replacing the two that pinned the old skip.

9 of the 11 fail against the unfixed code; the other 2 are regression guards for
the human-message path, which must pass both ways.

Full suite: 321 passed, 4 skipped.

## Recovering what was already lost

Two scripts, both dry-run by default. One-shots — new rows are correct at write
time — but safe to re-run.

### `scripts/repair_bot_authorship.py`

Lifts `bot_id` and the display name out of `raw_event` into their columns for the
orphan `message_changed` rows. Nothing was ever lost, only unread. 130 rows
across 9 bots (`SUP - Payment Case Closed` 55, `SUP - Opened Cases List` 53,
`CB RF flow` 10, then a tail of one-offs).

### `scripts/backfill_history.py`

Re-reads `conversations.history` / `.replies` and inserts what is absent. Writes
go through `DatabaseManager.save_message`, so the write path, the
`INSERT OR IGNORE` de-duplication and the derived columns are the live ones. Each
row is stamped `"_backfilled": true` inside `raw_event`, because the envelope is
reconstructed from a history message rather than received from Slack.

It records messages. It does **not** create Notion tasks — those still only come
from a live reaction.

Names are resolved the way the live handler resolves them: `users.info` per
author, cached to one call per distinct person, and `bot_profile.name` →
`username` for bot posts, which have no user id to look up. Without that,
backfilled rows would land nameless and be *worse* than the rows already there —
which is the only reason the first cut of this script was bot-only.

Guards and shortcuts worth knowing about:

- It **aborts** if `save_message` still skips bot messages. Run from a checkout
  predating this fix it would otherwise walk every channel, find plenty, and
  write nothing — a silent no-op that reads as a clean run.
- Channels come from `users.conversations`, i.e. what the bot is actually in —
  not from the table, which cannot show a channel the bot joined but that has
  been quiet since. That found one extra channel with 134 messages and no rows.
- A thread whose replies are all already recorded is skipped on a local `SELECT`
  rather than an API call — 1420 of 12883 threads at full history, and 12883 of
  13050 at the default floor, where nearly everything is already captured.
- Conversely, a thread whose *parent* predates the window floor is invisible to
  the history walk: someone reviving an old thread after the bot was installed
  leaves replies recorded with no fetched parent. Those are walked from the
  table instead. Five such threads at full history, more at a tighter floor.
- One class of thread is undiscoverable from a floored run and the script says
  so rather than looking exhaustive: parent older than the floor, and its only
  later activity was bot replies. History at the floor never returns the parent,
  and since those replies were exactly what was being dropped, nothing in the
  table points at it either. `--oldest 0` has no such gap.
- A partial run exits non-zero. A channel lost to `missing_scope` or a spent
  retry budget prints one line mid-scroll, and over a multi-hour unattended walk
  the exit code is the only thing anyone actually reads.
- Both `conversations.history` and `conversations.replies` follow their cursors.
  Review caught the replies walk reading only the first page — a failure that
  hides itself, because a thread left incomplete can never satisfy the
  already-complete check above, so every later run re-fetches page one and
  reports success. Covered by `--selftest`.
- Writes wait up to 30s on a busy database. The bot writes to the same file the
  whole time, the file is in rollback-journal mode so a writer locks it outright,
  and the live handler wraps its save in a `logger.exception` — a lock it cannot
  acquire drops a real message and leaves only a log line. Switching the DB to
  WAL would remove the contention rather than wait it out, but that is a
  persistent change to the file and should not be a side effect of a backfill.

### What is actually missing

| scope | top-level missing |
|---|---|
| bot's own window, bot messages only | 2355 |
| bot's own window, everything | 2371 |
| all retained history, everything, 23 channels | **21529** of 22982 |

The first two lines are the useful comparison: only **16** human messages are
missing from the window the bot was watching. The live capture has been reliable;
the hole really was bot-shaped. Everything beyond that is history from before the
bot joined each channel.

**`conversations.history` returns top-level messages only**, and 91% of this
table (15132 of 16583 rows) is thread replies, so a run without
`--include-threads` covers a minority of the corpus by design. Full depth means
11463 `conversations.replies` calls at one per thread — **roughly 3.8 hours** at
the Tier-3 rate limit, which is a floor, not an implementation detail: Slack has
no bulk thread endpoint and rate-limits per method, so parallelism buys nothing.

That 91% also explains a shape worth noticing: the case-tracker channel has 433
recorded replies hanging off 2 recorded parents, because the other 1485 parents
were bot posts and got dropped.

## Rollback

Revert the commit and restart. Rows already written stay; nothing reads
`slack_bot_id` yet, so they are inert.
