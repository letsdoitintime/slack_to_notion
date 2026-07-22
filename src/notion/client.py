"""Thin wrapper around the official Notion Python client."""

from __future__ import annotations

from notion_client import Client

# Pin the wire contract explicitly rather than inheriting notion-client's default.
# The default moves with the library: notion-client 2.x sends 2022-06-28, 3.x sends
# 2025-09-03. Under 2025-09-03 a database is a container of *data sources*, and
# `parent: {"database_id": ...}` below is only accepted as a compatibility shim for
# single-data-source databases — so the header is not cosmetic, it decides whether
# create_page works at all. Leaving it to the library means two installs from the
# same requirements.txt can talk different APIs. Verified against the live API on
# 2026-07-22; pinned by tests/test_notion_client.py.
NOTION_API_VERSION = "2025-09-03"


class NotionClient:
    """Wraps notion_client.Client with higher-level helpers."""

    def __init__(self, token: str) -> None:
        self._client = Client(auth=token, notion_version=NOTION_API_VERSION)

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
