"""Thin wrapper around the official Notion Python client."""

from __future__ import annotations

import logging

from notion_client import Client
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)


class NotionClient:
    """Wraps notion_client.Client with higher-level helpers."""

    def __init__(self, token: str) -> None:
        self._client = Client(auth=token)

    def create_page(
        self,
        database_id: str,
        properties: dict,
        children: list | None = None,
    ) -> dict:
        """Create a new page inside *database_id* with the given *properties*.

        Returns the created page dict, or raises on failure so the caller can handle it.
        """
        payload: dict = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return self._client.pages.create(**payload)

    def get_database_schema(self, database_id: str) -> dict[str, dict]:
        """Return the property schema of *database_id*.

        Returns an empty dict when the database cannot be retrieved.
        """
        try:
            db = self._client.databases.retrieve(database_id=database_id)
            return db.get("properties", {})
        except Exception as exc:
            logger.error(
                "Failed to retrieve Notion database schema for %s: %s",
                database_id,
                exc,
            )
            return {}
