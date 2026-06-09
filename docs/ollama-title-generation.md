# Ollama-powered title generation — design & implementation reference

This document captures how local-LLM title generation was added to the **SlackToNotion**
bot, and why each decision was made. It is the SlackToNotion counterpart to the original
Telegram Whisper write-up, adapted to this codebase's architecture (synchronous I/O
clients driven from an async handler, fully config-driven, no `Settings` dataclass).

---

## 1. What was built

When a configured emoji reaction creates a Notion task, the bot needs a short title for
the page. Previously it always used the first ~100 characters of the Slack message
(`_extract_title` — strips Slack mrkdwn, truncates at a word boundary). Fine for tidy
messages, poor for long or rambling ones.

The new behaviour, when enabled:

1. Before building `TaskData`, ask a local Ollama model for a 5–15 word title.
2. If Ollama answers within the timeout → use the AI title.
3. If anything goes wrong (service down, timeout, empty/invalid reply, HTTP error) →
   **fall back silently** to the original first-line title. The task is always created;
   the user never sees an error.

It is **off by default** (`enabled: false`). Existing deployments are unaffected until
the `ollama` section is turned on.

---

## 2. How it differs from the Telegram Whisper port

The Telegram bot is natively async and uses `httpx` with aiogram dependency-injection and
a `Settings` dataclass populated from environment variables. SlackToNotion is different in
three ways, so the port is adapted rather than copied:

| Concern | Telegram Whisper | SlackToNotion |
|---|---|---|
| HTTP | `httpx.AsyncClient` (async) | stdlib `urllib`, **synchronous**, run via `asyncio.to_thread` |
| Dependency injection | aiogram dispatcher service-locator | constructor injection into `TaskProcessor` |
| Configuration | `Settings` dataclass + env vars | `ollama:` section in `config.yaml` |
| Title-language scope | per-user (`config/notion.yaml`) | global (`ollama.title_language`) |
| Lifecycle teardown | `aclose()` on a pooled async client | none — each call is a short-lived stdlib request |

The reason for a **synchronous** client: every other I/O client in this repo
(`SlackClient`, `NotionClient`) is synchronous and called with
`await asyncio.to_thread(...)`. Matching that keeps the change idiomatic and adds
**zero new dependencies** — this repo has no `httpx`, and a stdlib `urllib` call needs
no connection-pool lifecycle to manage.

---

## 3. Key design decisions

### 3.1 No new dependencies

The client is built on `urllib.request` / `json` from the standard library. Ollama's REST
API is a single `POST /api/generate` call, so no SDK is needed and `requirements.txt` is
unchanged.

### 3.2 Separate `OllamaClient` module

The client lives in [`src/utils/ollama_client.py`](../src/utils/ollama_client.py), alongside
the other cross-cutting helpers (`user_mapper`, `field_extractor`, `due_date_parser`). It
knows only about HTTP + the Ollama API; the processor knows nothing about `urllib`.

### 3.3 Constructor injection, built from config

`TaskProcessor.__init__` gained an optional `ollama: OllamaClient | None = None` parameter.
`main.py` builds the client once and passes it in:

```python
ollama = build_ollama_client(config)   # None when the section is disabled/absent
...
processors[name] = TaskProcessor(..., db=db, ollama=ollama)
```

`build_ollama_client` returns `None` when the `ollama` section is missing or
`enabled: false`, so `TaskProcessor` treats "disabled" and "not configured" identically.
The default `ollama=None` also keeps every existing caller and test working unchanged.

### 3.4 Graceful degradation — the single fallback point

All fallback logic lives in one method,
[`TaskProcessor._derive_title`](../src/processors/task_processor.py):

```python
async def _derive_title(self, message_text: str) -> str:
    fallback = _extract_title(message_text)
    if self._ollama is None:
        return fallback
    cleaned = _clean_slack_text(message_text)
    if not cleaned:
        return fallback
    try:
        title = await asyncio.to_thread(
            self._ollama.generate_title, cleaned, self._ollama_title_language
        )
    except Exception:
        logger.debug("Ollama title generation failed — using first-line fallback.",
                     exc_info=True)
        return fallback
    return title.strip() or fallback
```

`except Exception` is intentional: a title is cosmetic and must never stop a task from
being created. Note the call passes **cleaned** text (Slack `<@U…|name>`, `<url|label>`
etc. stripped) to the model, while the fallback uses the same `_extract_title` path as
before.

### 3.5 `keep_alive: 0` — unload the model after each request

By default Ollama keeps a model resident in RAM for 5 minutes after a request. For this
bot's infrequent, bursty usage that wastes ~2–5 GB between tasks. Sending
`"keep_alive": 0` unloads it immediately. Trade-off: each call pays a cold-start cost
(~2–6 s for a 3B model on CPU), invisible because it runs asynchronously during task
creation.

### 3.6 `num_thread` — cap CPU usage

Ollama defaults to all cores. `options.num_thread` caps it (default `6`); `0` omits the
option entirely and lets Ollama decide.

### 3.7 Prompt and output limits

- Only the first **500 chars** of the (cleaned) message are sent — enough to grasp the
  topic without wasting tokens.
- The returned title is stripped of surrounding quotes/whitespace and **capped at 100
  chars**, matching `_extract_title`'s cap.

### 3.8 Title language

`ollama.title_language` (e.g. `"en"`) forces titles into a language regardless of the
message language; blank means "match the message". This is global here (one bot, one
shared workspace), unlike the per-user setting in the Telegram bot.

---

## 4. Ollama API reference

One endpoint is used.

### `POST /api/generate`

**Request body** (as built by `OllamaClient.generate_title`):

```json
{
  "model": "qwen2.5:3b",
  "prompt": "...",
  "stream": false,
  "keep_alive": 0,
  "options": { "num_thread": 6 }
}
```

| Field | Notes |
|---|---|
| `model` | Name as shown by `ollama list`. |
| `prompt` | The full prompt (see §5). |
| `stream` | `false` → one JSON response. |
| `keep_alive` | `0` = unload immediately. |
| `options.num_thread` | Omitted when `num_thread` is `0`. |

**Response (200):** the title is in the `response` field.

**Error handling:**

| Situation | Python exception | Mapped to |
|---|---|---|
| Ollama down / connection refused | `urllib.error.URLError` | `OllamaUnavailable` |
| Request times out | `TimeoutError` (socket timeout) / `OSError` | `OllamaUnavailable` |
| HTTP 4xx/5xx | `urllib.error.HTTPError` | `OllamaError` |
| Invalid JSON body | `json.JSONDecodeError` | `OllamaError` |
| Empty `response` field | — | `OllamaError` |

`OllamaUnavailable` subclasses `OllamaError`, so callers can catch the broad case while
still distinguishing "down" from "bad response" if they need to. `_derive_title` catches
the broadest `Exception` and falls back regardless.

---

## 5. The prompt

```
Generate a short task title (5-15 words) for this Slack message.
Reply with ONLY the title — no quotes, no trailing punctuation, no explanation.
Respond in {language_hint OR "the same language as the message"}.

Message:
{cleaned_text[:500]}
```

- **5–15 words:** meaningful but short enough for a Notion title.
- **"ONLY the title":** small models otherwise add "Here is the title:" or wrap in quotes;
  the explicit instruction plus a defensive `.strip('"')` removes both.
- **`text[:500]`:** the first ~500 chars are enough to understand the topic.

---

## 6. Files changed

| File | Change |
|---|---|
| [`src/utils/ollama_client.py`](../src/utils/ollama_client.py) | **New** — `OllamaClient`, `OllamaError`, `OllamaUnavailable`, `build_ollama_client`. |
| [`src/processors/task_processor.py`](../src/processors/task_processor.py) | `TaskProcessor` gets an `ollama` param; new `_derive_title`; line 140 now calls it. |
| [`src/main.py`](../src/main.py) | Builds the client via `build_ollama_client(config)` and injects it. |
| [`src/utils/config_loader.py`](../src/utils/config_loader.py) | New `_validate_ollama` for the optional section. |
| [`config/config.yaml.example`](../config/config.yaml.example) | Documented `ollama:` section. |
| [`tests/test_ollama_client.py`](../tests/test_ollama_client.py) | Client, builder, fallback, and live integration tests. |
| [`tests/test_config_loader.py`](../tests/test_config_loader.py) | `ollama` section validation tests. |

---

## 7. Configuration reference

```yaml
ollama:
  enabled: false                      # master switch; false/absent → first-line titles
  base_url: http://127.0.0.1:11434
  model: qwen2.5:3b                   # must match `ollama list`
  timeout_s: 15                       # fall back after this many seconds
  num_thread: 6                       # CPU threads; 0 = let Ollama decide
  title_language:                     # e.g. "en"; blank = match the message language
```

`base_url`/`model` accept `${ENV_VAR}` placeholders (resolved by `config_loader`); keep
`timeout_s`/`num_thread` as numeric literals.

---

## 8. Testing

### Confirm Ollama is up and the model is pulled

```bash
curl -s http://127.0.0.1:11434/api/tags | python3 -m json.tool   # lists models
ollama list
```

### Manually call the API

```bash
curl -s http://127.0.0.1:11434/api/generate \
  -d '{"model":"qwen2.5:3b","prompt":"Generate a short task title (5-15 words).\nReply with ONLY the title.\n\nMessage:\nThe checkout page 500s when the coupon field is empty.","stream":false,"keep_alive":0}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

### Run the unit + integration tests

```bash
python3 -m pytest tests/test_ollama_client.py tests/test_config_loader.py -v
```

The live integration tests in `tests/test_ollama_client.py` (`TestLiveOllama`) **auto-skip**
when Ollama is not reachable on `127.0.0.1:11434`, so the suite is green with or without a
running Ollama. When it is reachable, they call the real service end-to-end using whichever
model `ollama list` reports.

### Verify the fallback by hand

```bash
ollama stop                  # or: systemctl stop ollama
# React to a Slack message → task is still created, title = first line of the message.
ollama serve &               # bring it back; subsequent titles are AI-generated again.
```

---

## 9. Porting checklist (to yet another app)

1. Copy `ollama_client.py` (stdlib-only; no edits needed for the HTTP part).
2. Decide where the title is produced today and wrap that one call:
   `title = ai_title if ollama else first_line_title`, with `except Exception` → fallback.
3. Expose config: `enabled`, `base_url`, `model`, `timeout_s`, `num_thread`,
   `title_language`.
4. Tune the prompt wording / word-count / `text[:N]` truncation for your domain.
5. Keep the fallback `except Exception` — titles are never worth failing a task over.
