"""Shared test fixtures and helpers."""

from __future__ import annotations

import os

import aiosqlite.core
import pytest

# aiosqlite.Connection extends threading.Thread without daemon=True, so any
# connection whose thread hasn't been reaped yet will block Python's
# threading._shutdown() at the end of the test session.  Marking the thread
# as a daemon lets Python exit without waiting for it; aiosqlite's close()
# still sends the stop sentinel so the thread exits cleanly in the normal case.
_orig_aiosqlite_init = aiosqlite.core.Connection.__init__


def _daemonised_aiosqlite_init(self, *args, **kwargs):
    _orig_aiosqlite_init(self, *args, **kwargs)
    self.daemon = True


aiosqlite.core.Connection.__init__ = _daemonised_aiosqlite_init


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
