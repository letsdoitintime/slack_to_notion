"""Maps extracted task data to Notion page properties and creates pages."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .client import NotionClient

logger = logging.getLogger(__name__)

_MAX_TEXT_LEN = 2000

# ── Data transfer object ──────────────────────────────────────────────────────


@dataclass
class TaskData:
    """All fields extracted from a Slack reaction event."""

    title: str
    slack_url: str
    reporter_name: str
    assignee_notion_id: str | None
    status: str
    priority: str
    task_type: str
    due_date: str | None        # ISO 8601 date string e.g. "2024-06-15", or None
    channel_name: str
    message_text: str
    extra: dict = field(default_factory=dict)   # reserved for processor extensions


# ── Field formatter ───────────────────────────────────────────────────────────


def _format_field(field_type: str, value: str) -> dict | None:
    """Build a Notion property value dict for *field_type* and *value*.

    Supported types: title, rich_text, url, select, status, multi_select,
    date, number, checkbox, people / person.

    Returns ``None`` when the value is empty (for optional fields) or the
    type is unsupported — callers safely skip ``None`` results.
    """
    match field_type:
        case "title":
            return {"title": [{"text": {"content": value[:_MAX_TEXT_LEN]}}]}
        case "rich_text":
            return {"rich_text": [{"text": {"content": value[:_MAX_TEXT_LEN]}}]}
        case "url":
            return {"url": value} if value else None
        case "select":
            return {"select": {"name": value}} if value else None
        case "status":
            return {"status": {"name": value}} if value else None
        case "multi_select":
            options = [{"name": v.strip()} for v in value.split(",") if v.strip()]
            return {"multi_select": options} if options else None
        case "date":
            return {"date": {"start": value}} if value else None
        case "number":
            try:
                return {"number": float(value)}
            except (ValueError, TypeError):
                return None
        case "checkbox":
            return {"checkbox": value.lower() in ("true", "1", "yes")}
        case "people" | "person":
            uids = [v.strip() for v in value.split(",") if v.strip()]
            return {"people": [{"object": "user", "id": uid} for uid in uids]} if uids else None
        case _:
            logger.debug("Unsupported Notion field type '%s' — skipping.", field_type)
            return None


# ── Task creator ──────────────────────────────────────────────────────────────


class TaskCreator:
    """Builds Notion page properties from :class:`TaskData` and creates the page.

    Driven entirely by the ``notion_fields`` section in config.yaml — no
    database schema fetching needed.  Each entry maps a Notion property name
    to a ``{type, source}`` dict:

    .. code-block:: yaml

        notion_fields:
          Name:
            type: title
            source: task_title          # key from the context dict below
          Status:
            type: status
            source: "literal:Not Started"   # hard-coded constant
          Assignee:
            type: people
            source: assignee_notion_id  # Notion user UUID from user_mapper

    Available context keys:
        task_title, slack_url, reporter_name, assignee_notion_id,
        status, priority, task_type, due_date, channel_name, message_text,
        plus any key defined in body_fields (populated via extract_fields).

    Title templating:
        If ``task_title_template`` is set (globally in ``fields:`` or per-emoji
        in ``emoji_mappings``), it is rendered with the context dict — missing
        keys become empty strings.  The per-emoji value takes priority.

    Body generation:
        ``body_header_template`` (global or per-emoji) and ``body_fields``
        (global) drive a paragraph + table appended as Notion page children.
    """

    def __init__(self, notion: NotionClient, fields_config: dict) -> None:
        self._notion = notion
        self._fields_config = fields_config
        self._notion_fields: dict[str, dict] = fields_config.get("notion_fields", {})

    def _build_context(self, task_data: TaskData) -> dict[str, Any]:
        """Flatten :class:`TaskData` into a plain string-valued context dict."""
        return {
            "task_title": task_data.title,
            "slack_url": task_data.slack_url,
            "reporter_name": task_data.reporter_name,
            "assignee_notion_id": task_data.assignee_notion_id or "",
            "status": task_data.status,
            "priority": task_data.priority,
            "task_type": task_data.task_type,
            "due_date": task_data.due_date or "",
            "channel_name": task_data.channel_name,
            "message_text": task_data.message_text,
            **{str(k): str(v) for k, v in task_data.extra.items()},
        }

    def build_properties(self, task_data: TaskData, mapping: dict | None = None) -> dict:
        """Return a Notion ``properties`` dict ready for ``pages.create``.

        Applies the title template (per-emoji → global → raw title) before
        building properties.  When ``notion_fields`` is empty in config, falls
        back to setting only the ``Name`` title so the page is always valid.
        """
        context = self._build_context(task_data)

        # ── Apply title template ──────────────────────────────────────────────
        title_template: str | None = (
            (mapping or {}).get("task_title_template")
            or self._fields_config.get("task_title_template")
        )
        if title_template:
            context["task_title"] = _render_template(title_template, context)

        if not self._notion_fields:
            return {
                "Name": {
                    "title": [{"text": {"content": context["task_title"][:_MAX_TEXT_LEN]}}]
                }
            }

        properties: dict = {}
        has_title = False

        for prop_name, field_cfg in self._notion_fields.items():
            if not isinstance(field_cfg, dict):
                continue
            field_type: str = field_cfg.get("type", "rich_text")
            source: str = str(field_cfg.get("source", ""))

            # Resolve value: literal constant or context lookup.
            if source.startswith("literal:"):
                value = source[len("literal:"):]
            else:
                value = str(context.get(source, "") or "")

            prop = _format_field(field_type, value)
            if prop is not None:
                properties[prop_name] = prop
                if field_type == "title":
                    has_title = True

        # Notion requires exactly one title property per page.
        if not has_title:
            properties.setdefault(
                "Name",
                {"title": [{"text": {"content": context["task_title"][:_MAX_TEXT_LEN]}}]},
            )

        return properties

    def _build_body_blocks(
        self, task_data: TaskData, mapping: dict | None = None
    ) -> list[dict]:
        """Build Notion block children for the page body.

        Produces (in order):
        1. A paragraph block from ``body_header_template`` (per-emoji overrides global).
        2. A two-column table (Field | Value) from ``body_fields`` config.

        Returns an empty list when neither template nor body_fields are configured.
        """
        context = self._build_context(task_data)

        # Re-apply title template so the header can reference {task_title} too.
        title_template: str | None = (
            (mapping or {}).get("task_title_template")
            or self._fields_config.get("task_title_template")
        )
        if title_template:
            context["task_title"] = _render_template(title_template, context)

        blocks: list[dict] = []

        # ── Header paragraph ──────────────────────────────────────────────────
        header_template: str | None = (
            (mapping or {}).get("body_header_template")
            or self._fields_config.get("body_header_template")
        )
        if header_template:
            rendered = _render_template(header_template, context).strip()
            if rendered:
                blocks.append(_make_paragraph_block(rendered))

        # ── Body fields table ─────────────────────────────────────────────────
        body_fields: list[dict] = self._fields_config.get("body_fields", [])
        if body_fields:
            rows: list[dict] = [_make_table_row("Field", "Value")]
            for bf in body_fields:
                label: str = bf.get("label", bf.get("key", ""))
                value: str = str(context.get(bf.get("key", ""), "") or "")
                rows.append(_make_table_row(label, value))
            blocks.append(_make_table_block(rows))

        return blocks

    def create_task(
        self,
        database_id: str,
        task_data: TaskData,
        mapping: dict | None = None,
    ) -> dict | None:
        """Create a Notion page. Returns the page dict or ``None`` on failure."""
        properties = self.build_properties(task_data, mapping=mapping)
        if not properties:
            logger.error("Cannot create task: no properties could be built.")
            return None

        body_blocks = self._build_body_blocks(task_data, mapping=mapping)

        try:
            page = self._notion.create_page(
                database_id,
                properties,
                children=body_blocks or None,
            )
            logger.info(
                "Created Notion task '%s' → %s",
                task_data.title,
                page.get("url", ""),
            )
            return page
        except Exception as exc:
            logger.error("Notion API error while creating task: %s", exc)
            return None


# ── Template & block helpers ──────────────────────────────────────────────────


def _render_template(template: str, context: dict) -> str:
    """Render a ``{placeholder}`` template string against *context*.

    Any key absent from *context* silently becomes an empty string so that
    partially-filled templates never raise a ``KeyError``.
    """
    return template.format_map(defaultdict(str, context))


# Notion: a single block holds at most 100 rich_text objects.
_MAX_RICH_TEXT = 100
# Slack entity ``<...>`` — anchored to real entity prefixes (scheme / @ / # / !)
# so genuine angle brackets a user typed (``3 < 5``, ``<T>``) are left untouched.
_SLACK_ENTITY_RE = re.compile(
    r"<((?:https?://|mailto:|tel:|geo:|sms:|[@#!])[^<>]*)>"
)
# Bare http(s) URL inside plain text (stops at whitespace and Slack delimiters).
_URL_RE = re.compile(r"https?://[^\s<>|]+")
# Punctuation that unconditionally trails a URL in prose (never part of it).
_URL_TRAILING_PUNCT = ".,;:!?\"'»>"
# Closing brackets that trail a URL only when unmatched (e.g. strip the final )
# from "see (http://x.com)" but keep it in "https://en.wikipedia.org/wiki/Foo_(bar)").
_URL_CLOSING_BRACKETS: dict[str, str] = {")" : "(", "]": "[", "}": "{"}


def _decode_slack_entity(inner: str) -> tuple[str, str | None]:
    """Decode the inside of a Slack ``<...>`` entity into ``(display_text, link_url)``.

    ``<url|label>``  → ``(label, url)``      ``<url>``    → ``(url, url)``
    ``<@ID|name>``   → ``("@name", None)``   ``<@ID>``    → ``("@ID", None)``
    ``<#ID|name>``   → ``("#name", None)``   ``<!here>``  → ``("@here", None)``
    ``<mailto:a@b|Email>`` → ``("Email", None)``  (unwrapped, no link)

    Only http(s) entities carry a ``link_url`` — Notion reliably accepts only
    http(s) in a rich_text link, so mailto/tel/geo/sms are unwrapped to readable
    text without a link rather than risk a rejected payload.
    """
    payload, sep, label = inner.partition("|")
    if inner.startswith(("http://", "https://")):
        return (label if sep else payload), payload
    if inner[:1] in ("@", "#"):
        return inner[0] + (label if sep else payload[1:]), None
    if inner[:1] == "!":
        return "@" + (label if sep else payload[1:]), None
    # mailto:/tel:/geo:/sms: — show the label, else the address minus the scheme.
    if sep:
        return label, None
    _scheme, _colon, addr = inner.partition(":")
    return (addr or inner), None


def _split_plain_urls(span: str) -> list[tuple[str, str | None]]:
    """Split a plain-text *span* into segments, linkifying any bare http(s) URLs."""
    segments: list[tuple[str, str | None]] = []
    last = 0
    for m in _URL_RE.finditer(span):
        if m.start() > last:
            segments.append((span[last : m.start()], None))
        url = m.group(0)
        trail = ""
        while url:
            c = url[-1]
            if c in _URL_TRAILING_PUNCT:
                trail, url = c + trail, url[:-1]
            elif c in _URL_CLOSING_BRACKETS:
                open_c = _URL_CLOSING_BRACKETS[c]
                if url.count(c) > url.count(open_c):
                    trail, url = c + trail, url[:-1]
                else:
                    break
            else:
                break
        if url:
            segments.append((url, url))
        if trail:
            segments.append((trail, None))
        last = m.end()
    if last < len(span):
        segments.append((span[last:], None))
    return segments


def _slack_text_to_segments(text: str) -> list[tuple[str, str | None]]:
    """Tokenize Slack-formatted *text* into ``(display_text, link_url)`` segments."""
    segments: list[tuple[str, str | None]] = []
    pos = 0
    for m in _SLACK_ENTITY_RE.finditer(text):
        if m.start() > pos:
            segments.extend(_split_plain_urls(text[pos : m.start()]))
        segments.append(_decode_slack_entity(m.group(1)))
        pos = m.end()
    if pos < len(text):
        segments.extend(_split_plain_urls(text[pos:]))
    return segments


def _cap_rich_text(rich_text: list[dict]) -> list[dict]:
    """Enforce Notion's max-100-rich_text-objects-per-block limit.

    A pathological body (dozens of links) can produce more than 100 segments.
    Keep the first 99 (with their links intact) and fold the remainder into one
    trailing plain run, clipped to the 2 000-char limit, so the payload stays
    valid instead of being rejected by the API.
    """
    if len(rich_text) <= _MAX_RICH_TEXT:
        return rich_text
    logger.warning(
        "Paragraph produced %d rich_text segments (> %d); merging the overflow.",
        len(rich_text),
        _MAX_RICH_TEXT,
    )
    head = rich_text[: _MAX_RICH_TEXT - 1]
    tail = "".join(rt["text"]["content"] for rt in rich_text[_MAX_RICH_TEXT - 1 :])
    head.append({"type": "text", "text": {"content": tail[:_MAX_TEXT_LEN]}})
    return head


def _make_paragraph_block(text: str) -> dict:
    """Return a Notion paragraph block for *text*.

    Slack mrkdwn entities (``<url|label>``, ``<@ID>``, ``<#ID|name>`` …) are
    unwrapped and bare URLs are linkified, so links render as clickable links
    and no stray ``<>`` symbols leak into the page. Each segment's text is
    still chunked at the 2 000-char Notion limit, and the whole block is capped
    at 100 rich_text objects.
    """
    rich_text: list[dict] = []
    for content, link in _slack_text_to_segments(text):
        if not content:
            continue
        for i in range(0, len(content), _MAX_TEXT_LEN):
            text_obj: dict = {"content": content[i : i + _MAX_TEXT_LEN]}
            if link:
                text_obj["link"] = {"url": link}
            rich_text.append({"type": "text", "text": text_obj})
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _cap_rich_text(rich_text)},
    }


def _make_table_row(col1: str, col2: str) -> dict:
    """Return a Notion table_row block with two text cells."""
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {
            "cells": [
                [{"type": "text", "text": {"content": col1[:_MAX_TEXT_LEN]}}],
                [{"type": "text", "text": {"content": col2[:_MAX_TEXT_LEN]}}],
            ]
        },
    }


def _make_table_block(rows: list[dict]) -> dict:
    """Return a Notion table block containing *rows* (first row is the header)."""
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": 2,
            "has_column_header": True,
            "has_row_header": False,
            "children": rows,
        },
    }

