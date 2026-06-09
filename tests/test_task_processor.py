"""Tests for TaskProcessor and TaskCreator (with mocked Slack / Notion clients)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.notion.task_creator import (
    TaskCreator,
    TaskData,
    _format_field,
    _make_paragraph_block,
    _make_table_block,
    _make_table_row,
    _render_template,
)
from src.processors.task_processor import (
    TaskProcessor,
    _build_slack_url,
    _clean_slack_text,
    _extract_title,
    _resolve_reactor_assignee,
)
from src.utils.user_mapper import UserMapper


# ── _format_field ────────────────────────────────────────────────────────────

class TestFormatField:
    def test_title(self) -> None:
        result = _format_field("title", "My Task")
        assert result == {"title": [{"text": {"content": "My Task"}}]}

    def test_rich_text(self) -> None:
        result = _format_field("rich_text", "hello")
        assert result == {"rich_text": [{"text": {"content": "hello"}}]}

    def test_url(self) -> None:
        result = _format_field("url", "https://example.com")
        assert result == {"url": "https://example.com"}

    def test_url_empty_returns_none(self) -> None:
        assert _format_field("url", "") is None

    def test_select(self) -> None:
        assert _format_field("select", "High") == {"select": {"name": "High"}}

    def test_select_empty_returns_none(self) -> None:
        assert _format_field("select", "") is None

    def test_multi_select_single(self) -> None:
        assert _format_field("multi_select", "Bug") == {
            "multi_select": [{"name": "Bug"}]
        }

    def test_multi_select_comma_separated(self) -> None:
        result = _format_field("multi_select", "Bug, Feature")
        assert result == {"multi_select": [{"name": "Bug"}, {"name": "Feature"}]}

    def test_multi_select_empty_returns_none(self) -> None:
        assert _format_field("multi_select", "") is None

    def test_status(self) -> None:
        assert _format_field("status", "In Progress") == {
            "status": {"name": "In Progress"}
        }

    def test_date(self) -> None:
        assert _format_field("date", "2027-06-01") == {
            "date": {"start": "2027-06-01"}
        }

    def test_date_empty_returns_none(self) -> None:
        assert _format_field("date", "") is None

    def test_number(self) -> None:
        assert _format_field("number", "42") == {"number": 42.0}

    def test_number_invalid_returns_none(self) -> None:
        assert _format_field("number", "not-a-number") is None

    def test_checkbox_true(self) -> None:
        assert _format_field("checkbox", "true") == {"checkbox": True}

    def test_checkbox_false(self) -> None:
        assert _format_field("checkbox", "0") == {"checkbox": False}

    def test_people_single(self) -> None:
        assert _format_field("people", "uuid-123") == {
            "people": [{"object": "user", "id": "uuid-123"}]
        }

    def test_people_comma_separated(self) -> None:
        result = _format_field("people", "uuid-1, uuid-2")
        assert result == {
            "people": [
                {"object": "user", "id": "uuid-1"},
                {"object": "user", "id": "uuid-2"},
            ]
        }

    def test_people_empty_returns_none(self) -> None:
        assert _format_field("people", "") is None

    def test_unsupported_type_returns_none(self) -> None:
        assert _format_field("formula", "=1+1") is None

    def test_long_text_is_truncated(self) -> None:
        long_text = "x" * 3000
        result = _format_field("title", long_text)
        content = result["title"][0]["text"]["content"]
        assert len(content) == 2000


# ── _clean_slack_text ─────────────────────────────────────────────────────────

class TestCleanSlackText:
    def test_link_with_label_keeps_label(self) -> None:
        text = "added <https://example.com/services/ABC123|My Test App> to channel"
        assert _clean_slack_text(text) == "added My Test App to channel"

    def test_bare_url_is_removed(self) -> None:
        result = _clean_slack_text("see <https://example.com/foo>")
        assert "https://" not in result
        assert result.strip() == "see"

    def test_user_mention_with_name(self) -> None:
        assert _clean_slack_text("hello <@U123|alice>") == "hello @alice"

    def test_user_mention_without_name(self) -> None:
        assert _clean_slack_text("hi <@UABC123>") == "hi @UABC123"

    def test_channel_mention(self) -> None:
        assert _clean_slack_text("posted in <#C123|general>") == "posted in #general"

    def test_here_mention(self) -> None:
        assert _clean_slack_text("<!here> please check") == "@here please check"

    def test_channel_mention_special(self) -> None:
        assert _clean_slack_text("<!channel> heads up") == "@channel heads up"

    def test_multiple_formats_in_one_message(self) -> None:
        text = "added an integration to this channel: <https://claryfi.slack.com/services/B0B3XP19YS0|My Test App>"
        result = _clean_slack_text(text)
        assert result == "added an integration to this channel: My Test App"

    def test_plain_text_unchanged(self) -> None:
        assert _clean_slack_text("just plain text") == "just plain text"

    def test_collapses_extra_whitespace(self) -> None:
        assert _clean_slack_text("  too   many   spaces  ") == "too many spaces"


# ── _extract_title ────────────────────────────────────────────────────────────

class TestExtractTitle:
    def test_short_text_unchanged(self) -> None:
        assert _extract_title("Fix login bug") == "Fix login bug"

    def test_empty_returns_default(self) -> None:
        assert _extract_title("") == "Untitled Task"

    def test_whitespace_only_returns_default(self) -> None:
        assert _extract_title("   ") == "Untitled Task"

    def test_slack_link_stripped_before_truncating(self) -> None:
        text = "added an integration to this channel: <https://claryfi.slack.com/services/B0B3XP19YS0|My Test App>"
        result = _extract_title(text)
        assert result == "added an integration to this channel: My Test App"
        assert "<https://" not in result

    def test_long_text_is_truncated(self) -> None:
        text = "word " * 30  # 150 chars
        result = _extract_title(text, max_length=100)
        assert len(result) <= 101  # +1 for ellipsis char
        assert result.endswith("…")

    def test_truncated_at_word_boundary(self) -> None:
        text = "one two three four five six seven eight nine ten eleven twelve"
        result = _extract_title(text, max_length=20)
        assert "…" in result
        # Should not cut in the middle of a word
        body = result[:-1]  # strip ellipsis
        assert not body.endswith(" ")


# ── _build_slack_url ──────────────────────────────────────────────────────────

class TestBuildSlackUrl:
    def test_top_level_message(self) -> None:
        url = _build_slack_url("C123", "1234567890.123456", None)
        assert url == "https://slack.com/archives/C123/p1234567890123456"

    def test_thread_reply(self) -> None:
        url = _build_slack_url("C123", "111.222", "999.888")
        assert "thread_ts=999.888" in url
        assert "cid=C123" in url


# ── TaskCreator.build_properties ──────────────────────────────────────────────

class TestTaskCreatorBuildProperties:
    def _make_creator(self, notion_fields: dict) -> TaskCreator:
        notion_mock = MagicMock()
        return TaskCreator(notion_mock, {"notion_fields": notion_fields})

    def _sample_task(self) -> TaskData:
        return TaskData(
            title="Fix the login bug",
            slack_url="https://slack.com/archives/C1/p123",
            reporter_name="Alice",
            assignee_notion_id="notion-user-uuid",
            status="To Do",
            priority="High",
            task_type="Task",
            due_date="2027-09-01",
            channel_name="engineering",
            message_text="Fix the login bug by 2027-09-01",
        )

    def test_title_property_built(self) -> None:
        creator = self._make_creator({"Name": {"type": "title", "source": "task_title"}})
        props = creator.build_properties(self._sample_task())
        assert "Name" in props
        assert props["Name"]["title"][0]["text"]["content"] == "Fix the login bug"

    def test_literal_value_used(self) -> None:
        creator = self._make_creator({
            "Name": {"type": "title", "source": "task_title"},
            "Status": {"type": "status", "source": "literal:Not started"},
        })
        props = creator.build_properties(self._sample_task())
        assert props["Status"] == {"status": {"name": "Not started"}}

    def test_empty_optional_field_skipped(self) -> None:
        task = self._sample_task()
        task.due_date = None
        creator = self._make_creator({
            "Name": {"type": "title", "source": "task_title"},
            "Due Date": {"type": "date", "source": "due_date"},
        })
        props = creator.build_properties(task)
        assert "Due Date" not in props

    def test_unconfigured_field_absent(self) -> None:
        # Priority is not in notion_fields — should not appear in output
        creator = self._make_creator({"Name": {"type": "title", "source": "task_title"}})
        props = creator.build_properties(self._sample_task())
        assert "Priority" not in props

    def test_no_notion_fields_returns_name_fallback(self) -> None:
        creator = self._make_creator({})
        props = creator.build_properties(self._sample_task())
        assert "Name" in props
        assert props["Name"]["title"][0]["text"]["content"] == "Fix the login bug"

    def test_missing_title_in_fields_adds_name_fallback(self) -> None:
        # Only a rich_text field — no title type → Name should be auto-added
        creator = self._make_creator(
            {"Reporter": {"type": "rich_text", "source": "reporter_name"}}
        )
        props = creator.build_properties(self._sample_task())
        assert "Name" in props

    def test_people_field_built(self) -> None:
        creator = self._make_creator({
            "Name": {"type": "title", "source": "task_title"},
            "Assignee": {"type": "people", "source": "assignee_notion_id"},
        })
        props = creator.build_properties(self._sample_task())
        assert props["Assignee"] == {
            "people": [{"object": "user", "id": "notion-user-uuid"}]
        }

    def test_url_field_built(self) -> None:
        creator = self._make_creator({
            "Name": {"type": "title", "source": "task_title"},
            "Slack Link": {"type": "url", "source": "slack_url"},
        })
        props = creator.build_properties(self._sample_task())
        assert props["Slack Link"] == {"url": "https://slack.com/archives/C1/p123"}


# ── TaskProcessor.process ─────────────────────────────────────────────────────

class TestTaskProcessorProcess:
    def _make_processor(
        self,
        slack_mock: MagicMock | None = None,
        task_creator_mock: MagicMock | None = None,
        user_mapper: UserMapper | None = None,
        config: dict | None = None,
    ) -> TaskProcessor:
        return TaskProcessor(
            slack=slack_mock or MagicMock(),
            task_creator=task_creator_mock or MagicMock(),
            user_mapper=user_mapper or UserMapper({}),
            config=config or {"confirmation": {"react_with": "white_check_mark"}},
        )

    def _sample_event(self) -> dict:
        return {
            "reaction": "eyes",
            "user": "U_REACTOR",
            "item": {"type": "message", "channel": "C_CHANNEL", "ts": "111.222"},
        }

    def _sample_mapping(self) -> dict:
        return {
            "emoji": "eyes",
            "notion_db": "db-review-id",
            "task_type": "Review",
            "priority": "Medium",
            "processor": "TaskProcessor",
        }

    def _slack_for_success(
        self, text: str = "Review this PR", thread_ts: str | None = None
    ) -> MagicMock:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        message = {"text": text, "user": "U_AUTHOR"}
        if thread_ts is not None:
            message["thread_ts"] = thread_ts
        slack.get_message.return_value = message
        slack.get_user_info.return_value = {
            "id": "U_REACTOR",
            "name": "Alice",
            "email": None,
        }
        slack.get_channel_name.return_value = "engineering"
        slack.post_message.return_value = True
        return slack

    def _task_creator_for_success(self) -> MagicMock:
        task_creator = MagicMock()
        task_creator.create_task.return_value = {
            "id": "p1",
            "url": "https://notion.so/page/123",
        }
        return task_creator

    def _reply_config(self, **overrides: object) -> dict:
        reply_cfg = {
            "enabled": True,
            "channels": ["C_CHANNEL"],
            "message_template": (
                "✅ <{notion_url}|{task_title}> · {task_type} · by {reporter_name}"
            ),
            "in_thread": True,
            "broadcast": False,
        }
        reply_cfg.update(overrides)
        return {
            "confirmation": {"react_with": "white_check_mark"},
            "fields": {"parse_due_date": False},
            "notion_link_reply": reply_cfg,
        }

    async def test_skips_when_already_confirmed(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = True
        processor = self._make_processor(slack_mock=slack)

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is False
        slack.get_message.assert_not_called()

    async def test_skips_when_event_missing_channel(self) -> None:
        event = {"reaction": "eyes", "user": "U1", "item": {"type": "message", "ts": "1.2"}}
        processor = self._make_processor()
        assert await processor.process(event, self._sample_mapping()) is False

    async def test_skips_when_message_not_found(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        slack.get_message.return_value = None
        processor = self._make_processor(slack_mock=slack)

        assert await processor.process(self._sample_event(), self._sample_mapping()) is False

    async def test_returns_false_when_task_creator_fails(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        slack.get_message.return_value = {"text": "Review this PR", "user": "U_AUTHOR"}
        slack.get_user_info.return_value = {"id": "U_REACTOR", "name": "Alice", "email": None}
        slack.get_channel_name.return_value = "engineering"

        task_creator = MagicMock()
        task_creator.create_task.return_value = None  # simulate failure

        processor = self._make_processor(slack_mock=slack, task_creator_mock=task_creator)
        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is False
        slack.add_reaction.assert_not_called()

    async def test_creates_task_and_confirms(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        slack.get_message.return_value = {"text": "Review this PR", "user": "U_AUTHOR"}
        slack.get_user_info.return_value = {"id": "U_REACTOR", "name": "Alice", "email": None}
        slack.get_channel_name.return_value = "engineering"

        task_creator = MagicMock()
        task_creator.create_task.return_value = {"url": "https://notion.so/page/123"}

        processor = self._make_processor(slack_mock=slack, task_creator_mock=task_creator)
        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        task_creator.create_task.assert_called_once()
        slack.add_reaction.assert_called_once_with("C_CHANNEL", "111.222", "white_check_mark")
        slack.post_message.assert_not_called()

    async def test_assignee_mapped_from_user_mapper(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        slack.get_message.return_value = {"text": "Do something", "user": "U_AUTHOR"}
        slack.get_user_info.return_value = {"id": "U_REACTOR", "name": "Bob", "email": None}
        slack.get_channel_name.return_value = "general"

        task_creator = MagicMock()
        task_creator.create_task.return_value = {"url": "https://notion.so/page/456"}

        user_mapper = UserMapper({"U_AUTHOR": "notion-uuid-author"})
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            user_mapper=user_mapper,
        )
        await processor.process(self._sample_event(), self._sample_mapping())

        call_args = task_creator.create_task.call_args
        task_data: TaskData = call_args[0][1]
        assert task_data.assignee_notion_id == "notion-uuid-author"

    async def test_posts_notion_link_reply_for_configured_channel(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.add_reaction.assert_called_once_with(
            "C_CHANNEL", "111.222", "white_check_mark"
        )
        slack.post_message.assert_called_once_with(
            "C_CHANNEL",
            "✅ <https://notion.so/page/123|Review this PR> · Review · by Alice",
            "111.222",
            False,
        )

    async def test_skips_notion_link_reply_for_unlisted_channel(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(channels=["C_OTHER"]),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.add_reaction.assert_called_once()
        slack.post_message.assert_not_called()

    async def test_skips_notion_link_reply_when_disabled(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(enabled=False),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.add_reaction.assert_called_once()
        slack.post_message.assert_not_called()

    async def test_skips_notion_link_reply_when_section_absent(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config={"confirmation": {"react_with": "white_check_mark"}},
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.add_reaction.assert_called_once()
        slack.post_message.assert_not_called()

    async def test_notion_link_reply_uses_parent_thread_anchor(self) -> None:
        slack = self._slack_for_success(thread_ts="999.000")
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.post_message.assert_called_once_with(
            "C_CHANNEL",
            "✅ <https://notion.so/page/123|Review this PR> · Review · by Alice",
            "999.000",
            False,
        )

    async def test_notion_link_reply_broadcasts_with_thread_anchor(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(broadcast=True),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.post_message.assert_called_once_with(
            "C_CHANNEL",
            "✅ <https://notion.so/page/123|Review this PR> · Review · by Alice",
            "111.222",
            True,
        )

    async def test_notion_link_reply_ignores_broadcast_without_thread_anchor(self) -> None:
        slack = self._slack_for_success()
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(in_thread=False, broadcast=True),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.post_message.assert_called_once_with(
            "C_CHANNEL",
            "✅ <https://notion.so/page/123|Review this PR> · Review · by Alice",
            None,
            False,
        )

    async def test_notion_link_reply_failure_does_not_fail_task_creation(self) -> None:
        slack = self._slack_for_success()
        slack.post_message.return_value = False
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.add_reaction.assert_called_once_with(
            "C_CHANNEL", "111.222", "white_check_mark"
        )
        slack.post_message.assert_called_once()

    async def test_notion_link_reply_escapes_task_title_for_slack_link(self) -> None:
        slack = self._slack_for_success("Fix & <unsafe|title>")
        task_creator = self._task_creator_for_success()
        processor = self._make_processor(
            slack_mock=slack,
            task_creator_mock=task_creator,
            config=self._reply_config(),
        )

        result = await processor.process(self._sample_event(), self._sample_mapping())

        assert result is True
        slack.post_message.assert_called_once_with(
            "C_CHANNEL",
            (
                "✅ <https://notion.so/page/123|"
                "Fix &amp; &lt;unsafe/title&gt;> · Review · by Alice"
            ),
            "111.222",
            False,
        )


# ── _resolve_reactor_assignee ─────────────────────────────────────────────────

class TestResolveReactorAssignee:
    def test_returns_none_when_not_configured(self) -> None:
        mapping = {"emoji": "eyes", "notion_db": "db-id", "processor": "TaskProcessor"}
        assert _resolve_reactor_assignee("U_ANY", mapping) is None

    def test_returns_none_for_empty_reactor_assignees(self) -> None:
        mapping = {"reactor_assignees": {}}
        assert _resolve_reactor_assignee("U_ANY", mapping) is None

    def test_returns_ids_for_known_reactor(self) -> None:
        mapping = {
            "reactor_assignees": {
                "U_BOB": {"notion_user_ids": ["uuid-A", "uuid-B"]},
            }
        }
        result = _resolve_reactor_assignee("U_BOB", mapping)
        assert result == "uuid-A,uuid-B"

    def test_returns_empty_string_for_explicit_empty_list(self) -> None:
        mapping = {
            "reactor_assignees": {
                "U_BOB": {"notion_user_ids": []},
            }
        }
        assert _resolve_reactor_assignee("U_BOB", mapping) == ""

    def test_falls_back_to_default(self) -> None:
        mapping = {
            "reactor_assignees": {
                "U_BOB": {"notion_user_ids": ["uuid-bob"]},
                "default": {"notion_user_ids": ["uuid-default"]},
            }
        }
        result = _resolve_reactor_assignee("U_UNKNOWN", mapping)
        assert result == "uuid-default"

    def test_returns_empty_string_when_no_match_and_no_default(self) -> None:
        mapping = {
            "reactor_assignees": {
                "U_BOB": {"notion_user_ids": ["uuid-bob"]},
            }
        }
        result = _resolve_reactor_assignee("U_UNKNOWN", mapping)
        assert result == ""

    async def test_reactor_assignee_takes_priority_over_user_mapper(self) -> None:
        slack = MagicMock()
        slack.has_bot_reaction.return_value = False
        slack.get_message.return_value = {"text": "Do something", "user": "U_AUTHOR"}
        slack.get_user_info.return_value = {"id": "U_REACTOR", "name": "Alice", "email": None}
        slack.get_channel_name.return_value = "general"

        task_creator = MagicMock()
        task_creator.create_task.return_value = {"url": "https://notion.so/page/789"}

        user_mapper = UserMapper({"U_AUTHOR": "notion-author-uuid"})
        config = {"confirmation": {"react_with": "white_check_mark"}}
        processor = TaskProcessor(
            slack=slack, task_creator=task_creator, user_mapper=user_mapper, config=config
        )

        mapping = {
            "emoji": "eyes",
            "notion_db": "db-review-id",
            "task_type": "Review",
            "priority": "Medium",
            "processor": "TaskProcessor",
            "reactor_assignees": {
                "U_REACTOR": {"notion_user_ids": ["notion-reactor-uuid"]},
            },
        }
        event = {
            "reaction": "eyes",
            "user": "U_REACTOR",
            "item": {"type": "message", "channel": "C_CHANNEL", "ts": "111.222"},
        }
        await processor.process(event, mapping)

        call_args = task_creator.create_task.call_args
        task_data: TaskData = call_args[0][1]
        assert task_data.assignee_notion_id == "notion-reactor-uuid"


# ── Title template & body blocks ──────────────────────────────────────────────

class TestRenderTemplate:
    def test_renders_known_key(self) -> None:
        assert _render_template("{reporter_name}", {"reporter_name": "Alice"}) == "Alice"

    def test_missing_key_becomes_empty_string(self) -> None:
        assert _render_template("{missing_key}", {}) == ""

    def test_mixed_known_and_missing(self) -> None:
        result = _render_template("[{task_type}] {task_title}", {"task_type": "Task"})
        assert result == "[Task] "


class TestTitleTemplate:
    def _make_creator(self, fields_config: dict) -> TaskCreator:
        return TaskCreator(MagicMock(), fields_config)

    def _sample_task(self) -> TaskData:
        return TaskData(
            title="Raw title",
            slack_url="https://slack.com/archives/C1/p123",
            reporter_name="Alice",
            assignee_notion_id=None,
            status="To Do",
            priority="High",
            task_type="Task",
            due_date=None,
            channel_name="engineering",
            message_text="Raw title",
        )

    def test_global_template_applied(self) -> None:
        creator = self._make_creator({
            "task_title_template": "[{task_type}] {task_title}",
            "notion_fields": {"Name": {"type": "title", "source": "task_title"}},
        })
        props = creator.build_properties(self._sample_task())
        assert props["Name"]["title"][0]["text"]["content"] == "[Task] Raw title"

    def test_per_emoji_template_overrides_global(self) -> None:
        creator = self._make_creator({
            "task_title_template": "GLOBAL: {task_title}",
            "notion_fields": {"Name": {"type": "title", "source": "task_title"}},
        })
        mapping = {"task_title_template": "EMOJI: {task_title}"}
        props = creator.build_properties(self._sample_task(), mapping=mapping)
        assert props["Name"]["title"][0]["text"]["content"] == "EMOJI: Raw title"

    def test_no_template_uses_raw_title(self) -> None:
        creator = self._make_creator({
            "notion_fields": {"Name": {"type": "title", "source": "task_title"}},
        })
        props = creator.build_properties(self._sample_task())
        assert props["Name"]["title"][0]["text"]["content"] == "Raw title"


class TestBuildBodyBlocks:
    def _make_creator(self, fields_config: dict) -> TaskCreator:
        return TaskCreator(MagicMock(), fields_config)

    def _sample_task(self) -> TaskData:
        return TaskData(
            title="My Task",
            slack_url="https://slack.com/archives/C1/p123",
            reporter_name="Bob",
            assignee_notion_id=None,
            status="To Do",
            priority="Medium",
            task_type="Review",
            due_date=None,
            channel_name="ops",
            message_text="Look at this",
        )

    def test_no_config_returns_empty_list(self) -> None:
        creator = self._make_creator({})
        assert creator._build_body_blocks(self._sample_task()) == []

    def test_header_template_produces_paragraph_block(self) -> None:
        creator = self._make_creator({
            "body_header_template": "Reporter: {reporter_name}",
        })
        blocks = creator._build_body_blocks(self._sample_task())
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"
        assert "Bob" in blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]

    def test_body_fields_produce_table_block(self) -> None:
        creator = self._make_creator({
            "body_fields": [
                {"key": "reporter_name", "label": "Reporter"},
                {"key": "priority", "label": "Priority"},
            ],
        })
        blocks = creator._build_body_blocks(self._sample_task())
        assert len(blocks) == 1
        table = blocks[0]
        assert table["type"] == "table"
        rows = table["table"]["children"]
        # header row + 2 data rows
        assert len(rows) == 3
        header_cells = rows[0]["table_row"]["cells"]
        assert header_cells[0][0]["text"]["content"] == "Field"
        assert header_cells[1][0]["text"]["content"] == "Value"

    def test_body_fields_values_from_context(self) -> None:
        creator = self._make_creator({
            "body_fields": [{"key": "reporter_name", "label": "Reporter"}],
        })
        blocks = creator._build_body_blocks(self._sample_task())
        row = blocks[0]["table"]["children"][1]  # first data row
        assert row["table_row"]["cells"][0][0]["text"]["content"] == "Reporter"
        assert row["table_row"]["cells"][1][0]["text"]["content"] == "Bob"

    def test_per_emoji_header_overrides_global(self) -> None:
        creator = self._make_creator({
            "body_header_template": "GLOBAL: {reporter_name}",
        })
        mapping = {"body_header_template": "EMOJI: {reporter_name}"}
        blocks = creator._build_body_blocks(self._sample_task(), mapping=mapping)
        content = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
        assert content.startswith("EMOJI:")

    def test_extra_fields_available_in_body(self) -> None:
        task = self._sample_task()
        task.extra = {"channel_name": "custom-channel"}
        creator = self._make_creator({
            "body_fields": [{"key": "channel_name", "label": "Channel"}],
        })
        blocks = creator._build_body_blocks(task)
        row = blocks[0]["table"]["children"][1]
        assert row["table_row"]["cells"][1][0]["text"]["content"] == "custom-channel"


class TestNotionBlockHelpers:
    def test_make_paragraph_block_structure(self) -> None:
        block = _make_paragraph_block("Hello world")
        assert block["type"] == "paragraph"
        assert block["paragraph"]["rich_text"][0]["text"]["content"] == "Hello world"

    def test_make_paragraph_block_long_text_chunked(self) -> None:
        text = "x" * 4500
        block = _make_paragraph_block(text)
        total = sum(len(rt["text"]["content"]) for rt in block["paragraph"]["rich_text"])
        assert total == 4500
        assert len(block["paragraph"]["rich_text"]) == 3  # 2000 + 2000 + 500

    def test_make_paragraph_block_linkifies_bare_url(self) -> None:
        block = _make_paragraph_block("see https://slack.com/archives/C1/p123 now")
        rich = block["paragraph"]["rich_text"]
        link_seg = next(r for r in rich if r["text"].get("link"))
        assert link_seg["text"]["content"] == "https://slack.com/archives/C1/p123"
        assert link_seg["text"]["link"] == {"url": "https://slack.com/archives/C1/p123"}

    def test_make_paragraph_block_unwraps_slack_angle_link(self) -> None:
        block = _make_paragraph_block("<https://techcore.atlassian.net/browse/PG-5841>")
        rich = block["paragraph"]["rich_text"]
        assert len(rich) == 1
        assert rich[0]["text"]["content"] == "https://techcore.atlassian.net/browse/PG-5841"
        assert rich[0]["text"]["link"] == {"url": "https://techcore.atlassian.net/browse/PG-5841"}
        # No stray angle brackets leaked into the page.
        assert "<" not in rich[0]["text"]["content"]
        assert ">" not in rich[0]["text"]["content"]

    def test_make_paragraph_block_labeled_link(self) -> None:
        block = _make_paragraph_block("<https://example.com|click here>")
        seg = block["paragraph"]["rich_text"][0]
        assert seg["text"]["content"] == "click here"
        assert seg["text"]["link"] == {"url": "https://example.com"}

    def test_make_paragraph_block_unwraps_user_mention(self) -> None:
        block = _make_paragraph_block("<@U0ALD6BT39D> ping")
        text = "".join(r["text"]["content"] for r in block["paragraph"]["rich_text"])
        assert text == "@U0ALD6BT39D ping"
        assert "<" not in text and ">" not in text

    def test_make_paragraph_block_unwraps_channel_mention(self) -> None:
        block = _make_paragraph_block("<#C123|internal-payments>")
        assert block["paragraph"]["rich_text"][0]["text"]["content"] == "#internal-payments"

    def test_make_paragraph_block_full_body_scenario(self) -> None:
        """End-to-end body from the screenshot: no stray <>, both URLs clickable."""
        body = (
            "Reporter: Andrii Ka | Channel: #internal-payments\n"
            "<https://techcore.atlassian.net/browse/PG-5841>\n"
            "<@U0ALD6BT39D> обновишь описание согласно описанию?\n\n"
            "https://slack.com/archives/C05LY9SDVRP/p1780919338458349"
        )
        rich = _make_paragraph_block(body)["paragraph"]["rich_text"]
        full = "".join(r["text"]["content"] for r in rich)
        assert "<" not in full and ">" not in full
        links = {r["text"]["link"]["url"] for r in rich if r["text"].get("link")}
        assert links == {
            "https://techcore.atlassian.net/browse/PG-5841",
            "https://slack.com/archives/C05LY9SDVRP/p1780919338458349",
        }

    def test_make_paragraph_block_plain_text_has_no_link_key(self) -> None:
        block = _make_paragraph_block("Reporter: Bob")
        assert "link" not in block["paragraph"]["rich_text"][0]["text"]

    def test_make_paragraph_block_preserves_non_slack_angle_brackets(self) -> None:
        # Genuine user-typed angle brackets must not be eaten by the unwrapper.
        block = _make_paragraph_block("For x<y> and List<T> the result")
        text = "".join(r["text"]["content"] for r in block["paragraph"]["rich_text"])
        assert text == "For x<y> and List<T> the result"

    def test_make_paragraph_block_mailto_is_plain_text(self) -> None:
        block = _make_paragraph_block("<mailto:a@b.com|Email me>")
        seg = block["paragraph"]["rich_text"][0]
        assert seg["text"]["content"] == "Email me"
        assert "link" not in seg["text"]  # no non-http(s) link.url for Notion

    def test_make_paragraph_block_tel_entity_is_plain_text(self) -> None:
        block = _make_paragraph_block("<tel:+1-555-0123|Call us>")
        seg = block["paragraph"]["rich_text"][0]
        assert seg["text"]["content"] == "Call us"
        assert "link" not in seg["text"]

    def test_make_paragraph_block_caps_at_100_rich_text(self) -> None:
        body = " ".join(f"https://example.com/{i}" for i in range(150))
        rich = _make_paragraph_block(body)["paragraph"]["rich_text"]
        assert len(rich) <= 100
        assert all(len(r["text"]["content"]) <= 2000 for r in rich)

    def test_make_table_row_structure(self) -> None:
        row = _make_table_row("Field", "Value")
        assert row["type"] == "table_row"
        cells = row["table_row"]["cells"]
        assert cells[0][0]["text"]["content"] == "Field"
        assert cells[1][0]["text"]["content"] == "Value"

    def test_make_table_block_structure(self) -> None:
        rows = [_make_table_row("F", "V")]
        table = _make_table_block(rows)
        assert table["type"] == "table"
        assert table["table"]["table_width"] == 2
        assert table["table"]["has_column_header"] is True
        assert len(table["table"]["children"]) == 1
