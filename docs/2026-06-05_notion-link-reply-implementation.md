# Notion Link Reply Implementation Notes

**Date:** 2026-06-05

## Changes
- Added `SlackClient.post_message()` as a best-effort wrapper around `chat_postMessage`.
- Added `notion_link_reply` config validation and documented/live YAML sections.
- Added a gated Slack reply after task creation confirmation, limited to configured channel IDs.
- Added README scope guidance for `chat:write`.
- Added regression tests for configured-channel replies, inert defaults, threading, broadcast handling, best-effort failures, escaping, and config validation.
- Added test-only synchronous shims for threaded async helpers that hang in this sandbox.

## Decisions
- The feature stays inert by default: missing, disabled, or unlisted `notion_link_reply` config posts nothing.
- Reply posting is best-effort and does not affect Notion task creation success.
- No new reply idempotency guard was added because the existing confirmation reaction and DB guards already stop duplicate task creation before the reply block.
- Task titles are escaped for Slack link-label syntax before rendering the reply template.
- Production code still uses `asyncio.to_thread` and `aiosqlite`; the synchronous shims are limited to unit tests so verification can run in the current sandbox.
