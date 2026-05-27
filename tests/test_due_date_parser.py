"""Tests for src/utils/due_date_parser.py"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.utils.due_date_parser import parse_due_date


class TestParseDueDate:
    def test_returns_none_for_empty_string(self) -> None:
        assert parse_due_date("") is None

    def test_returns_none_when_no_date_in_text(self) -> None:
        result = parse_due_date("please review this PR when you get a chance")
        assert result is None

    def test_parses_explicit_date(self) -> None:
        result = parse_due_date("Fix this by 2027-03-15")
        assert result == "2027-03-15"

    def test_returns_iso_format(self) -> None:
        result = parse_due_date("due June 20 2027")
        assert result is not None
        # Must be YYYY-MM-DD
        parts = result.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4

    def test_handles_dateparser_exception_gracefully(self) -> None:
        with patch(
            "src.utils.due_date_parser._search_dates",
            side_effect=Exception("boom"),
        ):
            result = parse_due_date("some text with a date")
        assert result is None

    def test_returns_none_when_dateparser_unavailable(self) -> None:
        with patch("src.utils.due_date_parser._DATEPARSER_AVAILABLE", False):
            assert parse_due_date("tomorrow") is None
