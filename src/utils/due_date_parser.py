"""Best-effort due-date extraction from free-form Slack message text."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

logger = logging.getLogger(__name__)

try:
    from dateparser import parse as _parse_one
    from dateparser.search import search_dates as _search_dates

    _DATEPARSER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DATEPARSER_AVAILABLE = False
    logger.warning("dateparser is not installed; due-date parsing is disabled.")


_DATEPARSER_SETTINGS = {
    "PREFER_DATES_FROM": "future",
    "RETURN_AS_TIMEZONE_AWARE": False,
}

# A due date further out than this is not a due date — it is a misparse. Two years
# is generous for real work and still rejects the year-4300 class of nonsense.
_MAX_FUTURE_DAYS = 730

_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAY = (
    r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
)
_UNIT = r"hours?|days?|weeks?|months?"

# A month name only counts as a date when a day number rides along. On its own,
# "may" is the English modal verb far more often than the month ("this may require
# a code change" → 2027-05-22), and "June Invoice" is a document, not a deadline.
_MONTH_WITH_DAY = re.compile(
    rf"\b(?:{_MONTH})\b\W{{0,4}}\b\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\b\W{{0,4}}\b(?:{_MONTH})\b",
    re.IGNORECASE,
)

# A numeric date must be COMPLETE — year, month and day — with a 4-digit year.
#
# The 4-digit year is what separates a date from the ranges and counts this traffic
# is full of: `10-4`, `2-3`, `6-7`, `1/2` all parse as dates and none of them are.
# That costs the occasional real `24/07`, which is the right trade here.
#
# Requiring the day too is what stops the same trick sneaking back in near the
# current year. `SDK 2027-1` and `ISO 2027-2` are a version and a standard, but
# dateparser fills the missing day from today and lands inside the horizon check —
# so a year-month token would be accepted as a due date. `ISO 3166-2` only gets
# caught by the horizon because 3166 is absurd; 2027 is not.
_NUMERIC_DATE = re.compile(
    r"\b\d{4}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\b"
    r"|\b\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{4}\b"
)

# Explicit relative expressions — the ones the feature is actually for.
#
# A weekday is accepted BARE, without a preceding cue, because `search_dates`
# strips the cue: "next Friday", "due Friday" and "deadline Monday" all come back
# with a matched substring of just "Friday". Requiring `next|on|by` inside the
# match therefore rejected every one of them — including "next Friday", which this
# module's own docstring advertises as supported.
#
# A bare weekday is safe in a way a bare month name is not: it resolves to within a
# week, so the sanity window keeps it plausible. And the false positive that
# motivated whole-word matching — "We" in "We are testing…" — still cannot match,
# because "we" is not a weekday abbreviation ("wed" is the shortest that is).
_RELATIVE = re.compile(
    rf"\b(?:today|tomorrow|tonight)\b"
    rf"|\b(?:{_WEEKDAY})\b"
    rf"|\b(?:next|this|coming)\s+(?:{_UNIT})\b"
    rf"|\b(?:in\s+)?(?:a|an|\d{{1,3}})\s+(?:{_UNIT})\b",
    re.IGNORECASE,
)


def _is_datelike(matched: str) -> bool:
    """Does *matched* actually look like a date, rather than a stray number or word?

    ``search_dates`` scans for anything it can coerce into a date. Over real Slack
    traffic that means transaction amounts, method IDs, error codes, masked card
    numbers, ISO standard references, version ranges and ordinary English words all
    come back as dates — ``4300`` → 4300-07-22, ``$79`` → 2079-07-22, ``ISO 3166-2``
    → 3166-02-22, ``2-3`` → a March date, ``We are testing`` → next Wednesday, and
    ``this may require`` → May. None of those are due dates, and none of them fail
    loudly: they are written onto a real Notion task as if they were meant.

    So the match has to earn it — a month name *with* a day, a numeric date *with*
    a four-digit year, or an explicit relative expression.
    """
    return bool(
        _MONTH_WITH_DAY.search(matched)
        or _NUMERIC_DATE.search(matched)
        or _RELATIVE.search(matched)
    )


def parse_due_date(text: str) -> str | None:
    """Scan *text* for a date/time mention and return it as an ISO 8601 date string
    (``YYYY-MM-DD``), or ``None`` when nothing is found or dateparser is unavailable.

    Returns the first *plausible* date. Common relative expressions like "tomorrow",
    "next Friday" and "in 3 days" are supported via dateparser; matches that are
    merely bare numbers, and dates outside a sane due-date window, are skipped —
    see :func:`_is_datelike` for why that filtering is not optional here.
    """
    if not _DATEPARSER_AVAILABLE or not text:
        return None
    try:
        results = _search_dates(text, languages=["en"], settings=_DATEPARSER_SETTINGS)
    except Exception:
        logger.debug("dateparser raised an exception for text: %r", text, exc_info=True)
        return None
    if not results:
        return None

    today = date.today()
    horizon = today + timedelta(days=_MAX_FUTURE_DAYS)
    for matched, dt in results:
        if not _is_datelike(matched):
            logger.debug("Ignoring non-date match %r (not a date expression).", matched)
            continue

        # Re-resolve the match on its own. `search_dates` resolves each match
        # relative to the previous one in the same text, so a match we rejected
        # still moves the base for the ones after it: in "it may require … up to
        # 2 weeks of development", "2 weeks" resolved off the bogus "may" and came
        # back as 2027-06-05 instead of two weeks from today. Parsing the matched
        # substring alone anchors it to now, where it belongs.
        standalone = _parse_one(matched, languages=["en"], settings=_DATEPARSER_SETTINGS)
        parsed = (standalone or dt).date()

        # A due date in the past is not a due date; one past the horizon is a
        # misparsed year. Both mean: keep looking, this match was not it.
        if parsed < today or parsed > horizon:
            logger.debug(
                "Ignoring out-of-range date %s from match %r.", parsed, matched
            )
            continue
        return parsed.isoformat()
    return None
