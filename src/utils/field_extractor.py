"""Extracts structured fields from Slack message text via optional regex patterns."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def extract_fields(message_text: str, body_fields_config: list[dict]) -> dict[str, str]:
    """Extract field values from *message_text* based on *body_fields_config*.

    Each entry in *body_fields_config* may carry an optional ``extract_pattern``
    key containing a Python regex string.  Capturing group 1 of the first match
    is used as the field value.  Fields without a pattern, or whose pattern does
    not match, produce an empty string — they will appear as blank rows in the
    Notion body table for manual filling.

    Returns a ``{key: value}`` dict for every entry in *body_fields_config*.
    Never raises.
    """
    result: dict[str, str] = {}
    for field_cfg in body_fields_config:
        key: str = field_cfg.get("key", "")
        if not key:
            continue
        pattern: str | None = field_cfg.get("extract_pattern")
        if pattern and message_text:
            try:
                match = re.search(pattern, message_text)
                result[key] = match.group(1).strip() if match else ""
            except re.error as exc:
                logger.warning(
                    "Invalid extract_pattern for body field '%s': %s", key, exc
                )
                result[key] = ""
        else:
            result[key] = ""
    return result
