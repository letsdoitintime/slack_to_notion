"""Tests for src/utils/config_loader.py"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.utils.config_loader import _resolve_all, _resolve_env_vars, load_config


class TestResolveEnvVars:
    def test_replaces_defined_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _resolve_env_vars("${MY_VAR}") == "hello"

    def test_replaces_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "foo")
        monkeypatch.setenv("B", "bar")
        assert _resolve_env_vars("${A}/${B}") == "foo/bar"

    def test_raises_on_missing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(ValueError, match="MISSING_VAR"):
            _resolve_env_vars("${MISSING_VAR}")

    def test_no_placeholder_unchanged(self) -> None:
        assert _resolve_env_vars("plain-string") == "plain-string"


class TestResolveAll:
    def test_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOKEN", "tok123")
        result = _resolve_all({"notion": {"token": "${TOKEN}"}})
        assert result == {"notion": {"token": "tok123"}}

    def test_list_of_dicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB", "db-id")
        result = _resolve_all([{"notion_db": "${DB}"}])
        assert result == [{"notion_db": "db-id"}]

    def test_non_string_passthrough(self) -> None:
        assert _resolve_all({"count": 42, "flag": True}) == {"count": 42, "flag": True}


class TestLoadConfig:
    def _write_config(self, tmp_path: Path, content: str) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent(content))
        return cfg

    def _minimal_config(self, extra: str = "") -> str:
        base = """
        slack:
          bot_token: ${SLACK_BOT_TOKEN}
          app_token: ${SLACK_APP_TOKEN}
        notion:
          token: ${NOTION_TOKEN}
        emoji_mappings:
          - emoji: eyes
            notion_db: ${NOTION_DB_REVIEW}
            processor: TaskProcessor
        """
        return textwrap.dedent(base) + textwrap.dedent(extra)

    def test_loads_valid_config(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            """
            slack:
              bot_token: ${SLACK_BOT_TOKEN}
              app_token: ${SLACK_APP_TOKEN}
            notion:
              token: ${NOTION_TOKEN}
            emoji_mappings:
              - emoji: eyes
                notion_db: ${NOTION_DB_REVIEW}
                processor: TaskProcessor
            """,
        )
        config = load_config(cfg)
        assert config["slack"]["bot_token"] == "xoxb-test"
        assert config["notion"]["token"] == "secret_test"
        assert config["emoji_mappings"][0]["emoji"] == "eyes"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_missing_required_key_raises(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            """
            slack:
              bot_token: ${SLACK_BOT_TOKEN}
            notion:
              token: ${NOTION_TOKEN}
            emoji_mappings: []
            """,
        )
        with pytest.raises(ValueError):
            load_config(cfg)

    def test_empty_emoji_mappings_raises(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            """
            slack:
              bot_token: ${SLACK_BOT_TOKEN}
              app_token: ${SLACK_APP_TOKEN}
            notion:
              token: ${NOTION_TOKEN}
            emoji_mappings: []
            """,
        )
        with pytest.raises(ValueError, match="emoji_mappings"):
            load_config(cfg)

    def test_valid_notion_link_reply_section_loads(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                notion_link_reply:
                  enabled: true
                  channels: ["C123"]
                  message_template: "✅ <{notion_url}|{task_title}>"
                  in_thread: true
                  broadcast: false
                """
            ),
        )

        config = load_config(cfg)

        assert config["notion_link_reply"]["enabled"] is True
        assert config["notion_link_reply"]["channels"] == ["C123"]

    def test_absent_notion_link_reply_section_is_valid(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(tmp_path, self._minimal_config())

        config = load_config(cfg)

        assert "notion_link_reply" not in config

    def test_notion_link_reply_channels_must_be_list(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                notion_link_reply:
                  enabled: true
                  channels: C123
                """
            ),
        )

        with pytest.raises(ValueError, match="notion_link_reply.channels"):
            load_config(cfg)

    def test_notion_link_reply_channels_must_contain_non_empty_strings(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                notion_link_reply:
                  enabled: true
                  channels: [""]
                """
            ),
        )

        with pytest.raises(ValueError, match="notion_link_reply.channels\\[0\\]"):
            load_config(cfg)

    def test_notion_link_reply_must_be_mapping(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                notion_link_reply:
                  - C123
                """
            ),
        )

        with pytest.raises(ValueError, match="notion_link_reply"):
            load_config(cfg)


class TestOllamaValidation:
    """Validation of the optional `ollama` config section."""

    def _write_config(self, tmp_path: Path, content: str) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent(content))
        return cfg

    def _minimal_config(self, extra: str = "") -> str:
        base = """
        slack:
          bot_token: ${SLACK_BOT_TOKEN}
          app_token: ${SLACK_APP_TOKEN}
        notion:
          token: ${NOTION_TOKEN}
        emoji_mappings:
          - emoji: eyes
            notion_db: ${NOTION_DB_REVIEW}
            processor: TaskProcessor
        """
        return textwrap.dedent(base) + textwrap.dedent(extra)

    def test_absent_ollama_section_is_valid(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(tmp_path, self._minimal_config())
        config = load_config(cfg)
        assert "ollama" not in config

    def test_valid_ollama_section_loads(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  base_url: http://127.0.0.1:11434
                  model: qwen2.5:3b
                  timeout_s: 15
                  num_thread: 6
                  title_language: en
                """
            ),
        )
        config = load_config(cfg)
        assert config["ollama"]["enabled"] is True
        assert config["ollama"]["model"] == "qwen2.5:3b"

    def test_timeout_may_be_float(self, tmp_path: Path, minimal_env: dict) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  timeout_s: 7.5
                """
            ),
        )
        config = load_config(cfg)
        assert config["ollama"]["timeout_s"] == 7.5

    def test_ollama_must_be_mapping(self, tmp_path: Path, minimal_env: dict) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  - enabled
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama"):
            load_config(cfg)

    def test_enabled_must_be_boolean(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: "yes"
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama.enabled"):
            load_config(cfg)

    def test_base_url_must_be_string(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  base_url: 11434
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama.base_url"):
            load_config(cfg)

    def test_timeout_s_must_be_number(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  timeout_s: soon
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama.timeout_s"):
            load_config(cfg)

    def test_num_thread_must_be_integer(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  num_thread: 6.5
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama.num_thread"):
            load_config(cfg)

    def test_title_language_must_be_string(
        self, tmp_path: Path, minimal_env: dict
    ) -> None:
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  title_language: 42
                """
            ),
        )
        with pytest.raises(ValueError, match="ollama.title_language"):
            load_config(cfg)

    def test_accepts_string_timeout_and_num_thread(
        self, tmp_path: Path, minimal_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ${ENV_VAR} expansion yields strings; validator must accept numeric strings
        # because build_ollama_client already coerces them via float()/int().
        monkeypatch.setenv("OLLAMA_TIMEOUT", "12.5")
        monkeypatch.setenv("OLLAMA_THREADS", "4")
        cfg = self._write_config(
            tmp_path,
            self._minimal_config(
                """
                ollama:
                  enabled: true
                  timeout_s: ${OLLAMA_TIMEOUT}
                  num_thread: ${OLLAMA_THREADS}
                """
            ),
        )
        config = load_config(cfg)
        assert config["ollama"]["timeout_s"] == "12.5"
        assert config["ollama"]["num_thread"] == "4"
