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
