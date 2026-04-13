"""Tests for claude_on_the_fly.slack module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.agent import Response
from claude_on_the_fly.slack import (
    SLACK_BLOCK_LIMIT,
    SlackFrontend,
    _session_key,
    _split_blocks,
)


# ---------------------------------------------------------------------------
# _split_blocks
# ---------------------------------------------------------------------------


class TestSplitBlocks:
    def test_single_chunk_under_limit(self):
        text = "hello world"
        assert _split_blocks(text) == ["hello world"]

    def test_multiple_chunks_split_on_line_boundaries(self):
        line = "x" * 2000
        text = f"{line}\n{line}"
        result = _split_blocks(text)
        assert len(result) == 2
        assert result[0] == line
        assert result[1] == line

    def test_very_long_single_line_truncated(self):
        text = "a" * (SLACK_BLOCK_LIMIT + 500)
        result = _split_blocks(text)
        assert len(result) == 1
        assert len(result[0]) == SLACK_BLOCK_LIMIT

    def test_empty_text_returns_list_with_empty_string(self):
        assert _split_blocks("") == [""]

    def test_exact_limit_not_split(self):
        text = "a" * SLACK_BLOCK_LIMIT
        assert _split_blocks(text) == [text]

    def test_multiline_accumulation(self):
        lines = ["short line"] * 10
        text = "\n".join(lines)
        result = _split_blocks(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_line_after_chunk_flushes_previous(self):
        short = "hello"
        long_line = "b" * (SLACK_BLOCK_LIMIT + 100)
        text = f"{short}\n{long_line}"
        result = _split_blocks(text)
        assert result[0] == short
        assert result[1] == long_line[:SLACK_BLOCK_LIMIT]


# ---------------------------------------------------------------------------
# _session_key
# ---------------------------------------------------------------------------


class TestSessionKey:
    def test_same_inputs_same_key(self):
        assert _session_key("C123", "1234.5678") == _session_key("C123", "1234.5678")

    def test_different_channel_different_key(self):
        assert _session_key("C123", "1234.5678") != _session_key("C999", "1234.5678")

    def test_none_thread_ts_uses_root(self):
        """None thread_ts hashes as 'root', producing a stable key."""
        key_none = _session_key("C123", None)
        assert isinstance(key_none, int)
        # Deterministic: None always maps to the same key
        assert key_none == _session_key("C123", None)
        # Different from a real thread_ts
        assert key_none != _session_key("C123", "1234.5678")

    def test_returns_int(self):
        assert isinstance(_session_key("C123", "ts"), int)

    def test_different_thread_ts_different_key(self):
        assert _session_key("C123", "111.222") != _session_key("C123", "333.444")


# ---------------------------------------------------------------------------
# SlackFrontend.__init__
# ---------------------------------------------------------------------------


class TestSlackFrontendInit:
    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_user_id_in_allowed_ids(self, mock_app_cls):
        frontend = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert "U_SELF" in frontend._allowed_user_ids

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_user_id_added_to_existing_allowed(self, mock_app_cls):
        frontend = SlackFrontend(
            "xapp-tok", "xoxp-tok", "U_SELF", allowed_user_ids={"U_OTHER"}
        )
        assert "U_SELF" in frontend._allowed_user_ids
        assert "U_OTHER" in frontend._allowed_user_ids

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_empty_allowed_still_contains_user_id(self, mock_app_cls):
        frontend = SlackFrontend(
            "xapp-tok", "xoxp-tok", "U_SELF", allowed_user_ids=set()
        )
        assert frontend._allowed_user_ids == {"U_SELF"}

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_wildcard_sets_allow_all_flag(self, mock_app_cls):
        frontend = SlackFrontend(
            "xapp-tok", "xoxp-tok", "U_SELF", allowed_user_ids={"*"}
        )
        assert frontend._allow_all_senders is True

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_no_wildcard_keeps_allow_all_off(self, mock_app_cls):
        frontend = SlackFrontend(
            "xapp-tok", "xoxp-tok", "U_SELF", allowed_user_ids={"U_OTHER"}
        )
        assert frontend._allow_all_senders is False


# ---------------------------------------------------------------------------
# workspace_name / sender_name / channel_context
# ---------------------------------------------------------------------------


class TestMetadataAccessors:
    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_workspace_name_default(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        assert fe.workspace_name(999) == "slack/999"

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_workspace_name_cached(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        fe._workspace_names[42] = "dm-hoss-123"
        assert fe.workspace_name(42) == "slack/dm-hoss-123"

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_sender_name_default(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        assert fe.sender_name(999) == "unknown"

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_sender_name_cached(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        fe._sender_names[42] = "hoss"
        assert fe.sender_name(42) == "hoss"

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_channel_context_default(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        assert fe.channel_context(999) == "dm"

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_channel_context_cached(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        fe._channel_contexts[42] = "channel:#general (public)"
        assert fe.channel_context(42) == "channel:#general (public)"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def frontend():
    with patch("claude_on_the_fly.slack.AsyncApp") as mock_app_cls:
        mock_app = MagicMock()
        mock_app.client = MagicMock()
        mock_app.client.chat_postMessage = AsyncMock()
        mock_app.client.users_info = AsyncMock()
        mock_app.client.conversations_info = AsyncMock()
        mock_app.client.conversations_members = AsyncMock()
        mock_app.client.conversations_history = AsyncMock()
        mock_app_cls.return_value = mock_app

        fe = SlackFrontend(
            "xapp-tok",
            "xoxp-tok",
            "U_SELF",
            allowed_user_ids={"U_ALLOWED"},
        )
        fe._on_message = AsyncMock()

        # Pre-populate users_info to avoid unrelated failures
        mock_app.client.users_info.return_value = {"user": {"name": "testuser"}}
        mock_app.client.conversations_info.return_value = {
            "channel": {"name": "general", "is_mpim": False, "is_private": False}
        }

        yield fe


# ---------------------------------------------------------------------------
# _ingest_event
# ---------------------------------------------------------------------------


class TestIngestEvent:
    async def test_skips_subtype(self, frontend):
        await frontend._ingest_event({"subtype": "bot_message", "ts": "1"})
        frontend._on_message.assert_not_awaited()

    async def test_skips_own_message(self, frontend):
        frontend._our_sent_timestamps.append("1.0")
        await frontend._ingest_event({"ts": "1.0", "text": "hi", "channel": "C1"})
        frontend._on_message.assert_not_awaited()

    async def test_skips_already_processed(self, frontend):
        frontend._processed_ts.append("2.0")
        await frontend._ingest_event({"ts": "2.0", "text": "hi", "channel": "C1"})
        frontend._on_message.assert_not_awaited()

    async def test_skips_no_channel(self, frontend):
        await frontend._ingest_event({"ts": "3.0", "text": "hi"})
        frontend._on_message.assert_not_awaited()

    async def test_channel_skips_disallowed_sender(self, frontend):
        event = {
            "ts": "4.0",
            "text": "<@U_SELF> hello",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_RANDOM",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

    async def test_channel_allows_any_sender_with_wildcard(self, frontend):
        frontend._allow_all_senders = True
        event = {
            "ts": "4.1",
            "text": "<@U_SELF> hello",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_RANDOM",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()

    async def test_channel_skips_no_mention(self, frontend):
        event = {
            "ts": "5.0",
            "text": "hello no mention",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_SELF",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

    async def test_channel_strips_mention(self, frontend):
        event = {
            "ts": "6.0",
            "text": "<@U_SELF> do the thing",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "<@U_SELF>" not in call_text
        assert "do the thing" in call_text

    async def test_skips_empty_text_after_processing(self, frontend):
        event = {
            "ts": "7.0",
            "text": "<@U_SELF>",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

    async def test_dm_processes_without_mention(self, frontend):
        event = {
            "ts": "8.0",
            "text": "hello from dm",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_SOMEONE",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "hello from dm" in call_text

    async def test_adds_ts_to_processed(self, frontend):
        event = {
            "ts": "9.0",
            "text": "hi",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_SOMEONE",
        }
        await frontend._ingest_event(event)
        assert "9.0" in frontend._processed_ts

    async def test_calls_on_message_with_session_id_and_text(self, frontend):
        event = {
            "ts": "10.0",
            "text": "ping",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_SOMEONE",
        }
        await frontend._ingest_event(event)
        session_id, text = frontend._on_message.call_args[0]
        assert isinstance(session_id, int)
        assert "ping" in text
        assert "[from: testuser]" in text

    async def test_group_channel_type_requires_mention(self, frontend):
        event = {
            "ts": "11.0",
            "text": "no mention here",
            "channel": "G1",
            "channel_type": "group",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# notify_queued
# ---------------------------------------------------------------------------


class TestNotifyQueued:
    async def test_reacts_with_hourglass_on_last_message(self, frontend):
        session_id = 42
        frontend._last_msg[session_id] = ("C1", "100.0")
        frontend._app.client.reactions_add = AsyncMock()

        await frontend.notify_queued(session_id, 2)

        frontend._app.client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="100.0", name="hourglass_flowing_sand"
        )
        # should NOT post a chat message
        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_no_op_when_no_last_message(self, frontend):
        frontend._app.client.reactions_add = AsyncMock()
        await frontend.notify_queued(99999, 1)
        frontend._app.client.reactions_add.assert_not_awaited()

    async def test_ingest_records_last_msg(self, frontend):
        event = {
            "ts": "12.0",
            "text": "hi",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_SOMEONE",
        }
        await frontend._ingest_event(event)
        session_id = _session_key("D1", "12.0")
        assert frontend._last_msg[session_id] == ("D1", "12.0")


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


class TestSend:
    async def test_noop_when_session_not_found(self, frontend):
        response = Response(body="hi")
        await frontend.send(999999, response)
        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_posts_message_with_blocks(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")

        response = Response(body="hello", cost=0.01, model="sonnet")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(session_id, response)

        call_kwargs = frontend._app.client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C1"
        assert call_kwargs["thread_ts"] == "t1"
        blocks = call_kwargs["blocks"]
        assert any(b["type"] == "section" for b in blocks)
        assert any(b["type"] == "context" for b in blocks)

    async def test_tracks_sent_timestamp(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(session_id, Response(body="hi"))
        assert "99.0" in frontend._our_sent_timestamps

    async def test_handles_api_failure(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.side_effect = Exception("network")

        await frontend.send(session_id, Response(body="hi"))
        # Should not raise

    async def test_no_stats_block_when_no_stats(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "100"}

        response = Response(body="plain text")
        await frontend.send(session_id, response)

        blocks = frontend._app.client.chat_postMessage.call_args[1]["blocks"]
        assert not any(b["type"] == "context" for b in blocks)


# ---------------------------------------------------------------------------
# _resolve_sender
# ---------------------------------------------------------------------------


class TestResolveSender:
    async def test_returns_cached_name(self, frontend):
        frontend._user_name_cache["U_CACHED"] = "cached_user"
        result = await frontend._resolve_sender("U_CACHED")
        assert result == "cached_user"
        frontend._app.client.users_info.assert_not_awaited()

    async def test_calls_api_and_caches(self, frontend):
        frontend._app.client.users_info.return_value = {"user": {"name": "fresh_user"}}
        result = await frontend._resolve_sender("U_NEW")
        assert result == "fresh_user"
        assert frontend._user_name_cache["U_NEW"] == "fresh_user"

    async def test_returns_raw_id_on_failure(self, frontend):
        frontend._app.client.users_info.side_effect = Exception("api down")
        result = await frontend._resolve_sender("U_FAIL")
        assert result == "U_FAIL"
        assert "U_FAIL" not in frontend._user_name_cache


# ---------------------------------------------------------------------------
# _resolve_session_metadata
# ---------------------------------------------------------------------------


class TestResolveSessionMetadata:
    async def test_noop_if_already_resolved(self, frontend):
        frontend._workspace_names[42] = "already-set"
        await frontend._resolve_session_metadata(42, "hoss", "C1", "channel", "1.0")
        frontend._app.client.conversations_info.assert_not_awaited()

    async def test_dm_sets_workspace_and_context(self, frontend):
        await frontend._resolve_session_metadata(100, "hoss", "D1", "im", "123.456")
        assert frontend._workspace_names[100] == "dm-hoss-123"
        assert frontend._channel_contexts[100] == "dm (private)"

    async def test_channel_resolves_name_and_visibility_public(self, frontend):
        frontend._app.client.conversations_info.return_value = {
            "channel": {"name": "general", "is_mpim": False, "is_private": False}
        }
        await frontend._resolve_session_metadata(200, "hoss", "C1", "channel", "1.0")
        assert "general" in frontend._workspace_names[200]
        assert "public" in frontend._channel_contexts[200]

    async def test_channel_resolves_private(self, frontend):
        frontend._app.client.conversations_info.return_value = {
            "channel": {"name": "secret", "is_mpim": False, "is_private": True}
        }
        await frontend._resolve_session_metadata(201, "hoss", "C2", "group", "1.0")
        assert "private" in frontend._channel_contexts[201]

    async def test_mpim_resolves_members(self, frontend):
        frontend._app.client.conversations_info.return_value = {
            "channel": {"name": "mpdm-a-b", "is_mpim": True}
        }
        frontend._app.client.conversations_members.return_value = {
            "members": ["U_SELF", "U_OTHER"]
        }
        frontend._app.client.users_info.return_value = {"user": {"name": "other_user"}}
        await frontend._resolve_session_metadata(300, "hoss", "G1", "mpim", "1.0")
        assert "group-dm" in frontend._channel_contexts[300]
        assert "other_user" in frontend._channel_contexts[300]

    async def test_api_failure_sets_fallback(self, frontend):
        frontend._app.client.conversations_info.side_effect = Exception("boom")
        await frontend._resolve_session_metadata(400, "hoss", "C99", "channel", "5.0")
        assert "C99" in frontend._workspace_names[400]
        assert "C99" in frontend._channel_contexts[400]


# ---------------------------------------------------------------------------
# set_orchestrator
# ---------------------------------------------------------------------------


class TestSetOrchestrator:
    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_stores_orchestrator(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        sentinel = object()
        fe.set_orchestrator(sentinel)
        assert fe._orchestrator is sentinel


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_registers_handlers_and_starts_socket_mode(self, frontend):
        mock_on_message = AsyncMock()
        with patch(
            "claude_on_the_fly.slack.AsyncSocketModeHandler"
        ) as mock_handler_cls:
            mock_handler = AsyncMock()
            mock_handler_cls.return_value = mock_handler
            await frontend.start(mock_on_message)

        assert frontend._on_message is mock_on_message
        assert frontend._handler is mock_handler
        mock_handler.start_async.assert_awaited_once()

    async def test_event_handlers_registered(self, frontend):
        mock_on_message = AsyncMock()
        with patch(
            "claude_on_the_fly.slack.AsyncSocketModeHandler"
        ) as mock_handler_cls:
            mock_handler_cls.return_value = AsyncMock()
            await frontend.start(mock_on_message)

        # The "message" event and "hello" event should have been registered
        frontend._app.event.assert_any_call({"type": "message"})
        frontend._app.event.assert_any_call("hello")


# ---------------------------------------------------------------------------
# _on_hello
# ---------------------------------------------------------------------------


class TestOnHello:
    async def test_initial_connection_no_catchup(self, frontend):
        frontend._connected_once = False
        with patch.object(frontend, "_catchup", new_callable=AsyncMock) as mock_catchup:
            await frontend._on_hello(event={}, say=None)
        assert frontend._connected_once is True
        mock_catchup.assert_not_awaited()

    async def test_reconnection_triggers_catchup(self, frontend):
        frontend._connected_once = True
        with patch.object(frontend, "_catchup", new_callable=AsyncMock) as mock_catchup:
            await frontend._on_hello(event={}, say=None)
        mock_catchup.assert_awaited_once()


# ---------------------------------------------------------------------------
# _catchup
# ---------------------------------------------------------------------------


class TestCatchup:
    async def test_empty_active_channels_returns_early(self, frontend):
        frontend._active_channels = {}
        await frontend._catchup()
        frontend._app.client.conversations_history.assert_not_awaited()

    async def test_fetches_history_and_ingests_sorted(self, frontend):
        frontend._active_channels = {"C1": "100.0"}
        frontend._channel_types["C1"] = "im"
        frontend._app.client.conversations_history.return_value = {
            "messages": [
                {"ts": "102.0", "text": "second"},
                {"ts": "101.0", "text": "first"},
            ]
        }
        with patch.object(
            frontend, "_ingest_event", new_callable=AsyncMock
        ) as mock_ingest:
            await frontend._catchup()

        assert mock_ingest.await_count == 2
        first_call_msg = mock_ingest.call_args_list[0][0][0]
        second_call_msg = mock_ingest.call_args_list[1][0][0]
        # Sorted by ts: 101 before 102
        assert first_call_msg["ts"] == "101.0"
        assert second_call_msg["ts"] == "102.0"
        # channel and channel_type injected
        assert first_call_msg["channel"] == "C1"
        assert first_call_msg["channel_type"] == "im"

    async def test_api_failure_continues(self, frontend):
        frontend._active_channels = {"C1": "100.0", "C2": "200.0"}
        frontend._channel_types["C2"] = "im"
        frontend._app.client.conversations_history.side_effect = [
            Exception("network"),
            {"messages": [{"ts": "201.0", "text": "ok"}]},
        ]
        with patch.object(
            frontend, "_ingest_event", new_callable=AsyncMock
        ) as mock_ingest:
            await frontend._catchup()
        # Only C2 messages ingested (C1 failed)
        assert mock_ingest.await_count == 1


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_closes_handler(self, frontend):
        mock_handler = AsyncMock()
        frontend._handler = mock_handler
        await frontend.stop()
        mock_handler.close_async.assert_awaited_once()

    async def test_noop_when_no_handler(self, frontend):
        frontend._handler = None
        await frontend.stop()  # should not raise


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------


class TestSendTyping:
    async def test_send_typing_is_noop(self, frontend):
        # send_typing is a pass; just verify it doesn't raise
        await frontend.send_typing(12345)


# ---------------------------------------------------------------------------
# _resolve_mpim_members
# ---------------------------------------------------------------------------


class TestResolveMpimMembers:
    async def test_resolves_member_names_excluding_self(self, frontend):
        frontend._app.client.conversations_members.return_value = {
            "members": ["U_SELF", "U_A", "U_B"]
        }
        frontend._app.client.users_info.side_effect = [
            {"user": {"name": "alice"}},
            {"user": {"name": "bob"}},
        ]
        result = await frontend._resolve_mpim_members("G1")
        assert result == ["alice", "bob"]

    async def test_api_failure_returns_unknown(self, frontend):
        frontend._app.client.conversations_members.side_effect = Exception("boom")
        result = await frontend._resolve_mpim_members("G1")
        assert result == ["unknown"]
