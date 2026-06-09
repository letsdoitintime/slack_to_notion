"""Tests for the Ollama title-generation client and TaskProcessor's fallback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.processors.task_processor import TaskProcessor, _extract_title
from src.utils.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaUnavailable,
    build_ollama_client,
)
from src.utils.user_mapper import UserMapper

_OLLAMA_URL = "http://127.0.0.1:11434"


def _reachable_models() -> list[str]:
    """Return the model names a live Ollama reports, or [] if it's unreachable.

    Used to auto-skip the live integration tests so the suite stays green whether
    or not Ollama is running.
    """
    try:
        with urllib.request.urlopen(f"{_OLLAMA_URL}/api/tags", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


# Live-integration tests are opt-in: set OLLAMA_TESTS=1 to enable.
# Without the flag the probe is never made, keeping CI runs instant and side-effect-free.
_LIVE_MODELS = _reachable_models() if os.environ.get("OLLAMA_TESTS") else []
_PREFERRED_MODEL = next(
    (m for m in _LIVE_MODELS if m.startswith("qwen2.5:")),
    _LIVE_MODELS[0] if _LIVE_MODELS else "",
)


@contextmanager
def _fake_response(payload: dict):
    """Mimic the context manager returned by urllib.request.urlopen."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    yield resp


# ── OllamaClient.generate_title ───────────────────────────────────────────────

class TestGenerateTitle:
    def test_returns_stripped_response(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": "  Review the Q3 budget  "}),
        ):
            assert OllamaClient().generate_title("blah") == "Review the Q3 budget"

    def test_strips_surrounding_quotes(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": '"Call the dentist"'}),
        ):
            assert OllamaClient().generate_title("blah") == "Call the dentist"

    def test_caps_title_length(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": "x" * 200}),
        ):
            assert len(OllamaClient().generate_title("blah")) == 100

    def test_empty_response_raises_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": "   "}),
        ):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_connection_error_raises_unavailable(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(OllamaUnavailable):
                OllamaClient().generate_title("blah")

    def test_timeout_raises_unavailable(self) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(OllamaUnavailable):
                OllamaClient().generate_title("blah")

    def test_http_error_raises_error(self) -> None:
        err = urllib.error.HTTPError(
            url="http://x", code=500, msg="Server Error", hdrs=None, fp=None
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_request_body_includes_keep_alive_and_num_thread(self) -> None:
        captured: dict = {}

        def _fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _fake_response({"response": "ok title"})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            OllamaClient(num_thread=4, timeout=9.0).generate_title("hello world")

        assert captured["body"]["keep_alive"] == 0
        assert captured["body"]["stream"] is False
        assert captured["body"]["options"] == {"num_thread": 4}
        assert captured["timeout"] == 9.0

    def test_num_thread_zero_omits_options(self) -> None:
        captured: dict = {}

        def _fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _fake_response({"response": "ok title"})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            OllamaClient(num_thread=0).generate_title("hello")

        assert "options" not in captured["body"]

    def test_prompt_truncates_long_message(self) -> None:
        captured: dict = {}

        def _fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _fake_response({"response": "ok"})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            OllamaClient().generate_title("z" * 5000)

        # Only the first 500 chars of the message reach the model.
        assert captured["body"]["prompt"].count("z") == 500

    def test_collapses_multiline_response_to_single_line(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": "Fix checkout 500\n\nand add a test"}),
        ):
            title = OllamaClient().generate_title("blah")
        assert title == "Fix checkout 500 and add a test"
        assert "\n" not in title

    def test_null_response_raises_error(self) -> None:
        # A `{"response": null}` body must raise (not return the literal "None").
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"response": None}),
        ):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_missing_response_field_raises_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response({"done": True}),
        ):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_non_dict_json_raises_error(self) -> None:
        # A syntactically-valid but non-object body (e.g. a JSON array) must map to
        # OllamaError, honouring the documented "every failure raises OllamaError" contract.
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response(["not", "a", "dict"]),
        ):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_malformed_json_raises_error(self) -> None:
        @contextmanager
        def _bad_body():
            resp = MagicMock()
            resp.read.return_value = b"this is not json"
            yield resp

        with patch("urllib.request.urlopen", return_value=_bad_body()):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")

    def test_non_utf8_response_raises_error(self) -> None:
        @contextmanager
        def _non_utf8():
            resp = MagicMock()
            resp.read.return_value = b"\xff\xfe bad bytes"
            yield resp

        with patch("urllib.request.urlopen", return_value=_non_utf8()):
            with pytest.raises(OllamaError):
                OllamaClient().generate_title("blah")


# ── build_ollama_client ───────────────────────────────────────────────────────

class TestBuildOllamaClient:
    def test_returns_none_when_section_absent(self) -> None:
        assert build_ollama_client({}) is None

    def test_returns_none_when_disabled(self) -> None:
        assert build_ollama_client({"ollama": {"enabled": False}}) is None

    def test_builds_client_when_enabled(self) -> None:
        client = build_ollama_client(
            {"ollama": {"enabled": True, "model": "phi4-mini", "num_thread": 2}}
        )
        assert isinstance(client, OllamaClient)
        assert client._model == "phi4-mini"
        assert client._num_thread == 2

    def test_maps_all_config_keys(self) -> None:
        # Guards against a key-name typo silently falling back to a default.
        client = build_ollama_client(
            {
                "ollama": {
                    "enabled": True,
                    "base_url": "http://ollama.internal:1234",
                    "model": "qwen2.5:3b",
                    "timeout_s": 20,
                    "num_thread": 3,
                }
            }
        )
        assert client._base_url == "http://ollama.internal:1234"
        assert client._timeout == 20.0
        assert client._num_thread == 3

    def test_coerces_string_numbers(self) -> None:
        # YAML values arriving as strings (e.g. via ${ENV_VAR}) are coerced.
        client = build_ollama_client(
            {"ollama": {"enabled": True, "timeout_s": "12", "num_thread": "4"}}
        )
        assert client._timeout == 12.0
        assert client._num_thread == 4

    def test_uses_documented_defaults(self) -> None:
        client = build_ollama_client({"ollama": {"enabled": True}})
        assert client._base_url == "http://127.0.0.1:11434"
        assert client._model == "qwen2.5:3b"
        assert client._timeout == 15.0
        assert client._num_thread == 6

    def test_returns_none_for_null_section(self) -> None:
        # A bare `ollama:` key in YAML parses to None — must be treated as "off".
        assert build_ollama_client({"ollama": None}) is None

    def test_null_scalars_use_defaults(self) -> None:
        # `timeout_s:` / `num_thread:` left blank in YAML parse to None.
        # build_ollama_client must not raise TypeError on float(None)/int(None).
        client = build_ollama_client({"ollama": {"enabled": True, "timeout_s": None, "num_thread": None}})
        assert client._timeout == 15.0
        assert client._num_thread == 6

    def test_zero_num_thread_preserved_not_replaced_by_default(self) -> None:
        # num_thread: 0 means "let Ollama decide" — must not be replaced by default 6.
        client = build_ollama_client({"ollama": {"enabled": True, "num_thread": 0}})
        assert client._num_thread == 0

    def test_zero_timeout_s_preserved_not_replaced_by_default(self) -> None:
        client = build_ollama_client({"ollama": {"enabled": True, "timeout_s": 0}})
        assert client._timeout == 0.0


# ── TaskProcessor._derive_title (graceful fallback) ───────────────────────────

class TestDeriveTitle:
    def _processor(
        self, ollama: object, config: dict | None = None
    ) -> TaskProcessor:
        return TaskProcessor(
            slack=MagicMock(),
            task_creator=MagicMock(),
            user_mapper=UserMapper({}),
            config=config or {},
            ollama=ollama,  # type: ignore[arg-type]
        )

    async def test_falls_back_when_ollama_disabled(self) -> None:
        proc = self._processor(ollama=None)
        assert await proc._derive_title("Fix the login bug") == "Fix the login bug"

    async def test_uses_ollama_title_with_cleaned_text(self) -> None:
        ollama = MagicMock()
        ollama.generate_title.return_value = "AI generated title"
        proc = self._processor(ollama=ollama)

        title = await proc._derive_title("rambling message <@U1|bob> please")

        assert title == "AI generated title"
        passed_text = ollama.generate_title.call_args[0][0]
        assert "<@U1|bob>" not in passed_text  # Slack mrkdwn stripped before LLM
        assert "@bob" in passed_text
        # With no title_language configured, None is forwarded (match message language).
        assert ollama.generate_title.call_args[0][1] is None

    def test_construct_with_null_ollama_config_does_not_crash(self) -> None:
        # A bare `ollama:` key parses to None; constructing the processor (feature
        # off) must not raise — title_language resolves to None.
        proc = TaskProcessor(
            slack=MagicMock(),
            task_creator=MagicMock(),
            user_mapper=UserMapper({}),
            config={"ollama": None},
            ollama=None,
        )
        assert proc._ollama_title_language is None

    async def test_falls_back_on_ollama_failure(self) -> None:
        ollama = MagicMock()
        ollama.generate_title.side_effect = OllamaUnavailable("service down")
        proc = self._processor(ollama=ollama)
        assert await proc._derive_title("Fix the login bug") == "Fix the login bug"

    async def test_falls_back_on_empty_ai_title(self) -> None:
        ollama = MagicMock()
        ollama.generate_title.return_value = "   "
        proc = self._processor(ollama=ollama)
        assert await proc._derive_title("Fix the login bug") == "Fix the login bug"

    async def test_skips_ollama_for_empty_message(self) -> None:
        ollama = MagicMock()
        proc = self._processor(ollama=ollama)
        assert await proc._derive_title("   ") == "Untitled Task"
        ollama.generate_title.assert_not_called()

    async def test_passes_title_language_from_config(self) -> None:
        ollama = MagicMock()
        ollama.generate_title.return_value = "titre"
        proc = self._processor(ollama=ollama, config={"ollama": {"title_language": "fr"}})
        await proc._derive_title("Some message")
        assert ollama.generate_title.call_args[0][1] == "fr"

    async def test_falls_back_when_generate_title_raises_attribute_error(self) -> None:
        # Ensures the try block covers title.strip() — a non-string return from a
        # misconfigured mock (or subclass) must not propagate out of _derive_title.
        ollama = MagicMock()
        ollama.generate_title.return_value = None  # non-str → .strip() would raise
        proc = self._processor(ollama=ollama)
        # None.strip() raises AttributeError — must fall back, not propagate.
        assert await proc._derive_title("Fix the login bug") == "Fix the login bug"


# ── Live integration (auto-skips when Ollama is not reachable) ─────────────────

@pytest.mark.skipif(
    not _LIVE_MODELS, reason=f"Ollama not reachable on {_OLLAMA_URL}"
)
class TestLiveOllama:
    """End-to-end calls against a real local Ollama, using whichever model is pulled."""

    _MESSAGE = (
        "Hey team, the checkout page throws a 500 when the coupon field is empty. "
        "We need to validate it server-side before Friday's release and add a test."
    )

    def test_generate_title_returns_usable_title(self) -> None:
        client = OllamaClient(model=_PREFERRED_MODEL, timeout=60)
        title = client.generate_title(self._MESSAGE)
        assert title.strip()                 # non-empty
        assert len(title) <= 100             # capped
        assert "\n" not in title             # a single line, not a paragraph

    def test_generate_title_respects_language_hint(self) -> None:
        client = OllamaClient(model=_PREFERRED_MODEL, timeout=60)
        title = client.generate_title(
            "Нужно обновить документацию по оплате до конца недели.",
            language_hint="en",
        )
        assert title.strip()
        # The English hint is honoured only if the title is not in Cyrillic script.
        assert any("a" <= c.lower() <= "z" for c in title)   # has Latin letters
        assert not any("Ѐ" <= c <= "ӿ" for c in title)  # no Cyrillic

    async def test_derive_title_uses_live_ai_title(self) -> None:
        client = OllamaClient(model=_PREFERRED_MODEL, timeout=60)
        proc = TaskProcessor(
            slack=MagicMock(),
            task_creator=MagicMock(),
            user_mapper=UserMapper({}),
            config={},
            ollama=client,
        )
        title = await proc._derive_title(self._MESSAGE)
        assert title.strip()
        assert len(title) <= 100

    async def test_derive_title_falls_back_when_service_down(self) -> None:
        # A live Ollama is running, but this client points at a dead port: the
        # processor must still produce the first-line fallback, not raise.
        dead = OllamaClient(base_url="http://127.0.0.1:1", timeout=2)
        proc = TaskProcessor(
            slack=MagicMock(),
            task_creator=MagicMock(),
            user_mapper=UserMapper({}),
            config={},
            ollama=dead,
        )
        title = await proc._derive_title(self._MESSAGE)
        assert title == _extract_title(self._MESSAGE)
