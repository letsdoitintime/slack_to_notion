"""Thin wrapper around the Slack WebClient with structured return types."""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class SlackClient:
    """Wraps slack_sdk.WebClient with higher-level helpers used by the bot."""

    def __init__(self, bot_token: str) -> None:
        self._client = WebClient(token=bot_token)
        self._bot_user_id: str | None = None

    # ── Messages ──────────────────────────────────────────────────────────────

    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        broadcast: bool = False,
    ) -> bool:
        """Post *text* to Slack. Returns True on success, False on API errors."""
        kwargs = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if broadcast and thread_ts:
            kwargs["reply_broadcast"] = True

        try:
            self._client.chat_postMessage(**kwargs)
            return True
        except SlackApiError as exc:
            error = exc.response.get("error")
            hint = ""
            if error in {"missing_scope", "not_allowed_token_type"}:
                hint = " Add the chat:write bot token scope and reinstall the Slack app."
            logger.warning(
                "Failed to post Slack message to %s (thread_ts=%s): %s.%s",
                channel,
                thread_ts,
                error,
                hint,
            )
            return False

    def get_message(self, channel: str, ts: str) -> dict | None:
        """Fetch the Slack message identified by *channel* + *ts*.

        Handles both top-level messages and replies inside threads:
        if the message has a ``thread_ts`` that differs from its own ``ts``,
        we fetch the full thread and locate the reply by its ``ts``.
        """
        # First, try fetching as a top-level message.
        message = self._fetch_top_level(channel, ts)
        if message is None:
            # conversations_history only returns top-level messages.
            # If the reacted message is a thread reply, fall back to reactions.get
            # which works for both top-level messages and thread replies.
            message = self._fetch_via_reactions(channel, ts)
        if message is None:
            return None

        thread_ts = message.get("thread_ts")
        if thread_ts and thread_ts != ts:
            # The message is a thread reply — re-fetch it from the thread context
            # to get the full reply object with threading metadata.
            reply = self._fetch_thread_reply(channel, thread_ts, ts)
            if reply:
                return reply

        return message

    def _fetch_top_level(self, channel: str, ts: str) -> dict | None:
        try:
            response = self._client.conversations_history(
                channel=channel,
                latest=ts,
                oldest=ts,
                inclusive=True,
                limit=1,
            )
            messages = response.get("messages", [])
            return messages[0] if messages else None
        except SlackApiError as exc:
            logger.error("Failed to fetch message %s in %s: %s", ts, channel, exc)
            return None

    def _fetch_via_reactions(self, channel: str, ts: str) -> dict | None:
        """Fetch a message using reactions.get — works for thread replies too."""
        try:
            response = self._client.reactions_get(channel=channel, timestamp=ts, full=True)
            return response.get("message")
        except SlackApiError as exc:
            logger.error(
                "Failed to fetch message via reactions.get %s in %s: %s", ts, channel, exc
            )
            return None

    def _fetch_thread_reply(
        self, channel: str, thread_ts: str, reply_ts: str
    ) -> dict | None:
        try:
            response = self._client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                inclusive=True,
            )
            for msg in response.get("messages", []):
                if msg.get("ts") == reply_ts:
                    return msg
            return None
        except SlackApiError as exc:
            logger.error(
                "Failed to fetch thread reply %s in %s: %s", reply_ts, channel, exc
            )
            return None

    # ── Users ─────────────────────────────────────────────────────────────────

    def get_user_info(self, user_id: str) -> dict:
        """Return a dict with ``id``, ``name``, and ``email`` for *user_id*.

        Falls back gracefully when the API call fails or the user is not found.
        """
        try:
            response = self._client.users_info(user=user_id)
            user = response["user"]
            profile = user.get("profile", {})
            return {
                "id": user_id,
                "name": user.get("real_name") or user.get("name") or user_id,
                "email": profile.get("email"),
            }
        except SlackApiError as exc:
            logger.warning("Could not fetch user info for %s: %s", user_id, exc)
            return {"id": user_id, "name": user_id, "email": None}

    def is_human(self, user_id: str) -> bool:
        """Return True if *user_id* is a real, active person.

        Excludes bots, apps, Slackbot, and deactivated accounts. On an API
        error we default to True — better to nudge a real person than to
        silently drop them because a lookup hiccuped.
        """
        if user_id == "USLACKBOT":
            return False
        try:
            user = self._client.users_info(user=user_id)["user"]
            return not (user.get("is_bot") or user.get("deleted"))
        except SlackApiError as exc:
            logger.warning(
                "Could not classify user %s (assuming human): %s", user_id, exc
            )
            return True

    def get_bot_user_id(self) -> str:
        """Return the bot's own Slack user ID (cached after the first call)."""
        if self._bot_user_id is None:
            try:
                response = self._client.auth_test()
                self._bot_user_id = response["user_id"]
            except SlackApiError as exc:
                logger.error("auth_test failed: %s", exc)
                self._bot_user_id = ""
        return self._bot_user_id

    # ── Channels ──────────────────────────────────────────────────────────────

    def get_channel_name(self, channel: str) -> str:
        """Return the human-readable channel name, falling back to the ID."""
        try:
            response = self._client.conversations_info(channel=channel)
            return response["channel"].get("name", channel)
        except SlackApiError as exc:
            logger.warning("Could not fetch channel name for %s: %s", channel, exc)
            return channel

    def get_channel_members(self, channel: str) -> list[str] | None:
        """Return all member user IDs of *channel*, following pagination.

        Returns ``None`` when the lookup fails — NOT an empty or partial list.
        Callers use this to decide who has not reacted, and a truncated member
        list is not a smaller answer, it is a wrong one: it silently drops the
        people the request never reached. A missing scope, a rate limit or a
        transient failure must be distinguishable from a genuinely empty channel.

        Requires the ``channels:read`` (public) / ``groups:read`` (private) scope.
        """
        members: list[str] = []
        cursor: str = ""
        try:
            while True:
                kwargs: dict = {"channel": channel, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self._client.conversations_members(**kwargs)
                members.extend(response.get("members", []))
                cursor = response.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    break
        except SlackApiError as exc:
            logger.warning("Could not fetch members for %s: %s", channel, exc)
            return None
        return members

    # ── Reactions ─────────────────────────────────────────────────────────────

    def get_reactors(self, channel: str, ts: str) -> set[str] | None:
        """Return the set of user IDs who reacted with ANY emoji to the message.

        Returns ``None`` when the lookup fails — NOT an empty set. This is the
        dangerous direction: callers subtract reactors from the channel membership
        to find who to nudge, so an empty set from a *failed* call reads as "nobody
        has reacted" and would @mention the entire channel.
        """
        try:
            response = self._client.reactions_get(
                channel=channel, timestamp=ts, full=True
            )
            message = response.get("message", {})
            users: set[str] = set()
            for reaction in message.get("reactions", []):
                users.update(reaction.get("users", []))
            return users
        except SlackApiError as exc:
            logger.warning("Could not fetch reactors for %s: %s", ts, exc)
            return None

    def add_reaction(self, channel: str, ts: str, emoji: str) -> bool:
        """Add *emoji* reaction to a message. Returns True on success or if already
        reacted (idempotent). Returns False on other API errors."""
        try:
            self._client.reactions_add(channel=channel, timestamp=ts, name=emoji)
            return True
        except SlackApiError as exc:
            if exc.response.get("error") == "already_reacted":
                return True  # treat as success — idempotent
            logger.warning("Failed to add reaction '%s' to %s: %s", emoji, ts, exc)
            return False

    def has_bot_reaction(self, channel: str, ts: str, emoji: str) -> bool:
        """Return True if the bot has already added *emoji* to the message.

        Used as an idempotency guard to avoid duplicate task creation.
        """
        bot_user_id = self.get_bot_user_id()
        if not bot_user_id:
            return False
        try:
            response = self._client.reactions_get(
                channel=channel, timestamp=ts, full=True
            )
            message = response.get("message", {})
            for reaction in message.get("reactions", []):
                if reaction["name"] == emoji:
                    return bot_user_id in reaction.get("users", [])
        except SlackApiError as exc:
            logger.warning(
                "Could not check existing reactions on %s: %s", ts, exc
            )
        return False
