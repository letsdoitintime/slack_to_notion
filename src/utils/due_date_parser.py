"""Best-effort due-date extraction from free-form Slack message text."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from dateparser.search import search_dates as _search_dates

    _DATEPARSER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DATEPARSER_AVAILABLE = False
    logger.warning("dateparser is not installed; due-date parsing is disabled.")


_DATEPARSER_SETTINGS = {
    "PREFER_DATES_FROM": "future",
    "RETURN_AS_TIMEZONE_AWARE": False,
}


def parse_due_date(text: str) -> str | None:
    """Scan *text* for a date/time mention and return it as an ISO 8601 date string
    (``YYYY-MM-DD``), or ``None`` when nothing is found or dateparser is unavailable.

    Only the first recognised date is returned. Common relative expressions like
    "tomorrow", "next Friday", "in 3 days" are supported via dateparser.
    """
    if not _DATEPARSER_AVAILABLE or not text:
        return None
    try:
        results = _search_dates(text, languages=["en"], settings=_DATEPARSER_SETTINGS)
    except Exception:
        logger.debug("dateparser raised an exception for text: %r", text, exc_info=True)
        return None
    if results:
        _, dt = results[0]
        return dt.strftime("%Y-%m-%d")
    return None
