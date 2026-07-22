"""Cross-version contract tests for the Slack transport stack.

These exist because the rest of the suite cannot see what they check:

* `tests/test_reminders.py` drives the `reaction_added` handler through a
  `_FakeApp` that only collects functions in a list. It therefore proves nothing
  about **Bolt's own dispatch**, and the whole reminders design rests on one
  claim about that dispatch — that Bolt stops after the first matching listener,
  which is why reminders and emoji processing had to be merged into a single
  handler. Until now that claim lived in a docstring and was never executed.
* The `SlackClient` error paths are driven by `SlackApiError.response.get(...)`.
  A change in what `response` *is* would turn the `already_reacted` idempotency
  guard into a silent failure, not a loud one.
* `aiohttp` is never imported by `src/`, so nothing in the suite touches it — yet
  it is what Bolt's async app and socket-mode transport run on.

Every test here must pass on the OLD and NEW versions of these packages. One that
only passes on the new versions snapshots the present; one that passes on both
guards the next upgrade.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch
from urllib.parse import parse_qs

import aiohttp
import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse
from slack_sdk.web.base_client import BaseClient

from src.slack.client import SlackClient


async def _fake_authorize(**_kwargs) -> AuthorizeResult:
    """Stand in for Bolt's auth.test call.

    Without this, Bolt's default single-team authorization middleware makes a real
    HTTPS request to slack.com during dispatch and swallows the event when it fails.
    Supplying `authorize` keeps the test offline while still running the genuine
    middleware chain and listener matcher — which is the part under test.
    """
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-not-a-real-token",
        bot_id="B1",
        bot_user_id="U_BOT",
    )


def _make_app() -> AsyncApp:
    """A real AsyncApp that never talks to Slack."""
    return AsyncApp(
        signing_secret="not-a-real-secret",
        authorize=_fake_authorize,
        request_verification_enabled=False,
        ssl_check_enabled=False,
        url_verification_enabled=False,
    )


def _reaction_event_request() -> AsyncBoltRequest:
    return AsyncBoltRequest(
        body={
            "token": "t",
            "team_id": "T1",
            "api_app_id": "A1",
            "type": "event_callback",
            "event_id": "Ev1",
            "event_time": 1,
            "event": {
                "type": "reaction_added",
                "user": "U1",
                "reaction": "hmm_parrot",
                "item": {"type": "message", "channel": "C1", "ts": "1.2"},
                "event_ts": "1.2",
            },
        },
        mode="socket_mode",
    )


# Bolt runs async listeners as detached background tasks: `async_dispatch` returns
# before a listener body has run. Tests therefore wait on a signal the listener sets
# rather than asserting straight after dispatch.
# ponytail: fixed settle window for the negative case; if this ever goes flaky the
# upgrade path is to hook the listener runner instead of waiting on wall-clock.
_SETTLE_SECONDS = 0.25


class TestBoltDispatch:
    """The assumption the reminders feature is built on."""

    @pytest.mark.asyncio
    async def test_only_the_first_matching_listener_runs(self) -> None:
        """Bolt stops after the first matching `reaction_added` listener.

        This is why reminder scheduling lives *inside* the emoji handler rather
        than in a second listener. If a Bolt upgrade ever starts running both,
        this fails and the merge becomes unnecessary; if it keeps stopping at the
        first, a second listener added later would be silently dead code.
        """
        app = _make_app()
        fired: list[str] = []
        first_ran = asyncio.Event()

        @app.event("reaction_added")
        async def first(event: dict) -> None:
            fired.append("first")
            first_ran.set()

        @app.event("reaction_added")
        async def second(event: dict) -> None:
            fired.append("second")

        await app.async_dispatch(_reaction_event_request())
        await asyncio.wait_for(first_ran.wait(), timeout=5)
        # Give a second listener a real chance to run before asserting it didn't.
        await asyncio.sleep(_SETTLE_SECONDS)

        assert fired == ["first"], (
            "Bolt dispatch changed: a second reaction_added listener now runs. "
            "src/slack/event_handler.py merges reminders into the first listener "
            "specifically because it did not."
        )

    @pytest.mark.asyncio
    async def test_the_single_listener_actually_receives_the_event(self) -> None:
        """Guards the merge from the other side — the one listener must be reached."""
        app = _make_app()
        seen: list[dict] = []
        ran = asyncio.Event()

        @app.event("reaction_added")
        async def only(event: dict) -> None:
            seen.append(event)
            ran.set()

        await app.async_dispatch(_reaction_event_request())
        await asyncio.wait_for(ran.wait(), timeout=5)

        assert len(seen) == 1
        assert seen[0]["reaction"] == "hmm_parrot"
        assert seen[0]["item"]["channel"] == "C1"

    @pytest.mark.asyncio
    async def test_listeners_run_detached_from_dispatch(self) -> None:
        """Pin the detachment itself — it is why the two tests above wait.

        It also means a listener that raises cannot surface in the dispatch
        result, which is why `src/slack/event_handler.py` wraps its own body in
        try/except rather than relying on Bolt to report failures.
        """
        app = _make_app()
        ran = asyncio.Event()

        @app.event("reaction_added")
        async def slow(event: dict) -> None:
            ran.set()

        response = await app.async_dispatch(_reaction_event_request())

        assert response.status == 200
        assert not ran.is_set(), "Bolt now awaits listeners before returning"
        await asyncio.wait_for(ran.wait(), timeout=5)


class TestSlackApiErrorContract:
    """`SlackApiError.response` must stay subscript/`.get()`-able."""

    @staticmethod
    def _error(code: str) -> SlackApiError:
        response = SlackResponse(
            client=None,
            http_verb="POST",
            api_url="https://slack.com/api/reactions.add",
            req_args={},
            data={"ok": False, "error": code},
            headers={},
            status_code=200,
        )
        return SlackApiError(message=code, response=response)

    def test_response_get_returns_the_error_code(self) -> None:
        assert self._error("already_reacted").response.get("error") == "already_reacted"

    def test_already_reacted_is_still_treated_as_success(self) -> None:
        """The idempotency guard: a duplicate reaction must not read as failure.

        If `response.get("error")` ever stops returning the code, this returns
        False and the bot logs a spurious warning on every duplicate — a silent
        behaviour change the rest of the suite would not notice.
        """
        client = SlackClient("xoxb-fake")
        client._client.reactions_add = _raiser(self._error("already_reacted"))
        assert client.add_reaction("C1", "1.2", "white_check_mark") is True

    def test_other_errors_still_read_as_failure(self) -> None:
        client = SlackClient("xoxb-fake")
        client._client.reactions_add = _raiser(self._error("channel_not_found"))
        assert client.add_reaction("C1", "1.2", "white_check_mark") is False

    def test_post_message_maps_scope_errors(self) -> None:
        client = SlackClient("xoxb-fake")
        client._client.chat_postMessage = _raiser(self._error("missing_scope"))
        assert client.post_message("C1", "hi") is False


class TestSlackWebApiWire:
    """What actually goes over the wire for the endpoints this bot calls.

    Upgrading an API client means checking the wire contract, not the method
    signatures — those can be identical across versions while the endpoint,
    parameter names or body encoding move underneath. Every call below is
    captured at slack_sdk's send point, so nothing here touches the network.
    """

    @staticmethod
    def _capture(call) -> dict:
        cap: dict = {}

        def fake_send(_self, url, req):
            cap["url"] = url
            cap["method"] = req.get_method()
            cap["body"] = req.data.decode() if req.data else None
            cap["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return {"status": 200, "headers": {}, "body": '{"ok": true}'}

        with patch.object(
            BaseClient, "_perform_urllib_http_request_internal", fake_send
        ):
            try:
                call()
            except Exception:
                # The request is captured before the response is parsed, and the
                # stub body is deliberately empty. What the caller then does with
                # that empty response is the subject of other tests, not this one.
                #
                # But only swallow once something was actually captured. Anything
                # that fails BEFORE the wire — a missing method, a bad signature —
                # is a real error and must surface as itself rather than as a
                # confusing empty-capture assertion later.
                if not cap:
                    raise
        return cap

    def test_add_reaction_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.add_reaction("C1", "1.2", "white_check_mark"))

        assert cap["url"] == "https://slack.com/api/reactions.add"
        assert cap["method"] == "POST"
        assert cap["headers"]["authorization"] == "Bearer xoxb-test"
        assert cap["headers"]["content-type"] == "application/x-www-form-urlencoded"
        # `timestamp`, not `ts` — a rename here silently stops confirmations.
        assert parse_qs(cap["body"]) == {
            "channel": ["C1"],
            "name": ["white_check_mark"],
            "timestamp": ["1.2"],
        }

    def test_post_message_wire(self) -> None:
        """chat.postMessage is sent as JSON, unlike the form-encoded endpoints.

        The encoding is not cosmetic: `reply_broadcast` travels as a real JSON
        boolean here, where a form-encoded body would send the string "1". If
        slack_sdk ever changes which calls get JSON, the broadcast and threading
        flags are what would change meaning.
        """
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.post_message("C1", "hello", "1.2", True))

        assert cap["url"] == "https://slack.com/api/chat.postMessage"
        assert cap["headers"]["content-type"] == "application/json;charset=utf-8"
        assert json.loads(cap["body"]) == {
            "channel": "C1",
            "text": "hello",
            "thread_ts": "1.2",
            "reply_broadcast": True,
        }

    def test_post_message_without_thread_omits_threading_fields(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.post_message("C1", "hello"))

        body = json.loads(cap["body"])
        assert "thread_ts" not in body and "reply_broadcast" not in body

    def test_broadcast_without_thread_is_dropped(self) -> None:
        """broadcast is meaningless without an anchor and must not be sent alone."""
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.post_message("C1", "hello", None, True))

        assert "reply_broadcast" not in json.loads(cap["body"])

    def test_conversations_history_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client._fetch_top_level("C1", "1.2"))

        assert cap["url"] == "https://slack.com/api/conversations.history"
        body = parse_qs(cap["body"])
        assert body["channel"] == ["C1"]
        assert body["latest"] == ["1.2"] and body["oldest"] == ["1.2"]
        assert body["inclusive"] == ["1"] and body["limit"] == ["1"]


    def test_users_info_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.get_user_info("U1"))

        assert cap["url"] == "https://slack.com/api/users.info"
        assert parse_qs(cap["body"])["user"] == ["U1"]

    def test_conversations_info_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client.get_channel_name("C1"))

        assert cap["url"] == "https://slack.com/api/conversations.info"
        assert parse_qs(cap["body"])["channel"] == ["C1"]


    def test_auth_test_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(client.get_bot_user_id)

        assert cap["url"] == "https://slack.com/api/auth.test"

    def test_conversations_replies_wire(self) -> None:
        client = SlackClient("xoxb-test")
        cap = self._capture(lambda: client._fetch_thread_reply("C1", "1.0", "1.2"))

        assert cap["url"] == "https://slack.com/api/conversations.replies"
        body = parse_qs(cap["body"])
        assert body["channel"] == ["C1"]
        assert body["ts"] == ["1.0"] and body["inclusive"] == ["1"]


class TestAiohttpExceptionHierarchy:
    """aiohttp is Bolt's async transport; nothing in `src/` imports it.

    Retry and reconnect logic inside slack_sdk's socket-mode client catches base
    classes. A reparented subclass turns a handled disconnect into an unhandled
    crash of the listener loop, which for this bot means it goes quietly deaf.
    """

    @pytest.mark.parametrize(
        "exc, base",
        [
            (aiohttp.ClientConnectionError, aiohttp.ClientError),
            (aiohttp.ClientConnectorError, aiohttp.ClientConnectionError),
            (aiohttp.ServerTimeoutError, aiohttp.ClientError),
            (aiohttp.ClientPayloadError, aiohttp.ClientError),
            (aiohttp.WSServerHandshakeError, aiohttp.ClientResponseError),
            (aiohttp.ClientResponseError, aiohttp.ClientError),
        ],
    )
    def test_exception_is_still_a_subclass_of_its_base(self, exc, base) -> None:
        assert issubclass(exc, base)

    def test_timeout_is_still_catchable_as_asyncio_timeout(self) -> None:
        """slack_sdk's socket-mode ping/reconnect path relies on this."""
        import asyncio

        assert issubclass(aiohttp.ServerTimeoutError, (asyncio.TimeoutError, TimeoutError))

    def test_bolt_async_app_imports_on_this_aiohttp(self) -> None:
        """`aiohttp` is undeclared by slack-bolt but required by its async app."""
        from slack_bolt.app.async_app import AsyncApp as _AsyncApp

        assert _AsyncApp is not None


def _raiser(exc: Exception):
    def _raise(*_args, **_kwargs):
        raise exc

    return _raise
