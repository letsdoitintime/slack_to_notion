"""Tests for utils/field_extractor.py."""

from __future__ import annotations

import pytest

from src.utils.field_extractor import extract_fields


class TestExtractFields:
    def test_empty_config_returns_empty_dict(self) -> None:
        assert extract_fields("any text", []) == {}

    def test_field_without_pattern_returns_empty_string(self) -> None:
        config = [{"key": "reporter_name", "label": "Reporter"}]
        result = extract_fields("some message", config)
        assert result == {"reporter_name": ""}

    def test_pattern_match_returns_captured_group(self) -> None:
        config = [{"key": "channel_name", "label": "Channel", "extract_pattern": r"#(\w+)"}]
        result = extract_fields("Check #general please", config)
        assert result["channel_name"] == "general"

    def test_pattern_no_match_returns_empty_string(self) -> None:
        config = [{"key": "due_date", "label": "Due Date", "extract_pattern": r"due:(\S+)"}]
        result = extract_fields("no date here", config)
        assert result["due_date"] == ""

    def test_empty_message_text_returns_empty_strings(self) -> None:
        config = [
            {"key": "priority", "label": "Priority", "extract_pattern": r"priority:(\w+)"},
            {"key": "status", "label": "Status"},
        ]
        result = extract_fields("", config)
        assert result == {"priority": "", "status": ""}

    def test_multiple_fields_mixed(self) -> None:
        config = [
            {"key": "task_type", "label": "Type", "extract_pattern": r"type:(\w+)"},
            {"key": "channel_name", "label": "Channel"},
        ]
        result = extract_fields("type:Review something", config)
        assert result["task_type"] == "Review"
        assert result["channel_name"] == ""

    def test_invalid_regex_logs_warning_and_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        config = [{"key": "bad_field", "label": "Bad", "extract_pattern": r"[invalid"}]
        with caplog.at_level(logging.WARNING, logger="src.utils.field_extractor"):
            result = extract_fields("some message", config)
        assert result["bad_field"] == ""
        assert "bad_field" in caplog.text

    def test_value_is_stripped_of_whitespace(self) -> None:
        config = [{"key": "reporter_name", "label": "Reporter", "extract_pattern": r"by:\s+(\w+)\s*"}]
        result = extract_fields("submitted by:  alice  done", config)
        assert result["reporter_name"] == "alice"

    def test_entry_missing_key_is_skipped(self) -> None:
        config = [{"label": "No Key Here"}]
        result = extract_fields("text", config)
        assert result == {}

    def test_first_match_used_when_multiple_present(self) -> None:
        config = [{"key": "priority", "label": "Priority", "extract_pattern": r"P(\d)"}]
        result = extract_fields("P1 and also P3", config)
        assert result["priority"] == "1"
