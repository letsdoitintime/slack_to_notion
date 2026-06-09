"""Default processor: extracts task data from a Slack message and creates a Notion task."""

from __future__ import annotations

import asyncio
import logging
import re

from ..db.database import DatabaseManager
from ..notion.task_creator import TaskCreator, TaskData, _render_template
from ..slack.client import SlackClient
from ..utils.due_date_parser import parse_due_date
from ..utils.field_extractor import extract_fields
from ..utils.ollama_client import OllamaClient
from ..utils.user_mapper import UserMapper
from .base import BaseProcessor

logger = logging.getLogger(__name__)

_DEFAULT_NOTION_LINK_REPLY_TEMPLATE = (
    "✅ <{notion_url}|{task_title}> · {task_type} · by {reporter_name}"
)


class TaskProcessor(BaseProcessor):
    """Creates a Notion task when a configured emoji reaction is added to a Slack message.

    Flow:
    1. Idempotency check — skip if the bot already confirmed this message.
    2. Fetch the full Slack message (handles thread replies).
    3. Resolve channel name, reporter info, and optional assignee (Notion user).
    4. Parse an optional due date from the message text.
    5. Extract structured body fields from the message text.
    6. Resolve assignee from per-emoji reactor_assignees config (falls back to user_mapper).
    7. Build :class:`~src.notion.task_creator.TaskData` and call
       :class:`~src.notion.task_creator.TaskCreator`.
    8. Add the confirmation reaction to the original Slack message.
    """

    def __init__(
        self,
        slack: SlackClient,
        task_creator: TaskCreator,
        user_mapper: UserMapper,
        config: dict,
        db: DatabaseManager | None = None,
        ollama: OllamaClient | None = None,
    ) -> None:
        self._slack = slack
        self._task_creator = task_creator
        self._user_mapper = user_mapper
        self._config = config
        self._db = db
        self._ollama = ollama
        # `or {}` guards a bare/blank `ollama:` key (parses to None), matching the
        # tolerance of build_ollama_client and _validate_ollama — a None section
        # means "feature off", never a startup crash.
        self._ollama_title_language: str | None = (
            (config.get("ollama") or {}).get("title_language") or None
        )

    # ── BaseProcessor interface ───────────────────────────────────────────────

    async def process(self, event: dict, mapping: dict) -> bool:
        item = event.get("item", {})
        channel: str = item.get("channel", "")
        ts: str = item.get("ts", "")
        reactor_id: str = event.get("user", "")

        if not channel or not ts:
            logger.warning("reaction_added event missing channel or ts — skipping.")
            return False

        confirm_emoji: str = (
            self._config.get("confirmation", {}).get("react_with", "white_check_mark")
        )
        emoji: str = event.get("reaction", mapping.get("emoji", ""))

        # ── Idempotency guard (reaction) ──────────────────────────────────────
        if await asyncio.to_thread(self._slack.has_bot_reaction, channel, ts, confirm_emoji):
            logger.debug("Message %s already has confirmation reaction — skipping.", ts)
            return False

        # ── Idempotency guard (DB) ────────────────────────────────────────────
        if self._db is not None and await self._db.is_task_processed(channel, ts, emoji):
            logger.info(
                "Message %s/%s already processed for :%s: (found in DB) — skipping.",
                channel, ts, emoji,
            )
            return False

        # ── Fetch message ─────────────────────────────────────────────────────
        message = await asyncio.to_thread(self._slack.get_message, channel, ts)
        if not message:
            logger.error("Could not fetch message %s in channel %s.", ts, channel)
            return False

        message_text: str = message.get("text", "")
        message_author_id: str = message.get("user", reactor_id)
        thread_ts: str | None = message.get("thread_ts")

        # ── Slack permalink ───────────────────────────────────────────────────
        slack_url = _build_slack_url(channel, ts, thread_ts)

        # ── Reporter (who reacted) ────────────────────────────────────────────
        fields_config = self._config.get("fields", {})
        reporter_info = await asyncio.to_thread(self._slack.get_user_info, reactor_id)

        # ── Channel name ──────────────────────────────────────────────────────
        channel_name = (
            await asyncio.to_thread(self._slack.get_channel_name, channel)
            if fields_config.get("include_channel", True)
            else channel
        )

        # ── Due date ──────────────────────────────────────────────────────────
        due_date: str | None = None
        if fields_config.get("parse_due_date", True) and message_text:
            due_date = parse_due_date(message_text)

        # ── Extract body fields from message text ─────────────────────────────
        body_fields_cfg: list[dict] = fields_config.get("body_fields", [])
        extracted: dict[str, str] = extract_fields(message_text, body_fields_cfg)

        # ── Assignee: reactor_assignees → user_mapper fallback ────────────────
        assignee_notion_id: str | None = _resolve_reactor_assignee(
            reactor_id, mapping
        )
        if assignee_notion_id is None and fields_config.get("include_assignee", True):
            assignee_notion_id = self._user_mapper.slack_to_notion(message_author_id)

        # ── Log all extracted context ──────────────────────────────────────────
        author_info = await asyncio.to_thread(self._slack.get_user_info, message_author_id)
        logger.info(
            "reaction :%s: | reactor: %s (%s) | channel: #%s | "
            "post author: %s (%s) | assignee: %s | due: %s | emoji→db: %s\n"
            "  text: %s",
            mapping.get("emoji", "?"),
            reporter_info["name"], reactor_id,
            channel_name,
            author_info["name"], message_author_id,
            assignee_notion_id or "—",
            due_date or "—",
            mapping.get("notion_db", "?"),
            message_text[:200].replace("\n", " "),
        )

        # ── Build task data ───────────────────────────────────────────────────
        title = await self._derive_title(message_text)
        task_data = TaskData(
            title=title,
            slack_url=slack_url,
            reporter_name=reporter_info["name"],
            assignee_notion_id=assignee_notion_id,
            status=fields_config.get("default_status", "To Do"),
            priority=mapping.get("priority", "Medium"),
            task_type=mapping.get("task_type", "Task"),
            due_date=due_date,
            channel_name=channel_name,
            message_text=message_text,
            extra=extracted,
        )

        # ── Create Notion task ────────────────────────────────────────────────
        notion_db: str = mapping["notion_db"]
        page = await asyncio.to_thread(
            self._task_creator.create_task, notion_db, task_data, mapping
        )
        if page is None:
            return False

        # ── Save to DB ────────────────────────────────────────────────────────
        if self._db is not None:
            await self._db.save_processed_task(
                channel=channel,
                ts=ts,
                emoji=emoji,
                reactor_slack_id=reactor_id,
                reactor_slack_name=reporter_info["name"],
                slack_message_url=slack_url,
                notion_page_id=page.get("id"),
                notion_page_url=page.get("url"),
            )

        # ── Confirm in Slack ──────────────────────────────────────────────────
        await asyncio.to_thread(self._slack.add_reaction, channel, ts, confirm_emoji)

        # No extra idempotency guard: reaction/DB guards above exit before duplicate creation.
        reply_cfg = self._config.get("notion_link_reply", {})
        reply_channels = reply_cfg.get("channels") or []
        if reply_cfg.get("enabled", False) and channel in reply_channels:
            notion_url = page.get("url", "")
            if not notion_url:
                logger.debug(
                    "Notion link reply enabled for %s but page has no URL; skipping.",
                    channel,
                )
            else:
                context = {
                    "notion_url": notion_url,
                    "notion_page_id": page.get("id", ""),
                    "task_title": _escape_slack_mrkdwn(title),
                    "channel_name": channel_name,
                    "channel_id": channel,
                    "reporter_name": reporter_info["name"],
                    "task_type": mapping.get("task_type", ""),
                    "priority": mapping.get("priority", ""),
                    "slack_url": slack_url,
                    "emoji": emoji,
                }
                message_template = reply_cfg.get(
                    "message_template", _DEFAULT_NOTION_LINK_REPLY_TEMPLATE
                )
                text = _render_template(message_template, context)
                anchor = (thread_ts or ts) if reply_cfg.get("in_thread", True) else None
                broadcast = bool(reply_cfg.get("broadcast", False)) and anchor is not None
                await asyncio.to_thread(
                    self._slack.post_message, channel, text, anchor, broadcast
                )
        return True

    # ── Title generation ──────────────────────────────────────────────────────

    async def _derive_title(self, message_text: str) -> str:
        """Return an AI-generated title via Ollama, falling back to first-line text.

        The Ollama call is cosmetic and best-effort: any failure (disabled,
        service down, timeout, empty/bad response) silently falls back to
        :func:`_extract_title` so task creation never breaks and the user never
        sees an error. Slack mrkdwn is stripped before the text reaches the model.
        """
        fallback = _extract_title(message_text)
        if self._ollama is None:
            return fallback

        cleaned = _clean_slack_text(message_text)
        if not cleaned:
            return fallback

        try:
            title = await asyncio.to_thread(
                self._ollama.generate_title, cleaned, self._ollama_title_language
            )
            return title.strip() or fallback
        except Exception:
            logger.debug(
                "Ollama title generation failed — using first-line fallback.",
                exc_info=True,
            )
            return fallback


# ── Helpers ───────────────────────────────────────────────────────────────────

def _escape_slack_mrkdwn(text: str) -> str:
    """Escape text for Slack mrkdwn link labels."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "/")
    )


def _resolve_reactor_assignee(reactor_id: str, mapping: dict) -> str | None:
    """Return assignee Notion user IDs (comma-joined) from per-emoji reactor_assignees.

    Resolution order:
    1. ``mapping["reactor_assignees"][reactor_id]`` — specific reactor entry
    2. ``mapping["reactor_assignees"]["default"]`` — catch-all entry
    3. ``None`` — reactor_assignees not configured; caller falls back to user_mapper

    Returns an empty string ``""`` when a matching entry explicitly has an empty
    ``notion_user_ids`` list (meaning: assign nobody).
    Returns ``None`` when ``reactor_assignees`` is absent from the mapping entirely.
    """
    reactor_assignees: dict | None = mapping.get("reactor_assignees")
    if not reactor_assignees:
        return None  # not configured — caller should use user_mapper fallback

    assignee_cfg: dict | None = reactor_assignees.get(reactor_id) or reactor_assignees.get("default")
    if assignee_cfg is None:
        return ""  # reactor_assignees configured but no match and no default → no assignee

    notion_user_ids: list[str] = assignee_cfg.get("notion_user_ids", [])
    return ",".join(uid for uid in notion_user_ids if uid)


def _clean_slack_text(text: str) -> str:
    """Strip Slack mrkdwn formatting, keeping human-readable content.

    Conversions:
      ``<https://url|label>``  →  ``label``
      ``<https://url>``        →  *(removed)*
      ``<@USERID|name>``       →  ``@name``
      ``<@USERID>``            →  ``@USERID``
      ``<#CHANID|name>``       →  ``#name``
      ``<!here>`` / ``<!channel>`` / ``<!everyone>``  →  ``@here`` etc.
    """
    text = re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", text)   # <URL|label> → label
    text = re.sub(r"<https?://[^>]+>", "", text)                  # <URL> → remove
    text = re.sub(r"<@[A-Z0-9]+\|([^>]+)>", r"@\1", text)        # <@ID|name> → @name
    text = re.sub(r"<@([A-Z0-9]+)>", r"@\1", text)               # <@ID> → @ID
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)        # <#ID|name> → #name
    text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)    # <!here> → @here
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title(text: str, max_length: int = 100) -> str:
    """Return the first *max_length* characters of *text* as the task title.

    Slack mrkdwn formatting (links, user mentions, channel refs) is stripped
    before truncating so the title is always clean, readable text.
    """
    cleaned = _clean_slack_text(text)
    if not cleaned:
        return "Untitled Task"
    if len(cleaned) <= max_length:
        return cleaned
    # Truncate at a word boundary where possible.
    truncated = cleaned[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length // 2:
        truncated = truncated[:last_space]
    return truncated + "…"


def _build_slack_url(channel: str, ts: str, thread_ts: str | None) -> str:
    """Build a Slack deep-link URL to the message."""
    ts_compact = ts.replace(".", "")
    url = f"https://slack.com/archives/{channel}/p{ts_compact}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url
