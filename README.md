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
- Set `fields.notion_properties` to match your Notion database property names exactly
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

### `fields.notion_properties`

Maps the bot's internal field names to your Notion database property names.

| Internal field | What it contains | Recommended Notion type |
|---|---|---|
| `title` | First 100 chars of the Slack message | **Title** |
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
│       └── user_mapper.py   ← Slack ↔ Notion user mapping
└── tests/
```
