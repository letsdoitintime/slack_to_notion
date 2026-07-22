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

## Not done

- **Backfill of missed bot messages.** They are gone from the event stream but
  recoverable via `conversations.history` per channel. Separate job.
- **Repair of the 128 authorless rows.** Their `raw_event` still holds the nested
  `bot_id`, so it is one UPDATE with `json_extract(raw_event, '$.message.bot_id')`
  — but it rewrites prod data, so it needs its own go-ahead.

## Rollback

Revert the commit and restart. Rows already written stay; nothing reads
`slack_bot_id` yet, so they are inert.
