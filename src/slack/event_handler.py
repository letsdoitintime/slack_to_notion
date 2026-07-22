"""Registers Slack event handlers with the slack_bolt AsyncApp."""

from __future__ import annotations

import asyncio
import logging

from slack_bolt.async_app import AsyncApp

from ..db.database import DatabaseManager
from ..processors.base import BaseProcessor
from ..slack.client import SlackClient
from . import reminders

logger = logging.getLogger(__name__)


def register_handlers(
    app: AsyncApp,
    config: dict,
    processors: dict[str, BaseProcessor],
    *,
    slack_client: SlackClient | None = None,
    db: DatabaseManager | None = None,
) -> None:
    """Attach all event handlers to *app*.

    Args:
        app:          The slack_bolt ``AsyncApp`` instance.
        config:       Fully resolved config dict (from ``load_config``).
        processors:   Map of processor name → processor instance.
        slack_client: Optional Slack client for enriching saved messages.
        db:           Optional database manager for message persistence.
    """
    # Build a fast lookup: emoji name → emoji_mappings entry.
    emoji_map: dict[str, dict] = {
        m["emoji"]: m for m in config.get("emoji_mappings", [])
    }
    if not emoji_map:
        logger.warning("No emoji_mappings configured — the bot will not react to anything.")

    @app.event("message")
    async def handle_message_events(body: dict, event: dict) -> None:  # type: ignore[override]
        # NOTE: we intentionally omit the `logger` parameter here so that we use
        # the module-level logger (INFO) rather than the Bolt-injected child of
        # `slack_bolt` (which main.py sets to WARNING, suppressing all INFO logs).
        subtype: str | None = event.get("subtype")
        channel: str = event.get("channel", "?")
        ts: str = event.get("ts", "?")
        actor: str = event.get("user") or event.get("bot_id") or "?"

        logger.info(
            "Message event — channel: %s | ts: %s | subtype: %s | actor: %s",
            channel, ts, subtype or "(none)", actor,
        )

        if db is None:
            logger.debug("DB not configured — skipping message save.")
            return

        try:
            # Edits and deletes — forward to DB.
            if subtype in ("message_changed", "message_deleted"):
                logger.info("Persisting %s to DB — channel: %s | ts: %s.", subtype, channel, ts)
                await db.save_message(event)
                logger.info("%s persisted.", subtype)
                return

            # Skip bot-originated messages.
            if event.get("bot_id") or subtype == "bot_message":
                logger.debug("Skipping bot message — channel: %s | ts: %s.", channel, ts)
                return

            # New message (subtype None or "file_share") — enrich with resolved names.
            user_id: str | None = event.get("user")
            channel_name: str | None = None
            user_name: str | None = None
            user_email: str | None = None

            if slack_client is not None:
                try:
                    channel_name = await asyncio.to_thread(slack_client.get_channel_name, channel)
                except Exception as exc:
                    logger.warning("Could not resolve channel name for %s: %s", channel, exc)
                if user_id:
                    try:
                        user_info = await asyncio.to_thread(slack_client.get_user_info, user_id)
                        user_name = user_info.get("name")
                        user_email = user_info.get("email")
                    except Exception as exc:
                        logger.warning("Could not resolve user info for %s: %s", user_id, exc)

            logger.info(
                "Saving new message to DB — channel: #%s (%s) | user: %s (%s) | ts: %s",
                channel_name or "?", channel, user_name or user_id or "?", user_id, ts,
            )
            await db.save_message(
                event,
                channel_name=channel_name,
                user_name=user_name,
                user_email=user_email,
            )
            logger.info("Message saved to DB — ts: %s.", ts)

        except Exception:
            logger.exception(
                "Unhandled error saving message to DB — channel: %s | ts: %s.", channel, ts
            )

    allowed_reactors: list[str] = config.get("allowed_reactors", []) or []

    reminders_enabled = bool(config.get("reaction_reminders")) and db is not None

    @app.event("reaction_added")
    async def handle_reaction_added(event: dict) -> None:  # type: ignore[override]
        reaction: str = event.get("reaction", "")

        # The allowlist gates EVERY action this handler can take, reminders
        # included, so it is checked FIRST.
        #
        # It used to sit below the `not mapping` early-return, which made it
        # unreachable for reminders in the normal setup: the reminder trigger
        # emoji is deliberately not in emoji_mappings, so the handler returned
        # before the allowlist was ever consulted, and any channel member could
        # make the bot post in-thread @mention nudges. Scheduling a reminder is
        # the bot acting on someone's behalf exactly as processing an emoji is.
        if allowed_reactors:
            reactor_id: str = event.get("user", "")
            if reactor_id not in allowed_reactors:
                logger.debug(
                    "Ignoring :%s: from user %s — not in allowed_reactors.",
                    reaction,
                    reactor_id,
                )
                return

        # Reminders run independently of emoji_mappings — the trigger emoji is
        # usually NOT a processor emoji, so this must happen before the
        # `not mapping` early-return below. Bolt stops after the first matching
        # reaction_added listener, so both features share this one.
        if reminders_enabled:
            try:
                await reminders.schedule_for_event(event, config, db)
            except Exception:
                logger.exception("Failed to schedule reaction reminder.")

        mapping = emoji_map.get(reaction)
        if not mapping:
            logger.debug("Ignoring unhandled reaction: :%s:", reaction)
            return

        processor_name: str = mapping.get("processor", "TaskProcessor")
        processor = processors.get(processor_name)
        if processor is None:
            logger.error(
                "Processor '%s' is not registered. "
                "Check PROCESSOR_REGISTRY in src/processors/__init__.py.",
                processor_name,
            )
            return

        channel = event.get("item", {}).get("channel", "?")
        logger.info(
            "Handling :%s: in channel %s with processor '%s'.",
            reaction,
            channel,
            processor_name,
        )

        try:
            success = await processor.process(event, mapping)
            if success:
                logger.info("Successfully processed :%s: in channel %s.", reaction, channel)
            else:
                logger.warning(
                    "Processor '%s' returned False for :%s: — task may not have been created.",
                    processor_name,
                    reaction,
                )
        except Exception:
            logger.exception(
                "Unhandled exception in processor '%s' for reaction :%s:.",
                processor_name,
                reaction,
            )

