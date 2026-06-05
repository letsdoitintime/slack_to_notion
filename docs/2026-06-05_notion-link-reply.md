# Notion link reply in Slack (configured channels only)

**Date:** 2026-06-05
**Slug:** notion-link-reply
**Loop:** Claude plans → Codex codes → Claude reviews

## Goal
When a configured emoji reaction creates a Notion task, reply in Slack with the new Notion task
link so people can jump straight to it — but **only in channels explicitly listed in `config.yaml`**
(starting with `C05LY9SDVRP`). Today the only feedback is the `comet` confirmation reaction
(`src/processors/task_processor.py:173`); the page URL (`page["url"]`) is already returned by
`create_task()` but never surfaced.

User-confirmed decisions:
- **Threaded reply** under the reacted message (`in_thread: true`, no broadcast).
- **Message text:** `✅ <{notion_url}|{task_title}> · {task_type} · by {reporter_name}`.
- **Best-effort & inert-by-default:** absent/disabled section or unlisted channel → post nothing;
  a failed post must NOT fail task creation. (Inert default also keeps existing `TaskProcessor`
  tests — whose config omits this section — green.)

## Affected files
- `src/slack/client.py` — new `post_message()` helper (client currently only has `add_reaction`).
- `src/processors/task_processor.py` — reply block + `_escape_slack_mrkdwn()` helper; reuse
  `_render_template` from `notion/task_creator.py`.
- `src/utils/config_loader.py` — new `_validate_notion_link_reply()` called from `_validate`.
- `config/config.yaml` (live: enable + `C05LY9SDVRP`) and `config/config.yaml.example` (documented).
- `README.md` — add `chat:write` to the Bot Token Scopes table (lines 29–37).
- `tests/test_task_processor.py`, `tests/test_config_loader.py` — new tests.

## Approach / decisions

### Config (new top-level section, sibling of `confirmation:`)
```yaml
notion_link_reply:
  enabled: true
  channels: ["C05LY9SDVRP"]   # Slack channel IDs, not names; empty/missing ⇒ never fires
  message_template: "✅ <{notion_url}|{task_title}> · {task_type} · by {reporter_name}"
  in_thread: true             # reply inside the reacted message's thread
  broadcast: false            # if in_thread, also surface once in channel (reply_broadcast)
```
Placeholders to expose: `{notion_url} {notion_page_id} {task_title} {channel_name} {channel_id}
{reporter_name} {task_type} {priority} {slack_url} {emoji}`. `{task_title}` is escaped for Slack
link syntax. Gate: fire only when `enabled` is true AND `channel in channels`.

### `SlackClient.post_message(channel, text, thread_ts=None, broadcast=False) -> bool`
- kwargs `{channel, text}`; add `thread_ts` only when truthy; add `reply_broadcast=True` only when
  `broadcast and thread_ts` (Slack rejects `reply_broadcast` without `thread_ts`).
- `chat_postMessage(**kwargs)`; return True on success.
- On `SlackApiError`: log a **warning** (mirror `add_reaction`), surface `exc.response.get("error")`;
  give a clear hint on `missing_scope`/`not_allowed_token_type` (add `chat:write` + reinstall).
  Return False. Best-effort — caller ignores the result.

### `TaskProcessor.process` reply block (after the confirmation reaction at line 173, before `return True`)
- `reply_cfg = self._config.get("notion_link_reply", {})`.
- Gate `if reply_cfg.get("enabled", False) and channel in reply_cfg.get("channels", []):`.
- `notion_url = page.get("url", "")`; skip (debug log) if falsy.
- Context from existing locals; `task_title=_escape_slack_mrkdwn(title)`, `channel_id=channel`,
  `notion_page_id=page.get("id","")`, `task_type=mapping.get("task_type","")`, etc.
- `text = _render_template(reply_cfg.get("message_template", DEFAULT), context)`.
- `anchor = (thread_ts or ts) if reply_cfg.get("in_thread", True) else None` (`thread_ts` from line 87;
  `thread_ts or ts` lands in the parent thread when the reacted message is itself a reply).
- `broadcast = bool(reply_cfg.get("broadcast", False)) and anchor is not None`.
- `await asyncio.to_thread(self._slack.post_message, channel, text, anchor, broadcast)`.
- No new idempotency guard — reaction/DB guards (lines 67–77) already return before creation on
  re-reactions, so this block is unreachable on duplicates (add a one-line comment).
- `_escape_slack_mrkdwn`: `&`→`&amp;`, `<`→`&lt;`, `>`→`&gt;`, `|`→`/`.

### Validation (`_validate_notion_link_reply`, mirror `_validate_allowed_reactors`)
`None`→return; must be mapping; `channels` (if present) list of non-empty strings;
`enabled`/`in_thread`/`broadcast` (if present) bools; `message_template` (if present) str. Do NOT
require `channels` even when enabled.

### Docs
README Bot Token Scopes table: add `| chat:write | Post the Notion task link back to the channel/thread |`.

## Tests
- `tests/test_task_processor.py` (extend `TestTaskProcessorProcess`; `create_task` returns
  `{"id": "p1", "url": "https://notion.so/page/123"}`): posts for configured channel with
  `post_message(channel, text, "111.222", False)` and URL in text; skips when channel unlisted /
  `enabled: false` / section absent (`post_message.assert_not_called()`, `result is True`);
  thread anchor uses parent `thread_ts="999.000"`; broadcast passthrough True, and ignored
  (anchor None, broadcast False) when `in_thread: false`; `post_message` returns False ⇒ process
  still True and `add_reaction` still called once; title with `&`/`<`/`|` is escaped in posted text.
- `tests/test_config_loader.py`: valid section loads; `channels` as string raises; `channels: [""]`
  raises; non-mapping section raises; absent section OK.

## Verify
`python -m pytest -q` (venv python, asyncio_mode=auto). Config sanity:
`python -c "from src.utils.config_loader import load_config; load_config('config/config.yaml')"`.
