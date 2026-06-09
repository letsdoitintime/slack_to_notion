"""Loads and validates the YAML config, resolving ${ENV_VAR} placeholders."""

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

_REQUIRED_KEYS = [
    ("slack", "bot_token"),
    ("slack", "app_token"),
    ("notion", "token"),
]


def _resolve_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} placeholders with their environment variable values."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            raise ValueError(
                f"Config references undefined environment variable: '{var_name}'. "
                f"Make sure it is set in your .env file or shell environment."
            )
        return val

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _resolve_all(obj: Any) -> Any:
    """Recursively resolve ${ENV_VAR} placeholders in all string values."""
    if isinstance(obj, dict):
        return {k: _resolve_all(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_all(item) for item in obj]
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    return obj


def _validate(config: dict) -> None:
    """Raise ValueError if required top-level keys are missing."""
    for section, key in _REQUIRED_KEYS:
        if not config.get(section, {}).get(key):
            raise ValueError(
                f"Config is missing required field '{section}.{key}'. "
                f"Check your config.yaml and .env file."
            )
    if not config.get("emoji_mappings"):
        raise ValueError(
            "Config must define at least one entry under 'emoji_mappings'."
        )
    for i, mapping in enumerate(config["emoji_mappings"]):
        for field in ("emoji", "notion_db", "processor"):
            if not mapping.get(field):
                raise ValueError(
                    f"emoji_mappings[{i}] is missing required field '{field}'."
                )
        _validate_reactor_assignees(mapping, i)

    _validate_body_fields(config.get("fields", {}).get("body_fields", []))
    _validate_allowed_reactors(config.get("allowed_reactors"))
    _validate_notion_link_reply(config.get("notion_link_reply"))
    _validate_ollama(config.get("ollama"))


def _validate_ollama(ollama: object) -> None:
    """Raise ValueError if the optional 'ollama' section is present but malformed."""
    if ollama is None:
        return
    if not isinstance(ollama, dict):
        raise ValueError("'ollama' must be a mapping.")

    enabled = ollama.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("'ollama.enabled' must be a boolean.")

    for key in ("base_url", "model", "title_language"):
        value = ollama.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"'ollama.{key}' must be a string.")

    timeout = ollama.get("timeout_s")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
    ):
        raise ValueError("'ollama.timeout_s' must be a number.")

    num_thread = ollama.get("num_thread")
    if num_thread is not None and (
        isinstance(num_thread, bool) or not isinstance(num_thread, int)
    ):
        raise ValueError("'ollama.num_thread' must be an integer.")


def _validate_body_fields(body_fields: object) -> None:
    """Raise ValueError if body_fields config is malformed."""
    if not isinstance(body_fields, list):
        raise ValueError("'fields.body_fields' must be a list.")
    for i, bf in enumerate(body_fields):
        if not isinstance(bf, dict):
            raise ValueError(f"fields.body_fields[{i}] must be a mapping.")
        if not bf.get("key"):
            raise ValueError(
                f"fields.body_fields[{i}] is missing required field 'key'."
            )
        if not bf.get("label"):
            raise ValueError(
                f"fields.body_fields[{i}] is missing required field 'label'."
            )


def _validate_allowed_reactors(allowed_reactors: object) -> None:
    """Raise ValueError if allowed_reactors is present but malformed."""
    if allowed_reactors is None:
        return
    if not isinstance(allowed_reactors, list):
        raise ValueError(
            "'allowed_reactors' must be a list of Slack user ID strings."
        )
    for i, entry in enumerate(allowed_reactors):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"allowed_reactors[{i}] must be a non-empty string (Slack user ID)."
            )


def _validate_notion_link_reply(notion_link_reply: object) -> None:
    """Raise ValueError if notion_link_reply is present but malformed."""
    if notion_link_reply is None:
        return
    if not isinstance(notion_link_reply, dict):
        raise ValueError("'notion_link_reply' must be a mapping.")

    channels = notion_link_reply.get("channels")
    if channels is not None:
        if not isinstance(channels, list):
            raise ValueError(
                "'notion_link_reply.channels' must be a list of Slack channel ID strings."
            )
        for i, entry in enumerate(channels):
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"notion_link_reply.channels[{i}] must be a non-empty string "
                    "(Slack channel ID)."
                )

    for key in ("enabled", "in_thread", "broadcast"):
        value = notion_link_reply.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"'notion_link_reply.{key}' must be a boolean.")

    message_template = notion_link_reply.get("message_template")
    if message_template is not None and not isinstance(message_template, str):
        raise ValueError("'notion_link_reply.message_template' must be a string.")


def _validate_reactor_assignees(mapping: dict, index: int) -> None:
    """Raise ValueError if reactor_assignees in an emoji mapping is malformed."""
    reactor_assignees = mapping.get("reactor_assignees")
    if reactor_assignees is None:
        return
    if not isinstance(reactor_assignees, dict):
        raise ValueError(
            f"emoji_mappings[{index}].reactor_assignees must be a mapping."
        )
    for slack_id, assignee_cfg in reactor_assignees.items():
        if not isinstance(assignee_cfg, dict):
            raise ValueError(
                f"emoji_mappings[{index}].reactor_assignees['{slack_id}'] "
                f"must be a mapping with a 'notion_user_ids' list."
            )
        if not isinstance(assignee_cfg.get("notion_user_ids", []), list):
            raise ValueError(
                f"emoji_mappings[{index}].reactor_assignees['{slack_id}']"
                f".notion_user_ids must be a list."
            )


def load_config(path: str | Path = "config/config.yaml") -> dict:
    """Load config YAML, resolve env vars, and validate required fields."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path.resolve()}. "
            f"Copy config/config.yaml.example and fill in your values."
        )
    with config_path.open() as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("Config file must contain a YAML mapping at the top level.")
    resolved = _resolve_all(raw)
    _validate(resolved)
    return resolved
