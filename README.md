# Slack → Notion Task Bot

Reacts to Slack emoji reactions and automatically creates tasks in a Notion database.

## How it works

1. Someone adds a configured emoji reaction (e.g. 👀 `:eyes:`) to any Slack message
2. The bot fetches the message content, extracts all relevant fields, and creates a Notion task
3. A ✅ confirmation reaction is added back to the Slack message

Supports **multiple emojis → multiple Notion databases**, thread replies, due-date parsing,
user mapping, and is driven entirely by a YAML config file — no code changes needed for
common customisations.

---

## Quick start

### 1. Create a Slack App

Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.

**Socket Mode** (Settings → Socket Mode → Enable):
- This lets the bot run locally without a public URL.
- Generate an **App-Level Token** with the `connections:write` scope — this becomes `SLACK_APP_TOKEN`.

**OAuth & Permissions** → Bot Token Scopes (add all of these):

| Scope | Purpose |
|---|---|
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels |
| `reactions:read` | Read reactions on messages |
| `reactions:write` | Add the ✅ confirmation reaction |
| `chat:write` | Post the Notion task link back to the channel/thread |
| `users:read` | Resolve user display names |
| `channels:read` | Resolve channel names |
| `groups:read` | Resolve private channel names |

Install the app to your workspace → copy the **Bot User OAuth Token** → `SLACK_BOT_TOKEN`.

**Event Subscriptions** (only needed if not using Socket Mode):
- Subscribe to the `reaction_added` bot event.

With Socket Mode active, the **Events API URL is not required**.

**Add the bot to channels**: `/invite @YourBotName` in each channel you want it to monitor.

### 2. Create a Notion Integration

Go to <https://www.notion.so/my-integrations> → New integration → copy the **Internal Integration Token** → `NOTION_TOKEN`.

**Share your Notion database(s)** with the integration:
- Open the database → `···` menu → **Connections** → add your integration.

**Copy the database ID** from the URL:
```
https://www.notion.so/workspace/THIS-IS-THE-DATABASE-ID?v=...
```

### 3. Configure the bot

```bash
cp .env.example .env
# Edit .env and fill in your tokens and database IDs
```

Edit `config/config.yaml`:
- Map emojis to Notion databases under `emoji_mappings`
- Set `fields.notion_fields` to match your Notion database property names exactly
- Add Slack → Notion user mappings under `users` (optional, for assignee field)

### 4. Install dependencies & run

```bash
pip install -r requirements.txt
python3 -m src.main
```

---

## Configuration reference

### `emoji_mappings`

Each entry triggers an action when its emoji is added to a message.

```yaml
emoji_mappings:
  - emoji: "eyes"               # Emoji name without colons
    notion_db: ${NOTION_DB_REVIEW}  # Target Notion database ID (env var or literal)
    task_type: "Review"         # Value for the Type property
    priority: "Medium"          # Value for the Priority property
    processor: "TaskProcessor"  # Which processor class to use
```

### `fields.notion_fields`

Maps the bot's internal field names to your Notion database property names.

| Internal field | What it contains | Recommended Notion type |
|---|---|---|
| `title` | AI-generated summary (if [Ollama](#ai-title-generation-ollama) is enabled) or the first ~100 chars of the message | **Title** |
| `status` | Default status from config | Status / Select |
| `priority` | Priority from `emoji_mappings` | Select |
| `task_type` | Task type from `emoji_mappings` | Select |
| `slack_url` | Deep link to the Slack message | URL |
| `reporter` | Display name of who reacted | Rich text |
| `assignee` | Notion user ID of message author (from `users` map) | People |
| `due_date` | First date found in message text | Date |
| `channel` | Source Slack channel name | Rich text / Select |

Remove any properties you don't want from your config — they will be skipped.

### `users`

Maps Slack user IDs to Notion user UUIDs for the Assignee field.

```yaml
users:
  U01SLACKUSERID: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Find Notion user IDs: `GET https://api.notion.com/v1/users` with your integration token.

---

## AI title generation (Ollama)

By default the task title is the first ~100 characters of the Slack message (mrkdwn
stripped, truncated at a word boundary). This is fine for tidy messages but poor for
long or rambling ones.

When the optional **`ollama`** section is enabled, the bot instead asks a local
[Ollama](https://ollama.com) model for a short 5–15 word title that summarises the
message — e.g. a message reading *"Hey team, the checkout page throws a 500 when the
coupon field is empty, we need to validate it server-side before Friday's release"*
becomes **"Validate and sanitize coupon input before Friday release"**.

### Graceful fallback — Ollama is never required

Title generation is **cosmetic and best-effort**. If anything goes wrong — Ollama not
running, model not pulled, request times out, empty/invalid response — the bot
**silently falls back to the original first-line title**. The task is always created
and the user never sees an error. Concretely, the fallback triggers when:

- the `ollama` section is absent or `enabled: false` (no LLM call at all);
- the message is empty after stripping Slack formatting;
- the Ollama service is unreachable (`OllamaUnavailable`);
- Ollama returns a non-200 status or an empty/invalid body (`OllamaError`).

This means you can enable it on a server with Ollama and leave it disabled (or simply
not install Ollama) everywhere else — the same code runs in both.

### Setup

1. Install Ollama and pull a model (a small one is plenty for titles):

   ```bash
   # https://ollama.com/download
   ollama pull qwen2.5:3b
   ollama list          # confirm the model name
   ```

2. Enable the section in `config/config.yaml`:

   ```yaml
   ollama:
     enabled: true
     base_url: http://127.0.0.1:11434
     model: qwen2.5:3b          # must match a name from `ollama list`
     timeout_s: 15              # fall back to the first-line title after this many seconds
     num_thread: 6              # CPU threads for inference; 0 = let Ollama decide
     title_language:            # optional, e.g. "en"; blank = match the message language
   ```

   Omit the section entirely (or set `enabled: false`) to keep first-line titles.

The generated title still flows through `task_title_template`, so per-emoji/global
wrappers like `"[Slack] {task_title} — {channel_name}"` continue to apply.

### Configuration reference

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch. `false`/absent → first-line titles, no LLM call. |
| `base_url` | `http://127.0.0.1:11434` | Ollama service address. |
| `model` | `qwen2.5:3b` | Model name; must match `ollama list`. |
| `timeout_s` | `15` | Seconds before the request is abandoned and the fallback used. |
| `num_thread` | `6` | CPU threads for inference; `0` = let Ollama decide. |
| `title_language` | _(blank)_ | Force titles into a language (e.g. `en`); blank = match the message. |

### Design notes

- **No new dependencies.** The client ([`src/utils/ollama_client.py`](src/utils/ollama_client.py))
  is a thin synchronous wrapper over Ollama's `POST /api/generate`, built on the
  standard library and run off the event loop via `asyncio.to_thread` — the same way
  the Slack and Notion clients are called.
- **`keep_alive: 0`** unloads the model from RAM immediately after each request — good
  for the bot's infrequent, bursty usage. Trade-off: each call pays a cold-start cost
  (~2–6 s for a 3B model on CPU), which is invisible because it runs asynchronously
  during task creation.
- **Only the first 500 chars** of the message are sent to the model (enough to grasp
  the topic), and the returned title is capped at 100 chars.
- `base_url`/`model` can be supplied via `${ENV_VAR}` placeholders like any other
  config string; keep `timeout_s`/`num_thread` as numeric literals.

See [`docs/ollama-title-generation.md`](docs/ollama-title-generation.md) for the full
design and porting reference.

---

## Extending with new processors

1. Create `src/processors/my_processor.py` inheriting `BaseProcessor`:

```python
from .base import BaseProcessor

class MyProcessor(BaseProcessor):
    def process(self, event: dict, mapping: dict) -> bool:
        # your custom logic here
        return True
```

2. Register it in `src/processors/__init__.py`:

```python
from .my_processor import MyProcessor

PROCESSOR_REGISTRY = {
    "TaskProcessor": TaskProcessor,
    "MyProcessor": MyProcessor,   # add this
}
```

3. Reference it in `config.yaml`:

```yaml
emoji_mappings:
  - emoji: "calendar"
    processor: "MyProcessor"
    notion_db: ${NOTION_DB_CALENDAR}
```

---

## Running tests

```bash
python3 -m pytest tests/ -v
```

---

## Project structure

```
SlackToNotion/
├── config/
│   └── config.yaml          ← all behaviour configured here
├── src/
│   ├── main.py              ← entry point
│   ├── slack/
│   │   ├── client.py        ← Slack API wrapper
│   │   └── event_handler.py ← reaction_added handler
│   ├── notion/
│   │   ├── client.py        ← Notion API wrapper
│   │   └── task_creator.py  ← builds and posts Notion pages
│   ├── processors/
│   │   ├── base.py          ← abstract BaseProcessor
│   │   └── task_processor.py← default task creation processor
│   └── utils/
│       ├── config_loader.py ← YAML + env var resolution
│       ├── due_date_parser.py← NLP date extraction
│       ├── ollama_client.py ← optional local-LLM title generation
│       └── user_mapper.py   ← Slack ↔ Notion user mapping
└── tests/
```
