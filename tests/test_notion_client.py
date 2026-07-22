"""Wire-contract tests for the Notion client.

These deliberately assert what goes over the HTTP wire, not the Python surface.
notion-client's method signatures are identical across 2.x and 3.x while the
default ``Notion-Version`` header moves (2022-06-28 → 2025-09-03), and that
header decides whether ``pages.create`` with a ``database_id`` parent is
accepted. A green suite that only exercises the Python API cannot see that.
"""

from unittest.mock import patch

from src.notion.client import NOTION_API_VERSION, NotionClient


class TestWireContract:
    def test_notion_version_header_is_pinned(self):
        """The pinned API version must reach the actual request headers.

        Built through httpx's own request builder so this reflects the merged
        headers really sent, not an attribute we hope is used.
        """
        notion = NotionClient("xoxb-fake")
        request = notion._client.client.build_request("POST", "pages")
        assert request.headers["Notion-Version"] == NOTION_API_VERSION

    def test_pinned_version_does_not_drift_silently(self):
        """Guard the constant itself — changing it changes the live wire contract."""
        assert NOTION_API_VERSION == "2025-09-03"

    def test_auth_header_still_bearer(self):
        notion = NotionClient("xoxb-fake")
        request = notion._client.client.build_request("POST", "pages")
        assert request.headers["Authorization"] == "Bearer xoxb-fake"


class TestCreatePage:
    def test_sends_database_id_parent(self):
        """The parent shape is coupled to the pinned version — assert it explicitly."""
        notion = NotionClient("xoxb-fake")
        with patch.object(notion._client.pages, "create", return_value={"id": "p1"}) as create:
            notion.create_page("db-123", {"Name": {"title": []}})
        assert create.call_args.kwargs["parent"] == {"database_id": "db-123"}

    def test_omits_children_when_empty(self):
        notion = NotionClient("xoxb-fake")
        with patch.object(notion._client.pages, "create", return_value={"id": "p1"}) as create:
            notion.create_page("db-123", {}, children=[])
        assert "children" not in create.call_args.kwargs

    def test_passes_children_through(self):
        notion = NotionClient("xoxb-fake")
        blocks = [{"object": "block", "type": "paragraph"}]
        with patch.object(notion._client.pages, "create", return_value={"id": "p1"}) as create:
            notion.create_page("db-123", {}, children=blocks)
        assert create.call_args.kwargs["children"] == blocks
