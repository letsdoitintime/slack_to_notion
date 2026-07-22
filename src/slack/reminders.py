"""Emoji-triggered reminders: ping channel members who haven't reacted yet.

Flow
----
1. A configured trigger emoji (e.g. :hmm_parrot:) is added to a message in a
   watched channel → ``schedule_for_event`` writes one reminder row per
   configured delay to the DB (survives a restart).
2. ``run_reminder_loop`` polls the DB every ``poll_seconds``. For each due
   reminder it re-computes, *at send time*, who still hasn't reacted
   (channel members − anyone who reacted with any emoji − the poster − the bot),
   then keeps only real, active people (bots, apps, and deactivated accounts are
   dropped) and posts an in-thread message @mentioning them.

Config shape (top-level ``reaction_reminders``, a list of rules)::

    reaction_reminders:
      - channels: ["C0123456"]
        trigger_emoji: "hmm_parrot"
        message_template: "Still waiting on {mentions} to weigh in."
        reminders:
          - after_minutes: 60
          - after_minutes: 180
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..db.database import DatabaseManager
from .client import SlackClient

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_TEMPLATE = "Reminder: {mentions} haven't reacted yet."
_POLL_SECONDS = 60
# Retry a failing reminder on this many poll cycles before giving up, so a
# permanently broken one (e.g. bot removed from the channel) can't loop forever.
_MAX_ATTEMPTS = 5


def compute_non_reactors(
    members: list[str], reactors: set[str], exclude: list[str]
) -> list[str]:
    """Members who did not react, minus *exclude*, in member order (deduped)."""
    excluded = set(reactors) | {e for e in exclude if e}
    seen: set[str] = set()
    result: list[str] = []
    for m in members:
        if m in excluded or m in seen:
            continue
        seen.add(m)
        result.append(m)
    return result


def _match_rule(config: dict, channel: str, emoji: str) -> dict | None:
    """Return the first reaction_reminders rule matching this channel + emoji."""
    for rule in config.get("reaction_reminders") or []:
        if emoji == rule.get("trigger_emoji") and channel in (rule.get("channels") or []):
            return rule
    return None


async def schedule_for_event(
    event: dict, config: dict, db: DatabaseManager
) -> None:
    """If a ``reaction_added`` event matches a reminder rule, persist its reminders."""
    emoji: str = event.get("reaction", "")
    item: dict = event.get("item", {})
    channel: str = item.get("channel", "")
    ts: str = item.get("ts", "")
    if not channel or not ts:
        return

    rule = _match_rule(config, channel, emoji)
    if rule is None:
        return

    template = rule.get("message_template") or DEFAULT_REMINDER_TEMPLATE
    now = datetime.now(timezone.utc)
    rows: list[tuple[float, str]] = []
    for reminder in rule.get("reminders") or []:
        minutes = reminder.get("after_minutes")
        if minutes is None:
            continue
        remind_at = (now + timedelta(minutes=float(minutes))).isoformat()
        rows.append((float(minutes), remind_at))
    if not rows:
        return

    await db.schedule_reminders(
        channel=channel,
        ts=ts,
        trigger_emoji=emoji,
        message_template=template,
        rows=rows,
    )
    logger.info(
        "Scheduled %d reminder(s) for :%s: on %s/%s.", len(rows), emoji, channel, ts
    )


async def _send_one(slack: SlackClient, reminder: dict) -> bool:
    """Post one due reminder, re-computing non-reactors right now.

    Returns True when handled (posted, or nothing to send), False when the Slack
    post failed and the reminder should be retried on a later poll cycle.
    """
    channel: str = reminder["slack_channel"]
    ts: str = reminder["slack_ts"]

    members = await asyncio.to_thread(slack.get_channel_members, channel)
    reactors = await asyncio.to_thread(slack.get_reactors, channel, ts)
    # Both return None on a failed lookup, which is not the same as "empty" and
    # must not be treated as data. An unknown member list would silently drop the
    # reminder; an unknown reactor set would read as "nobody reacted" and mention
    # the whole channel. Neither is recoverable by guessing — retry instead.
    if members is None or reactors is None:
        logger.warning(
            "Reminder %s: Slack lookup failed (members=%s, reactors=%s) — "
            "retrying next cycle rather than guessing.",
            reminder["id"],
            "ok" if members is not None else "FAILED",
            "ok" if reactors is not None else "FAILED",
        )
        return False

    message = await asyncio.to_thread(slack.get_message, channel, ts)
    bot_id = await asyncio.to_thread(slack.get_bot_user_id)
    poster = message.get("user") if message else None

    non_reactors = compute_non_reactors(members, reactors, [poster or "", bot_id])
    # Ping real people only — filter out bots/apps/deactivated accounts. We
    # classify only the non-reactor candidates (small set), not every member.
    humans = [
        u for u in non_reactors if await asyncio.to_thread(slack.is_human, u)
    ]
    if not humans:
        logger.info(
            "Reminder %s: no human non-reactors left — nothing to send.", reminder["id"]
        )
        return True  # handled — nothing to do, do not retry

    mentions = " ".join(f"<@{u}>" for u in humans)
    template = reminder["message_template"] or DEFAULT_REMINDER_TEMPLATE
    text = template.replace("{mentions}", mentions)
    thread_ts = (message.get("thread_ts") if message else None) or ts

    ok = await asyncio.to_thread(slack.post_message, channel, text, thread_ts)
    if ok:
        logger.info(
            "Reminder %s: pinged %d human non-reactor(s).", reminder["id"], len(humans)
        )
    else:
        logger.warning(
            "Reminder %s: Slack post failed — will retry next cycle.", reminder["id"]
        )
    return ok


async def _fire_due(slack: SlackClient, db: DatabaseManager) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    for reminder in await db.due_reminders(now_iso):
        try:
            handled = await _send_one(slack, reminder)
        except Exception:
            logger.exception("Failed to send reminder id=%s.", reminder["id"])
            handled = False

        if handled:
            await db.mark_reminder_sent(reminder["id"])
            continue

        # Failed — leave it unsent so it retries next cycle, but give up after
        # _MAX_ATTEMPTS so a permanently broken reminder can't loop forever.
        attempts = await db.bump_reminder_attempt(reminder["id"])
        if attempts >= _MAX_ATTEMPTS:
            logger.error(
                "Reminder %s failed %d times — giving up.", reminder["id"], attempts
            )
            await db.mark_reminder_sent(reminder["id"])


async def run_reminder_loop(
    config: dict,
    slack: SlackClient,
    db: DatabaseManager,
    poll_seconds: int = _POLL_SECONDS,
) -> None:
    """Poll the DB for due reminders forever. No-op if the feature is unconfigured.

    Cancel the returned task at shutdown; the sleep makes cancellation prompt.
    """
    if not (config.get("reaction_reminders")):
        logger.info("No reaction_reminders configured — reminder loop not started.")
        return
    logger.info("Reaction-reminder loop started (poll every %ds).", poll_seconds)
    while True:
        try:
            await _fire_due(slack, db)
        except Exception:
            logger.exception("Reminder loop iteration failed — continuing.")
        await asyncio.sleep(poll_seconds)
