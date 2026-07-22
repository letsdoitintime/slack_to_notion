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


class TestRejectsNonDates:
    """Real messages from the bot's own traffic that used to produce a due date.

    Every string here was taken from the production corpus, where `search_dates`
    coerced it into a date that was then written onto a Notion task with no error.
    """

    @pytest.mark.parametrize(
        "text, was",
        [
            # Bare integers read as years — amounts, IDs, error and ticket numbers.
            ("сумма 4300 TRY, по их словам", "4300-07-22"),
            ("You need to update the method ID to 1664", "1664-07-22"),
            ("Доброе утро! P1/deposits/Pay2Play 9800", "9800-07-22"),
            ("external id duplicate ... (ID: 1062)", "1062-07-22"),
            ("6048 - make sure we release", "6048-07-22"),
            # The reported case: a currency amount became the year 2079.
            ("$79.89 Manually credited to the user's balance", "2079-07-22"),
            # An ISO standard reference became a date.
            ("The region must be a valid Canada subdivision (ISO 3166-2)", "3166-02-22"),
            # Ordinary English words matched as weekday/month names.
            ("We are testing the withdrawal method", "next Wednesday"),
            ("this may require minor changes from our end", "May"),
            # Version ranges, counts and percentages.
            ("там же набор из 2-3 значениц максимум", "a March date"),
            ("не часто но на день 1-3", "a January date"),
            ("Pay IN: 6,5 % Pay OUT: 4,5%", "June 5"),
            ("P2P/СБП RUB ... - 10%", "October"),
            # A bare clock time is not a due date.
            ('"timestamp": "06:42:32"', "tomorrow"),
            # Year-month tokens near the current year: dateparser invents the day
            # from today and the result lands inside the horizon, so the sanity
            # window cannot catch these the way it catches `ISO 3166-2`.
            ("upgrade to SDK 2027-1", "2027-01-<today's day>"),
            ("see ISO 2027-2", "2027-02-<today's day>"),
            ("spec version 2026-12", "2026-12-<today's day>"),
            ("ticket 2027-3 is blocked", "2027-03-<today's day>"),
        ],
    )
    def test_no_due_date_extracted(self, text: str, was: str) -> None:
        assert parse_due_date(text) is None, f"still parses as a date (was {was})"

    def test_no_date_is_ever_absurdly_far_out(self) -> None:
        """The sanity window, stated directly."""
        from datetime import date, timedelta

        from src.utils.due_date_parser import _MAX_FUTURE_DAYS

        for text in ("сумма 4300 TRY", "9800", "$79.89", "ISO 3166-2"):
            got = parse_due_date(text)
            if got is not None:
                parsed = date.fromisoformat(got)
                assert date.today() <= parsed <= date.today() + timedelta(
                    days=_MAX_FUTURE_DAYS
                )


class TestStillParsesRealDates:
    """The expressions the feature exists for must survive the filtering."""

    @pytest.mark.parametrize(
        "text",
        [
            "please finish this by tomorrow",
            "let's ship it today",
            "we'll get to it next week",
            "can you do it on Monday",
            "this will take 2 weeks",
            "give it 48 hours",
            "deadline is by 1 August",
            "due by July 1st",
            "target: 10 July",
            "Fix this by 2027-03-15",
            "deploy 2027-01-15 confirmed",
            "due 15/03/2027",
        ],
    )
    def test_parses(self, text: str) -> None:
        assert parse_due_date(text) is not None, "a real due-date expression was lost"

    def test_relative_match_is_anchored_to_today_not_a_prior_match(self) -> None:
        """`search_dates` resolves each match relative to the previous one.

        In this real message "2 weeks" was computed off the bogus "may" match and
        came back as 2027-06-05. The accepted match is re-parsed on its own so it
        anchors to now — roughly two weeks out, not eleven months.
        """
        from datetime import date, timedelta

        got = parse_due_date(
            "it may require code change and up to 2 weeks of development"
        )
        assert got is not None
        assert date.fromisoformat(got) <= date.today() + timedelta(days=30)
