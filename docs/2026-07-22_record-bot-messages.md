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

Full suite: 320 passed, 4 skipped.

## Recovering what was already lost

Two scripts, both dry-run by default. One-shots — new rows are correct at write
time — but safe to re-run.

### `scripts/repair_bot_authorship.py`

Lifts `bot_id` and the display name out of `raw_event` into their columns for the
orphan `message_changed` rows. Nothing was ever lost, only unread. 130 rows
across 9 bots (`SUP - Payment Case Closed` 55, `SUP - Opened Cases List` 53,
`CB RF flow` 10, then a tail of one-offs).

### `scripts/backfill_bot_messages.py`

Re-reads `conversations.history` and inserts the bot messages that were dropped.
Bot messages only: human ones are already there, and re-inserting them here would
land rows with no resolved user name — worse than what is already stored. Writes
go through `DatabaseManager.save_message`, so the write path, the
`INSERT OR IGNORE` de-duplication and the derived columns are the live ones. Each
row is stamped `"_backfilled": true` inside `raw_event`, because the envelope is
reconstructed from a history message rather than received from Slack.

Two guards worth knowing about:

- It **aborts** if `save_message` still skips bot messages. Run from a checkout
  predating this fix it would otherwise walk every channel, find plenty, and
  write nothing — a silent no-op that reads as a clean run.
- Each channel is walked back only as far as its own earliest row, i.e. the
  window this bot was actually watching.

Measured over the 22 channels in the table: **2355 top-level bot messages are
missing**, concentrated — three channels hold 2300 of them.

**`conversations.history` returns top-level messages only**, and 91% of this
table (15132 of 16583 rows) is thread replies, so the default pass sees a
minority of the corpus by design. `--include-threads` walks
`conversations.replies` for each of the 1575 parents that have replies, one API
call each — roughly 30 minutes at the Tier-3 rate limit.

That 91% also explains a shape worth noticing: the case-tracker channel has 433
recorded replies hanging off 2 recorded parents, because the other 1485 parents
were bot posts and got dropped.

## Rollback

Revert the commit and restart. Rows already written stay; nothing reads
`slack_bot_id` yet, so they are inert.
