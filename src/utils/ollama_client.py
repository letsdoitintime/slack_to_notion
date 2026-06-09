"""Local Ollama LLM client for generating Notion task titles from Slack messages.

A tiny *synchronous* wrapper over Ollama's ``POST /api/generate`` endpoint built
entirely on the standard library — no extra dependencies (this repo has no
``httpx``). Callers run it off the event loop via ``asyncio.to_thread``, exactly
like the Slack and Notion clients.

Title generation is cosmetic: every failure path raises :class:`OllamaError` /
:class:`OllamaUnavailable` so the caller can fall back to the first-line title
without the user ever seeing an error. See
:meth:`~src.processors.task_processor.TaskProcessor._derive_title`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_GENERATE_PATH = "/api/generate"
_PROMPT_CHAR_LIMIT = 500   # only the first ~500 chars are needed to grasp the topic
_TITLE_CHAR_LIMIT = 100    # hard cap on the returned title (matches _extract_title)


class OllamaError(Exception):
    """Ollama returned an error status or an unusable response."""


class OllamaUnavailable(OllamaError):
    """Ollama could not be reached (down, connection refused, or timed out)."""


class OllamaClient:
    """Generates a short task title from message text using a local Ollama model.

    The model is unloaded immediately after each request (``keep_alive: 0``) to
    avoid holding RAM between the bot's infrequent, bursty calls.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:3b",
        timeout: float = 15.0,
        num_thread: int = 6,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._num_thread = num_thread

    def generate_title(self, text: str, language_hint: str | None = None) -> str:
        """Return an AI-generated task title for *text*.

        Raises :class:`OllamaUnavailable` when Ollama can't be reached and
        :class:`OllamaError` on a bad HTTP status or empty/invalid response. The
        caller is expected to catch these and fall back to a non-AI title.
        """
        body: dict = {
            "model": self._model,
            "prompt": self._build_prompt(text, language_hint),
            "stream": False,
            "keep_alive": 0,   # unload right after — local, bursty usage
        }
        if self._num_thread > 0:
            body["options"] = {"num_thread": self._num_thread}

        request = urllib.request.Request(
            self._base_url + _GENERATE_PATH,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:   # 4xx / 5xx — subclass of URLError
            raise OllamaError(f"HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaUnavailable(str(exc)) from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaError(f"unreadable response from Ollama: {exc}") from exc

        if not isinstance(payload, dict):
            raise OllamaError(f"unexpected response type: {type(payload).__name__}")
        raw = payload.get("response")
        if not isinstance(raw, str):
            raise OllamaError("missing or non-string 'response' field")
        # Collapse all whitespace (incl. newlines) to single spaces so a multi-line
        # model reply becomes a single-line title before it ever reaches Notion.
        title = " ".join(raw.split()).strip('"').strip("'").strip()
        if not title:
            raise OllamaError("empty response")
        return title[:_TITLE_CHAR_LIMIT]

    @staticmethod
    def _build_prompt(text: str, language_hint: str | None) -> str:
        lang = (
            f"Respond in {language_hint}."
            if language_hint
            else "Respond in the same language as the message."
        )
        return (
            "Generate a short task title (5-15 words) for this Slack message.\n"
            "Reply with ONLY the title — no quotes, no trailing punctuation, "
            "no explanation.\n"
            f"{lang}\n\nMessage:\n{text[:_PROMPT_CHAR_LIMIT]}"
        )


def build_ollama_client(config: dict) -> OllamaClient | None:
    """Construct an :class:`OllamaClient` from the ``ollama:`` config section.

    Returns ``None`` when the section is absent or ``enabled`` is false, in which
    case the processor uses the first-line title without any LLM call.
    """
    cfg = config.get("ollama", {}) or {}
    if not cfg.get("enabled", False):
        return None
    client = OllamaClient(
        base_url=cfg.get("base_url") or "http://127.0.0.1:11434",
        model=cfg.get("model") or "qwen2.5:3b",
        timeout=float(cfg.get("timeout_s") or 15),
        num_thread=int(cfg.get("num_thread") or 6),
    )
    logger.info("Ollama title generation enabled (model=%s).", client._model)
    return client
