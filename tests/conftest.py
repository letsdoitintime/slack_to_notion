"""Shared test fixtures and helpers."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove real secrets from the environment during tests."""
    for var in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "NOTION_TOKEN",
        "NOTION_DB_TASKS",
        "NOTION_DB_REVIEW",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def minimal_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set the minimum environment variables required to load the config."""
    values = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "NOTION_TOKEN": "secret_test",
        "NOTION_DB_TASKS": "db-tasks-id",
        "NOTION_DB_REVIEW": "db-review-id",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values
