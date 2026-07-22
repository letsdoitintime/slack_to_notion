# 2026-07-22 — Reaction reminders

Adds an optional nudge: when a trigger emoji lands on a message in a watched channel,
schedule one or more reminders. Each one, when it fires, re-computes who in the channel
has **not** reacted (with any emoji) and posts an in-thread message @mentioning them.

Also adds `reaction_date` as a usable Notion field source, and ignores machine-local
agent state.

## Shape

| piece | file |
|---|---|
| scheduling + firing loop | `src/slack/reminders.py` (new) |
| persistence (schema v2, v3) | `src/db/database.py` |
| Slack calls (`get_channel_members`, `get_reactors`, `is_human`) | `src/slack/client.py` |
| handler wiring | `src/slack/event_handler.py` |
| loop start/cancel | `src/main.py` |
| config validation | `src/utils/config_loader.py` |
| `reaction_date` field source | `src/processors/task_processor.py` |

Omit the `reaction_reminders` config section entirely and the feature is off — the loop
logs once and returns without starting.

## ⚠️ Contains a DB migration

Two, applied automatically on startup by the existing migration runner:

- **v2** — creates `reaction_reminders`, with `UNIQUE(slack_channel, slack_ts,
  trigger_emoji, after_minutes)` so re-adding the same trigger emoji cannot duplicate a
  message's reminders.
- **v3** — adds `attempts INTEGER NOT NULL DEFAULT 0` for bounded retry.

Both are additive; no existing table or column is touched, so a rollback to the previous
code leaves the new table sitting unused rather than breaking anything.

## Design points worth knowing

- **Reminders are open to anyone, and scoped by channel — deliberately.** The per-rule
  `channels` list is the scope control: the nudge works in the selected channels and
  nowhere else, but within them any member may trigger it. `allowed_reactors` does **not**
  apply — it restricts who can create Notion tasks, a much heavier action than asking
  teammates to react.

  Review flagged this as an authorization gap and it was kept on purpose. Because the
  reactor is unrestricted, the channel list is the *only* thing keeping the nudge out of
  channels it was never configured for, so it is covered directly: an unlisted channel
  schedules nothing, and an empty `channels` list fails closed rather than matching
  everywhere.
- **One listener, not two.** Bolt runs only the *first* matching `reaction_added`
  listener, so reminder scheduling lives inside the existing emoji handler and runs
  *before* the `emoji_mappings` early-return — the trigger emoji is usually not a
  processor emoji. This was a real regression once; it is now pinned by a test against
  real Bolt in `tests/test_slack_transport_contract.py` (PR #5).
- **Non-reactors are computed at send time, not schedule time.** Anyone who reacts
  during the waiting period drops off the list.
- **Only real people are pinged.** The poster, the bot, other bots/apps and deactivated
  accounts are excluded. Classification runs over the non-reactor candidates only, not
  every channel member.
- **Bounded retry.** A failed Slack post leaves the row unsent so the next poll retries
  it, with `attempts` incremented; after `_MAX_ATTEMPTS` (5) it is marked sent and given
  up on, so a permanently broken reminder cannot loop forever.
- **Survives restarts.** Reminders live in SQLite, and the loop picks up anything due on
  the next poll (~60s).
- **`after_minutes` accepts numeric strings.** `${ENV_VAR}` placeholders resolve to
  strings, so `after_minutes: ${REMINDER_DELAY}` arrives as `"60"`. Rejecting that would
  fail at startup on a value `schedule_for_event` handles fine, and would break the
  env-based config pattern used everywhere else here. Booleans are still rejected
  explicitly — `True` is an `int` in Python and would otherwise pass as 1 minute.
- **`reaction_date` is filled in, not defaulted.** `extract_fields` pre-seeds `""` for
  every configured `body_field` without an `extract_pattern`, so a `setdefault` left the
  blank in place and the value came out empty in exactly the configuration that asks for
  the field. Only a non-empty extracted value overrides it.

## Tests

`tests/test_reminders.py` — 24 tests: the non-reactor diff, rule matching, scheduling
idempotency, the retry/give-up ladder, config validation, handler wiring, and the wire
contract for the two Slack endpoints this feature adds.

The wire tests matter more than they look: `get_channel_members` and `get_reactors`
decide *who gets pinged*. A renamed parameter yields an empty reactor set, and an empty
reactor set means **everyone in the channel gets @mentioned** — loud in Slack, silent in
a normal test suite. Both requests are asserted directly, captured at slack_sdk's send
point so nothing touches the network.

Full suite: 219 passed, 4 skipped.

## Required Slack scopes

`channels:read` (public) or `groups:read` (private), `users:read`, `reactions:read`,
`chat:write`. The bot must be a member of each watched channel.

## Rollback

Revert the commit and restart. The `reaction_reminders` table stays behind, unused and
harmless. To stop the feature without a code change, delete the `reaction_reminders`
section from `config/config.yaml` and restart.
