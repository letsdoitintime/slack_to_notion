"""Bidirectional mapping between Slack user IDs and Notion user UUIDs."""


class UserMapper:
    """Maps Slack user IDs to Notion user UUIDs as configured in config.yaml."""

    def __init__(self, users_config: dict[str, str]) -> None:
        # users_config format: {"SLACK_USER_ID": "notion-user-uuid", ...}
        self._slack_to_notion: dict[str, str] = users_config or {}

    def slack_to_notion(self, slack_user_id: str) -> str | None:
        """Return the Notion user UUID for a given Slack user ID, or None if unmapped."""
        return self._slack_to_notion.get(slack_user_id)

    def has_mapping(self, slack_user_id: str) -> bool:
        return slack_user_id in self._slack_to_notion
