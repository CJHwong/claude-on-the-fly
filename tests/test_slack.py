"""Tests for claude_on_the_fly.slack module."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from claude_on_the_fly.agent import Response
from claude_on_the_fly.slack import (
    CONTINUE_COMMAND,
    DEFAULT_JOB_COMMAND,
    JOB_LIST_LIMIT,
    SLACK_REPLY_SOFT_LIMIT,
    SlackFrontend,
    _render_job_list,
    _session_key,
    _split_blocks,
)
from claude_on_the_fly.slack_mrkdwn import SLACK_BLOCK_LIMIT


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
        # The newline the split consumed rides with the following chunk, so the
        # chunks still reassemble into exactly the input.
        assert result[1] == f"\n{line}"
        assert "".join(result) == text

    def test_very_long_single_line_is_sliced_not_truncated(self):
        """A line over the limit used to be cut to line[:LIMIT] with the tail
        dropped — no error, no log, and nothing in the output saying so."""
        text = "a" * (SLACK_BLOCK_LIMIT + 500)
        result = _split_blocks(text)
        assert len(result) == 2
        assert all(len(chunk) <= SLACK_BLOCK_LIMIT for chunk in result)
        assert "".join(result) == text

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
        assert all(len(chunk) <= SLACK_BLOCK_LIMIT for chunk in result)
        assert "".join(result) == text


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
    def test_silent_sender_ids_default_empty(self, mock_app_cls):
        frontend = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert frontend._silent_sender_ids == set()

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_no_wildcard_keeps_allow_all_off(self, mock_app_cls):
        frontend = SlackFrontend(
            "xapp-tok", "xoxp-tok", "U_SELF", allowed_user_ids={"U_OTHER"}
        )
        assert frontend._allow_all_senders is False

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_user_token_keeps_self_events(self, mock_app_cls):
        frontend = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert frontend._is_bot_token is False
        _, kwargs = mock_app_cls.call_args
        assert kwargs["ignoring_self_events_enabled"] is False

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_bot_token_ignores_self_events(self, mock_app_cls):
        frontend = SlackFrontend("xapp-tok", "xoxb-tok", "U_SELF")
        assert frontend._is_bot_token is True
        _, kwargs = mock_app_cls.call_args
        assert kwargs["ignoring_self_events_enabled"] is True


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
    async def test_skips_non_allowed_subtype(self, frontend):
        await frontend._ingest_event({"subtype": "bot_message", "ts": "1"})
        frontend._on_message.assert_not_awaited()

    async def test_allows_file_share_subtype(self, frontend):
        event = {
            "subtype": "file_share",
            "ts": "1.1",
            "text": "check this",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
            "files": [
                {
                    "id": "F1",
                    "name": "screenshot.png",
                    "url_private_download": "https://files.slack.com/f1",
                }
            ],
        }
        with patch.object(
            frontend,
            "_save_files",
            new_callable=AsyncMock,
            return_value=["[File saved: screenshot.png]"],
        ):
            await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "[File saved: screenshot.png]" in call_text
        assert "check this" in call_text

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

    async def test_blocklist_wins_over_wildcard(self, frontend):
        frontend._allow_all_senders = True
        frontend._blocked_senders = {"U_BANNED"}
        event = {
            "ts": "4.2",
            "text": "<@U_SELF> hello",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_BANNED",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

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
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "hello from dm" in call_text

    async def test_dm_skips_disallowed_sender(self, frontend):
        event = {
            "ts": "8.1",
            "text": "hello from dm",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_RANDOM",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

    async def test_mpim_skips_disallowed_sender(self, frontend):
        event = {
            "ts": "8.2",
            "text": "hello from mpim",
            "channel": "G1",
            "channel_type": "mpim",
            "user": "U_RANDOM",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_not_awaited()

    async def test_dm_allows_any_sender_with_wildcard(self, frontend):
        frontend._allow_all_senders = True
        event = {
            "ts": "8.3",
            "text": "hello from dm",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_RANDOM",
        }
        await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()

    async def test_adds_ts_to_processed(self, frontend):
        event = {
            "ts": "9.0",
            "text": "hi",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        assert "9.0" in frontend._processed_ts

    async def test_calls_on_message_with_session_id_and_text(self, frontend):
        event = {
            "ts": "10.0",
            "text": "ping",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
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
    async def test_reacts_with_hourglass_on_latest_pending(self, frontend):
        from collections import deque

        session_id = 42
        frontend._pending_msg[session_id] = deque([("C1", "90.0"), ("C1", "100.0")])
        frontend._app.client.reactions_add = AsyncMock()

        await frontend.notify_queued(session_id, 2)

        frontend._app.client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="100.0", name="hourglass_flowing_sand"
        )
        # should NOT post a chat message
        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_no_op_when_no_pending_message(self, frontend):
        frontend._app.client.reactions_add = AsyncMock()
        await frontend.notify_queued(99999, 1)
        frontend._app.client.reactions_add.assert_not_awaited()

    async def test_ingest_records_pending_msg(self, frontend):
        event = {
            "ts": "12.0",
            "text": "hi",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        session_id = _session_key("D1", "12.0")
        assert list(frontend._pending_msg[session_id]) == [("D1", "12.0")]


class TestNotifyStart:
    async def test_transitions_hourglass_to_eyes_on_oldest(self, frontend):
        from collections import deque

        session_id = 7
        frontend._pending_msg[session_id] = deque([("C1", "10.0"), ("C1", "20.0")])
        frontend._app.client.reactions_add = AsyncMock()
        frontend._app.client.reactions_remove = AsyncMock()

        await frontend.notify_start(session_id)

        frontend._app.client.reactions_remove.assert_awaited_once_with(
            channel="C1", timestamp="10.0", name="hourglass_flowing_sand"
        )
        frontend._app.client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="10.0", name="eyes"
        )
        assert list(frontend._pending_msg[session_id]) == [("C1", "20.0")]
        assert frontend._in_flight[session_id] == ("C1", "10.0")

    async def test_no_op_when_no_pending(self, frontend):
        frontend._app.client.reactions_add = AsyncMock()
        frontend._app.client.reactions_remove = AsyncMock()
        await frontend.notify_start(99999)
        frontend._app.client.reactions_add.assert_not_awaited()
        frontend._app.client.reactions_remove.assert_not_awaited()


class TestNotifyComplete:
    async def test_removes_eyes_from_in_flight(self, frontend):
        session_id = 7
        frontend._in_flight[session_id] = ("C1", "10.0")
        frontend._app.client.reactions_remove = AsyncMock()

        await frontend.notify_complete(session_id)

        frontend._app.client.reactions_remove.assert_awaited_once_with(
            channel="C1", timestamp="10.0", name="eyes"
        )
        assert session_id not in frontend._in_flight

    async def test_no_op_when_no_in_flight(self, frontend):
        frontend._app.client.reactions_remove = AsyncMock()
        await frontend.notify_complete(99999)
        frontend._app.client.reactions_remove.assert_not_awaited()


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

    async def test_suppressed_bot_reply_omits_slack_post(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._in_flight_reply_suppressed[session_id] = True

        delivered = await frontend.send(session_id, Response(body="hi"))

        assert delivered == []
        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_suppressed_bot_reply_marks_attachments_handled(
        self, frontend, tmp_path
    ):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._in_flight_reply_suppressed[session_id] = True
        report = tmp_path / "report.csv"
        report.write_text("data")

        delivered = await frontend.send(
            session_id, Response(body="hi", attachments=[report])
        )

        assert delivered == [report]
        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_tools_footer_in_context_block(self, frontend, monkeypatch):
        monkeypatch.setenv("SLACK_STATS_MODE", "detailed")
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        response = Response(body="done", tool_counts={"Read": 1})
        await frontend.send(session_id, response)

        blocks = frontend._app.client.chat_postMessage.call_args[1]["blocks"]
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert len(context_blocks) >= 1
        tools_text = context_blocks[-1]["elements"][0]["text"]
        assert "Read" in tools_text

    async def test_tracks_sent_timestamp(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(session_id, Response(body="hi"))
        assert "99.0" in frontend._our_sent_timestamps

    async def test_send_ok_false_logs_warning(self, frontend: SlackFrontend) -> None:
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {  # type: ignore[assignment]
            "ok": False,
            "error": "channel_not_found",
        }
        await frontend.send(session_id, Response(body="hi"))
        # Should not raise; error is logged

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

    async def test_no_upload_when_no_attachments(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
        frontend._app.client.files_upload_v2 = AsyncMock()

        await frontend.send(session_id, Response(body="hi"))
        frontend._app.client.files_upload_v2.assert_not_awaited()

    async def test_uploads_attachments_in_thread(self, frontend, tmp_path):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
        frontend._app.client.files_upload_v2 = AsyncMock(return_value={"files": []})
        report = tmp_path / "report.csv"
        report.write_text("data")

        await frontend.send(session_id, Response(body="hi", attachments=[report]))

        call = frontend._app.client.files_upload_v2.call_args[1]
        assert call["channel"] == "C1"
        assert call["thread_ts"] == "t1"
        # File bytes are read off the event loop and passed as content, not a path.
        assert call["file"] == b"data"
        assert call["filename"] == "report.csv"

    async def test_returns_attachments_only_when_text_post_succeeds(
        self, frontend, tmp_path
    ):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.files_upload_v2 = AsyncMock(return_value={"files": []})
        report = tmp_path / "report.csv"
        report.write_text("data")

        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
        delivered = await frontend.send(
            session_id, Response(body="hi", attachments=[report])
        )
        assert delivered == [report]

    async def test_returns_empty_when_text_post_fails(self, frontend, tmp_path):
        # The orchestrator archives whatever send() returns; a failed text post
        # must report nothing so the un-uploaded file isn't archived/lost.
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage = AsyncMock(side_effect=Exception("boom"))
        frontend._app.client.files_upload_v2 = AsyncMock()
        report = tmp_path / "report.csv"
        report.write_text("data")

        delivered = await frontend.send(
            session_id, Response(body="hi", attachments=[report])
        )
        assert delivered == []
        frontend._app.client.files_upload_v2.assert_not_awaited()

    async def test_records_upload_share_ts_as_echo_guard(self, frontend, tmp_path):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
        frontend._app.client.files_upload_v2 = AsyncMock(
            return_value={"files": [{"shares": {"private": {"C1": [{"ts": "55.5"}]}}}]}
        )
        f = tmp_path / "a.txt"
        f.write_text("x")

        await frontend.send(session_id, Response(body="hi", attachments=[f]))
        assert "55.5" in frontend._our_sent_timestamps

    async def test_upload_failure_posts_notice_and_does_not_raise(
        self, frontend, tmp_path
    ):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
        frontend._app.client.files_upload_v2 = AsyncMock(
            side_effect=SlackApiError(
                "no scope", {"ok": False, "error": "missing_scope"}
            )
        )
        report = tmp_path / "report.csv"
        report.write_text("x")

        await frontend.send(session_id, Response(body="hi", attachments=[report]))

        notices = [
            c
            for c in frontend._app.client.chat_postMessage.call_args_list
            if "missing_scope" in (c.kwargs.get("text") or "")
        ]
        assert len(notices) == 1
        assert notices[0].kwargs["thread_ts"] == "t1"


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
        from claude_on_the_fly.orchestrator import Orchestrator

        fe = SlackFrontend("xapp", "xoxp", "U1")
        orch = Orchestrator(fe, "slack")
        fe.set_orchestrator(orch)
        assert fe._orchestrator is orch

    @patch("claude_on_the_fly.slack.AsyncApp")
    def test_rejects_non_orchestrator(self, mock_app_cls):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        with pytest.raises(TypeError):
            fe.set_orchestrator(object())


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
        assert first_call_msg["ts"] == "101.0"
        assert second_call_msg["ts"] == "102.0"
        assert first_call_msg["channel"] == "C1"
        assert first_call_msg["channel_type"] == "im"

    async def test_empty_messages_skips_channel(self, frontend: SlackFrontend) -> None:
        """When conversations_history returns empty messages, continue to next channel."""
        frontend._active_channels = {"C1": "100.0", "C2": "200.0"}
        frontend._channel_types["C2"] = "im"
        frontend._app.client.conversations_history.side_effect = [  # type: ignore[assignment]
            {"messages": []},
            {"messages": [{"ts": "201.0", "text": "msg"}]},
        ]
        with patch.object(
            frontend, "_ingest_event", new_callable=AsyncMock
        ) as mock_ingest:
            await frontend._catchup()
        assert mock_ingest.await_count == 1

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


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


class TestSaveFiles:
    async def test_downloads_and_returns_lines(self, frontend, tmp_path):
        frontend._workspace_names[42] = "test-ws"
        files = [
            {
                "id": "F1",
                "name": "doc.pdf",
                "url_private_download": "https://example.com/f1",
            },
            {
                "id": "F2",
                "name": "img.png",
                "url_private_download": "https://example.com/f2",
            },
        ]
        with (
            patch.object(frontend, "_workspace_path", return_value=tmp_path),
            patch.object(frontend, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            result = await frontend._save_files(42, files)

        assert result == ["[File saved: doc.pdf]", "[File saved: img.png]"]
        assert mock_dl.await_count == 2

    async def test_skips_file_without_url(self, frontend, tmp_path):
        files = [{"id": "F1", "name": "no_url.txt"}]
        with patch.object(frontend, "_workspace_path", return_value=tmp_path):
            result = await frontend._save_files(42, files)
        assert result == []

    async def test_continues_on_download_failure(self, frontend, tmp_path):
        files = [
            {
                "id": "F1",
                "name": "bad.txt",
                "url_private_download": "https://example.com/bad",
            },
            {
                "id": "F2",
                "name": "good.txt",
                "url_private_download": "https://example.com/good",
            },
        ]
        with (
            patch.object(frontend, "_workspace_path", return_value=tmp_path),
            patch.object(
                frontend,
                "_download_file",
                new_callable=AsyncMock,
                side_effect=[Exception("network"), None],
            ),
        ):
            result = await frontend._save_files(42, files)
        assert result == ["[File saved: good.txt]"]

    async def test_fallback_name_when_name_missing(self, frontend, tmp_path):
        files = [{"id": "F99", "url_private_download": "https://example.com/f99"}]
        with (
            patch.object(frontend, "_workspace_path", return_value=tmp_path),
            patch.object(frontend, "_download_file", new_callable=AsyncMock),
        ):
            result = await frontend._save_files(42, files)
        assert result == ["[File saved: file_F99]"]


class TestDownloadFile:
    @staticmethod
    def _mock_aiohttp(content: bytes, content_type: str = "image/png"):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content_type = content_type
        mock_resp.read = AsyncMock(return_value=content)

        mock_get_ctx = MagicMock()
        mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_ctx)

        mock_client_ctx = MagicMock()
        mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_ctx.__aexit__ = AsyncMock(return_value=False)
        return mock_client_ctx, mock_session

    async def test_writes_bytes_to_dest(self, tmp_path):
        dest = tmp_path / "test.png"
        mock_ctx, mock_session = self._mock_aiohttp(b"fake image bytes")

        with patch(
            "claude_on_the_fly.slack.aiohttp.ClientSession", return_value=mock_ctx
        ):
            await SlackFrontend._download_file(
                "https://example.com/f", dest, "xoxp-tok"
            )

        assert dest.read_bytes() == b"fake image bytes"
        mock_session.get.assert_called_once_with(
            "https://example.com/f",
            headers={"Authorization": "Bearer xoxp-tok"},
            allow_redirects=True,
        )

    async def test_rejects_html_response(self, tmp_path):
        dest = tmp_path / "test.png"
        mock_ctx, _ = self._mock_aiohttp(b"<html>login</html>", "text/html")

        with (
            patch(
                "claude_on_the_fly.slack.aiohttp.ClientSession", return_value=mock_ctx
            ),
            pytest.raises(RuntimeError, match="got HTML"),
        ):
            await SlackFrontend._download_file(
                "https://example.com/f", dest, "xoxp-tok"
            )

    async def test_rejects_empty_body(self, tmp_path):
        dest = tmp_path / "test.png"
        mock_ctx, _ = self._mock_aiohttp(b"", "image/png")

        with (
            patch(
                "claude_on_the_fly.slack.aiohttp.ClientSession", return_value=mock_ctx
            ),
            pytest.raises(RuntimeError, match="empty response"),
        ):
            await SlackFrontend._download_file(
                "https://example.com/f", dest, "xoxp-tok"
            )


class TestIngestEventWithFiles:
    async def test_file_only_no_text(self, frontend):
        """File upload with no caption should still produce a message."""
        event = {
            "subtype": "file_share",
            "ts": "50.0",
            "text": "",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
            "files": [
                {
                    "id": "F1",
                    "name": "report.csv",
                    "url_private_download": "https://example.com/f1",
                }
            ],
        }
        with patch.object(
            frontend,
            "_save_files",
            new_callable=AsyncMock,
            return_value=["[File saved: report.csv]"],
        ):
            await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "[File saved: report.csv]" in call_text

    async def test_file_with_caption_in_channel(self, frontend):
        """File upload in channel with @mention and caption."""
        event = {
            "subtype": "file_share",
            "ts": "51.0",
            "text": "<@U_SELF> analyze this",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
            "files": [
                {
                    "id": "F1",
                    "name": "data.xlsx",
                    "url_private_download": "https://example.com/f1",
                }
            ],
        }
        with patch.object(
            frontend,
            "_save_files",
            new_callable=AsyncMock,
            return_value=["[File saved: data.xlsx]"],
        ):
            await frontend._ingest_event(event)
        frontend._on_message.assert_awaited_once()
        _, call_text = frontend._on_message.call_args[0]
        assert "[File saved: data.xlsx]" in call_text
        assert "analyze this" in call_text
        assert "<@U_SELF>" not in call_text


# ---------------------------------------------------------------------------
# _react / _unreact error handling
# ---------------------------------------------------------------------------


class TestReactErrorHandling:
    async def test_react_logs_warning_on_exception(self, frontend, caplog):
        frontend._app.client.reactions_add = AsyncMock(
            side_effect=Exception("rate_limited")
        )
        await frontend._react("C1", "100.0", "eyes")
        assert "react: failed to add" in caplog.text

    async def test_unreact_silently_ignores_no_reaction(self, frontend, caplog):
        frontend._app.client.reactions_remove = AsyncMock(
            side_effect=Exception("no_reaction")
        )
        await frontend._unreact("C1", "100.0", "eyes")
        assert "unreact: failed to remove" not in caplog.text

    async def test_unreact_logs_warning_on_other_exception(self, frontend, caplog):
        frontend._app.client.reactions_remove = AsyncMock(
            side_effect=Exception("rate_limited")
        )
        await frontend._unreact("C1", "100.0", "eyes")
        assert "unreact: failed to remove" in caplog.text


# ---------------------------------------------------------------------------
# _workspace_path
# ---------------------------------------------------------------------------


class TestWorkspacePath:
    @patch("claude_on_the_fly.slack.DATA_DIR", Path("/data"))
    def test_returns_data_dir_workspaces_path(self):
        fe = SlackFrontend("xapp", "xoxp", "U1")
        fe._workspace_names[42] = "my-ws"
        result = fe._workspace_path(42)
        assert result == Path("/data/workspaces/slack/my-ws")


# ---------------------------------------------------------------------------
# Reply soft-limit gate
# ---------------------------------------------------------------------------


class TestReplySoftLimit:
    def _dm_event(self, ts: str, text: str) -> dict:
        return {
            "ts": ts,
            "text": text,
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }

    async def test_gates_inbound_when_over_limit(self, frontend):
        session_id = _session_key("D1", "200.0")
        frontend._reply_counts[session_id] = SLACK_REPLY_SOFT_LIMIT
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "w.0"}

        await frontend._ingest_event(self._dm_event("200.0", "another question"))

        frontend._on_message.assert_not_awaited()
        warning = frontend._app.client.chat_postMessage.call_args[1]["text"]
        assert CONTINUE_COMMAND in warning

    async def test_under_limit_processes_normally(self, frontend):
        session_id = _session_key("D1", "201.0")
        frontend._reply_counts[session_id] = SLACK_REPLY_SOFT_LIMIT - 1

        await frontend._ingest_event(self._dm_event("201.0", "still going"))

        frontend._on_message.assert_awaited_once()

    async def test_continue_resets_and_processes_remainder(self, frontend):
        session_id = _session_key("D1", "202.0")
        frontend._reply_counts[session_id] = SLACK_REPLY_SOFT_LIMIT

        await frontend._ingest_event(
            self._dm_event("202.0", f"{CONTINUE_COMMAND} now do X")
        )

        assert frontend._reply_counts[session_id] == 0
        frontend._on_message.assert_awaited_once()
        _, text = frontend._on_message.call_args[0]
        assert "now do X" in text
        assert CONTINUE_COMMAND not in text

    async def test_bare_continue_resets_and_drops(self, frontend):
        session_id = _session_key("D1", "203.0")
        frontend._reply_counts[session_id] = SLACK_REPLY_SOFT_LIMIT

        await frontend._ingest_event(self._dm_event("203.0", CONTINUE_COMMAND))

        assert frontend._reply_counts[session_id] == 0
        frontend._on_message.assert_not_awaited()

    async def test_send_increments_reply_count(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(session_id, Response(body="hello"))

        assert frontend._reply_counts[session_id] == 1


# ---------------------------------------------------------------------------
# Session eviction (memory bound)
# ---------------------------------------------------------------------------


class TestSessionEviction:
    def _dm_event(self, ts: str, thread_ts: str | None = None) -> dict:
        event = {
            "ts": ts,
            "text": "hi",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        if thread_ts is not None:
            event["thread_ts"] = thread_ts
        return event

    async def test_evicts_oldest_over_cap(self, frontend):
        with patch("claude_on_the_fly.slack.SLACK_SESSION_CAP", 2):
            for ts in ("300.0", "301.0", "302.0"):
                await frontend._ingest_event(self._dm_event(ts))

        assert len(frontend._sessions) == 2
        oldest = _session_key("D1", "300.0")
        assert oldest not in frontend._sessions
        assert oldest not in frontend._sender_names
        assert oldest not in frontend._workspace_names
        assert oldest not in frontend._channel_contexts
        assert oldest not in frontend._session_sender_ids
        assert oldest not in frontend._reply_counts

    async def test_recent_activity_protects_session(self, frontend):
        with patch("claude_on_the_fly.slack.SLACK_SESSION_CAP", 2):
            await frontend._ingest_event(self._dm_event("400.0"))
            await frontend._ingest_event(self._dm_event("401.0"))
            # Touch the first thread again with a reply, moving it to the back.
            await frontend._ingest_event(self._dm_event("400.1", thread_ts="400.0"))
            # A new thread now evicts 401 (the oldest), not the touched 400.
            await frontend._ingest_event(self._dm_event("402.0"))

        assert _session_key("D1", "400.0") in frontend._sessions
        assert _session_key("D1", "401.0") not in frontend._sessions

    async def test_forget_session_clears_all_dicts(self, frontend):
        session_id = 12345
        frontend._sessions[session_id] = ("D1", "t")
        frontend._sender_names[session_id] = "hoss"
        frontend._reply_counts[session_id] = 5
        frontend._in_flight[session_id] = ("D1", "t")

        frontend._forget_session(session_id)

        assert session_id not in frontend._sessions
        assert session_id not in frontend._sender_names
        assert session_id not in frontend._reply_counts
        assert session_id not in frontend._in_flight


# ---------------------------------------------------------------------------
# Slash commands + skill picker (bot token only)
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_frontend():
    with patch("claude_on_the_fly.slack.AsyncApp") as mock_app_cls:
        mock_app = MagicMock()
        mock_app.client = MagicMock()
        mock_app.client.users_info = AsyncMock(
            return_value={"user": {"name": "testuser"}}
        )
        mock_app.client.conversations_info = AsyncMock(
            return_value={
                "channel": {"name": "general", "is_mpim": False, "is_private": False}
            }
        )
        mock_app.client.conversations_members = AsyncMock()
        mock_app.client.views_open = AsyncMock()
        mock_app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "111.222"}
        )
        mock_app_cls.return_value = mock_app

        fe = SlackFrontend("xapp-tok", "xoxb-tok", "U_SELF", allowed_user_ids={"*"})
        fe._on_message = AsyncMock()
        yield fe


class TestAppInteractionRegistration:
    """The picker and shortcut are app-scoped so they always register; the slash
    command is workspace-global and therefore opt-in."""

    def test_registers_the_command_when_set(self, bot_frontend):
        with patch("claude_on_the_fly.slack.SLASH_COMMAND", "/cof-hoss"):
            bot_frontend._register_app_interactions()
        bot_frontend._app.command.assert_called_once_with("/cof-hoss")

    def test_skips_the_command_when_unset(self, bot_frontend):
        with patch("claude_on_the_fly.slack.SLASH_COMMAND", None):
            bot_frontend._register_app_interactions()
        bot_frontend._app.command.assert_not_called()

    @pytest.mark.parametrize("command", ["/cof-hoss", None])
    def test_picker_and_shortcut_register_either_way(self, bot_frontend, command):
        with patch("claude_on_the_fly.slack.SLASH_COMMAND", command):
            bot_frontend._register_app_interactions()
        bot_frontend._app.view.assert_called_once_with("cof_picker")
        bot_frontend._app.shortcut.assert_called_once_with("cof_run_skill")


class TestSlashCommandRouting:
    async def test_forwards_skill_verbatim(self, bot_frontend):
        ack, respond = AsyncMock(), AsyncMock()
        command = {
            "text": "simplify make it lean",
            "channel_id": "D1",
            "user_id": "U_A",
        }
        await bot_frontend._handle_slash_command(
            ack, command, {"trigger_id": "t"}, respond
        )

        ack.assert_awaited()
        # A real anchor message is posted and the run is threaded under it.
        bot_frontend._app.client.chat_postMessage.assert_awaited_once()
        bot_frontend._on_message.assert_awaited_once()
        session_id, prompt = bot_frontend._on_message.await_args.args
        assert prompt == "/simplify make it lean"
        assert session_id == _session_key("D1", "111.222")

    async def test_bare_command_opens_picker(self, bot_frontend):
        ack, respond = AsyncMock(), AsyncMock()
        with (
            patch("claude_on_the_fly.slack.cached_skills", AsyncMock(return_value=[])),
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
        ):
            await bot_frontend._handle_slash_command(
                ack,
                {"text": "", "channel_id": "D1", "user_id": "U"},
                {"trigger_id": "trig"},
                respond,
            )
        bot_frontend._app.client.views_open.assert_awaited_once()
        kwargs = bot_frontend._app.client.views_open.await_args.kwargs
        assert kwargs["trigger_id"] == "trig"
        assert kwargs["view"]["callback_id"] == "cof_picker"
        bot_frontend._on_message.assert_not_awaited()


class TestSkillPicker:
    def test_option_groups_grouped_by_plugin(self):
        from claude_on_the_fly.slack import _skill_option_groups

        groups = _skill_option_groups(
            [("gf-qa:foo", "desc foo"), ("gf-qa:bar", ""), ("babysit", "triage")]
        )
        assert [g["label"]["text"] for g in groups] == ["gf-qa", "user"]  # sorted
        gfqa = {o["value"]: o for o in groups[0]["options"]}
        assert gfqa["gf-qa:foo"]["text"]["text"] == "foo"  # short name shown
        assert gfqa["gf-qa:foo"]["description"]["text"] == "desc foo"
        assert "description" not in gfqa["gf-qa:bar"]  # omitted when empty
        assert groups[1]["options"][0]["value"] == "babysit"  # plain -> "user"

    def test_option_groups_empty(self):
        from claude_on_the_fly.slack import _skill_option_groups

        assert _skill_option_groups([]) == []

    def test_long_description_truncated_to_one_line(self):
        from claude_on_the_fly.slack import SKILL_DESC_MAXLEN, _skill_option_groups

        long = "word " * 40  # ~200 chars, multi-word
        groups = _skill_option_groups([("plug:x", long)])
        text = groups[0]["options"][0]["description"]["text"]
        assert len(text) <= SKILL_DESC_MAXLEN + 1  # + the ellipsis
        assert text.endswith("…")
        assert "\n" not in text

    async def test_open_picker_builds_static_select(self, bot_frontend):
        skills = AsyncMock(return_value=[("gf-qa:foo", "d"), ("babysit", "")])
        with (
            patch("claude_on_the_fly.slack.cached_skills", skills),
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
        ):
            await bot_frontend._open_skill_picker("trig", "D1", "U", "5.0")
        view = bot_frontend._app.client.views_open.await_args.kwargs["view"]
        element = view["blocks"][0]["element"]
        assert element["type"] == "static_select"
        assert element["action_id"] == "cof_skill"
        assert {g["label"]["text"] for g in element["option_groups"]} == {
            "gf-qa",
            "user",
        }
        assert view["private_metadata"] == "D1:U:5.0"

    async def test_open_picker_empty_skills_drops_select(self, bot_frontend):
        with (
            patch("claude_on_the_fly.slack.cached_skills", AsyncMock(return_value=[])),
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
        ):
            await bot_frontend._open_skill_picker("trig", "D1", "U", None)
        view = bot_frontend._app.client.views_open.await_args.kwargs["view"]
        assert view["blocks"][0]["type"] == "section"  # message, no select


class TestPickerSubmit:
    async def test_forwards_selected_skill_with_args(self, bot_frontend):
        ack = AsyncMock()
        view = {
            "private_metadata": "D1:U_A",
            "state": {
                "values": {
                    "skill": {"cof_skill": {"selected_option": {"value": "simplify"}}},
                    "args": {"cof_args": {"value": "trim it"}},
                }
            },
        }
        await bot_frontend._handle_picker_submit(ack, view)
        ack.assert_awaited()
        # No thread in metadata (bare picker) -> anchors to a posted message.
        bot_frontend._app.client.chat_postMessage.assert_awaited_once()
        bot_frontend._on_message.assert_awaited_once()
        session_id, prompt = bot_frontend._on_message.await_args.args
        assert prompt == "/simplify trim it"
        assert session_id == _session_key("D1", "111.222")

    async def test_no_selection_is_noop(self, bot_frontend):
        ack = AsyncMock()
        view = {"private_metadata": "D1:U", "state": {"values": {}}}
        await bot_frontend._handle_picker_submit(ack, view)
        bot_frontend._on_message.assert_not_awaited()


class TestStartGatesOnToken:
    async def test_bot_token_registers_commands(self, bot_frontend):
        bot_frontend._register_app_interactions = MagicMock()
        with (
            patch("claude_on_the_fly.slack.AsyncSocketModeHandler") as handler_cls,
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
            patch("claude_on_the_fly.slack.cached_skills", AsyncMock(return_value=[])),
        ):
            handler_cls.return_value.start_async = AsyncMock()
            await bot_frontend.start(AsyncMock())
        bot_frontend._register_app_interactions.assert_called_once()
        if bot_frontend._warm_task:
            await bot_frontend._warm_task

    async def test_user_token_skips_commands(self, frontend):
        frontend._register_app_interactions = MagicMock()
        with patch("claude_on_the_fly.slack.AsyncSocketModeHandler") as handler_cls:
            handler_cls.return_value.start_async = AsyncMock()
            await frontend.start(AsyncMock())
        frontend._register_app_interactions.assert_not_called()
        assert frontend._warm_task is None


# ---------------------------------------------------------------------------
# $stop text prefix (works in threads; both token kinds)
# ---------------------------------------------------------------------------


class TestStopPrefix:
    async def test_aborts_and_does_not_forward(self, frontend):
        orch = MagicMock()
        orch.abort = AsyncMock(return_value=True)
        frontend._orchestrator = orch
        event = {
            "ts": "9.0",
            "text": "$stop",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.abort.assert_awaited_once_with(_session_key("D1", "9.0"))
        frontend._on_message.assert_not_awaited()
        frontend._app.client.chat_postMessage.assert_awaited()

    async def test_targets_thread_session(self, frontend):
        orch = MagicMock()
        orch.abort = AsyncMock(return_value=False)
        frontend._orchestrator = orch
        event = {
            "ts": "9.9",
            "thread_ts": "5.0",
            "text": "$stop",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.abort.assert_awaited_once_with(_session_key("D1", "5.0"))
        frontend._on_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Message shortcut (thread-aware picker)
# ---------------------------------------------------------------------------


class TestRunSkillShortcut:
    async def test_opens_thread_scoped_picker(self, bot_frontend):
        ack = AsyncMock()
        shortcut = {
            "trigger_id": "trig",
            "channel": {"id": "D1"},
            "user": {"id": "U_A"},
            "message": {"ts": "1.0", "thread_ts": "5.0"},
        }
        with (
            patch("claude_on_the_fly.slack.cached_skills", AsyncMock(return_value=[])),
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
        ):
            await bot_frontend._handle_run_skill_shortcut(ack, shortcut)
        ack.assert_awaited()
        assert bot_frontend._app.client.views_open.await_args is not None
        view = bot_frontend._app.client.views_open.await_args.kwargs["view"]
        assert view["private_metadata"] == "D1:U_A:5.0"

    async def test_root_message_uses_its_ts(self, bot_frontend):
        ack = AsyncMock()
        shortcut = {
            "trigger_id": "t",
            "channel": {"id": "D1"},
            "user": {"id": "U"},
            "message": {"ts": "1.0"},
        }
        with (
            patch("claude_on_the_fly.slack.cached_skills", AsyncMock(return_value=[])),
            patch("claude_on_the_fly.slack.get_backend", MagicMock()),
        ):
            await bot_frontend._handle_run_skill_shortcut(ack, shortcut)
        assert bot_frontend._app.client.views_open.await_args is not None
        view = bot_frontend._app.client.views_open.await_args.kwargs["view"]
        assert view["private_metadata"] == "D1:U:1.0"

    async def test_picker_submit_forwards_into_thread(self, bot_frontend):
        ack = AsyncMock()
        view = {
            "private_metadata": "D1:U_A:5.0",
            "state": {
                "values": {
                    "skill": {"cof_skill": {"selected_option": {"value": "simplify"}}},
                    "args": {"cof_args": {"value": ""}},
                }
            },
        }
        await bot_frontend._handle_picker_submit(ack, view)
        # Thread already known (message shortcut) -> reuse it, no anchor post.
        bot_frontend._app.client.chat_postMessage.assert_not_awaited()
        bot_frontend._on_message.assert_awaited_once()
        session_id, prompt = bot_frontend._on_message.await_args.args
        assert prompt == "/simplify"
        assert session_id == _session_key("D1", "5.0")


# ---------------------------------------------------------------------------
# Bot-only progress indicator (assistant.threads.setStatus)
# ---------------------------------------------------------------------------


class TestStatusIndicator:
    async def test_bot_sets_status_when_thread_present(self, bot_frontend):
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await bot_frontend._set_status(sid, "is thinking...")
        bot_frontend._app.client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="5.0", status="is thinking..."
        )

    async def test_user_token_skips_status(self, frontend):
        sid = _session_key("D1", "5.0")
        frontend._sessions[sid] = ("D1", "5.0")
        frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await frontend._set_status(sid, "is thinking...")
        frontend._app.client.assistant_threads_setStatus.assert_not_awaited()

    async def test_no_thread_skips_status(self, bot_frontend):
        sid = _session_key("D1", None)
        bot_frontend._sessions[sid] = ("D1", None)
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await bot_frontend._set_status(sid, "is thinking...")
        bot_frontend._app.client.assistant_threads_setStatus.assert_not_awaited()

    async def test_status_failure_is_swallowed(self, bot_frontend):
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock(
            side_effect=Exception("not an assistant thread")
        )
        # Must not raise — degrades to the emoji reaction already shown.
        await bot_frontend._set_status(sid, "is thinking...")

    async def test_send_typing_updates_live_status(self, bot_frontend):
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._status_started[sid] = time.monotonic() - 12
        bot_frontend._status_verbs[sid] = ["percolating"]
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await bot_frontend.send_typing(sid)
        call = bot_frontend._app.client.assistant_threads_setStatus.await_args
        assert call is not None
        status = call.kwargs["status"]
        assert status.startswith("is ") and status.endswith("s)")

    async def test_verb_rotates_through_the_fixed_sequence(self, bot_frontend):
        # The order is shuffled once at notify_start; ticks walk that fixed
        # sequence by elapsed time — no per-tick random draw.
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._status_verbs[sid] = ["alpha", "beta", "gamma"]
        set_status = AsyncMock()
        bot_frontend._app.client.assistant_threads_setStatus = set_status

        bot_frontend._status_started[sid] = time.monotonic()  # elapsed ~0 -> idx 0
        await bot_frontend.send_typing(sid)
        bot_frontend._status_started[sid] = time.monotonic() - 6  # ~6s -> idx 1
        await bot_frontend.send_typing(sid)

        statuses = [c.kwargs["status"] for c in set_status.await_args_list]
        assert statuses[0].startswith("is alpha… (")
        assert statuses[1].startswith("is beta… (")

    async def test_send_typing_noop_without_active_turn(self, bot_frontend):
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await bot_frontend.send_typing(sid)
        bot_frontend._app.client.assistant_threads_setStatus.assert_not_awaited()

    async def test_notify_start_sets_status_without_pending_reaction(
        self, bot_frontend
    ):
        # Slash/picker forwards have a session route but no _pending_msg entry;
        # the status must still fire (regression: it used to sit behind the
        # pending-msg early return).
        sid = _session_key("D1", "5.0")
        bot_frontend._sessions[sid] = ("D1", "5.0")
        bot_frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await bot_frontend.notify_start(sid)
        bot_frontend._app.client.assistant_threads_setStatus.assert_awaited_once()
        assert sid in bot_frontend._status_started


# ---------------------------------------------------------------------------
# Allowlist enforced on every command/shortcut/picker path
# ---------------------------------------------------------------------------


class TestAllowlistGate:
    async def test_slash_command_denied_for_unlisted_user(self, bot_frontend):
        bot_frontend._allow_all_senders = False
        bot_frontend._allowed_user_ids = {"U_OK"}
        ack, respond = AsyncMock(), AsyncMock()
        command = {"text": "simplify", "channel_id": "D1", "user_id": "U_BAD"}
        await bot_frontend._handle_slash_command(
            ack, command, {"trigger_id": "t"}, respond
        )
        respond.assert_awaited_with("Not authorized.")
        bot_frontend._on_message.assert_not_awaited()

    async def test_blocked_sender_denied_even_with_wildcard(self, bot_frontend):
        # bot_frontend allows "*"; a blocked id must still be refused.
        bot_frontend._blocked_senders = {"U_BAD"}
        ack, respond = AsyncMock(), AsyncMock()
        command = {"text": "simplify", "channel_id": "D1", "user_id": "U_BAD"}
        await bot_frontend._handle_slash_command(
            ack, command, {"trigger_id": "t"}, respond
        )
        respond.assert_awaited_with("Not authorized.")
        bot_frontend._on_message.assert_not_awaited()

    async def test_shortcut_denied_for_unlisted_user(self, bot_frontend):
        bot_frontend._allow_all_senders = False
        bot_frontend._allowed_user_ids = {"U_OK"}
        ack = AsyncMock()
        shortcut = {
            "trigger_id": "t",
            "channel": {"id": "D1"},
            "user": {"id": "U_BAD"},
            "message": {"ts": "1.0"},
        }
        await bot_frontend._handle_run_skill_shortcut(ack, shortcut)
        bot_frontend._app.client.views_open.assert_not_awaited()

    async def test_picker_submit_denied_for_unlisted_user(self, bot_frontend):
        bot_frontend._allow_all_senders = False
        bot_frontend._allowed_user_ids = {"U_OK"}
        ack = AsyncMock()
        view = {
            "private_metadata": "D1:U_BAD:",
            "state": {
                "values": {
                    "skill": {"cof_skill": {"selected_option": {"value": "simplify"}}},
                    "args": {"cof_args": {"value": ""}},
                }
            },
        }
        await bot_frontend._handle_picker_submit(ack, view)
        bot_frontend._on_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bot-token DM gate: only act on DMs the bot itself is in
# ---------------------------------------------------------------------------


class TestBotConversationGate:
    async def test_own_dm_is_bot_conversation(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            return_value={"channel": {"is_im": True}}
        )
        assert await bot_frontend._is_bot_conversation("D1") is True

    async def test_foreign_dm_is_not(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("x", {"ok": False, "error": "channel_not_found"})
        )
        assert await bot_frontend._is_bot_conversation("DX") is False

    async def test_ambiguous_error_fails_open(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("x", {"ok": False, "error": "ratelimited"})
        )
        assert await bot_frontend._is_bot_conversation("DY") is True

    async def test_ingest_skips_foreign_dm(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("x", {"ok": False, "error": "channel_not_found"})
        )
        await bot_frontend._ingest_event(
            {
                "ts": "1.0",
                "text": "hi",
                "channel": "DX",
                "channel_type": "im",
                "user": "U_ALLOWED",
            }
        )
        bot_frontend._on_message.assert_not_awaited()

    async def test_ingest_processes_own_dm(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            return_value={"channel": {"is_im": True}}
        )
        await bot_frontend._ingest_event(
            {
                "ts": "2.0",
                "text": "hi",
                "channel": "D1",
                "channel_type": "im",
                "user": "U_ALLOWED",
            }
        )
        bot_frontend._on_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# $job — background-job producer intercept
# ---------------------------------------------------------------------------


class _FakeJobQueue:
    """Records enqueued jobs; the rest of the port is inert for producer tests."""

    def __init__(self, unfinished: list | None = None) -> None:
        self.jobs: list = []
        self.unfinished = list(unfinished or [])
        self.list_limits: list[int] = []

    def enqueue(self, job) -> None:
        self.jobs.append(job)

    def claim(self):
        return None

    def complete(self, job, result) -> None:
        pass

    def mark_delivered(self, job_id) -> None:
        pass

    def undelivered(self):
        return []

    def list_unfinished(self, limit):
        self.list_limits.append(limit)
        return self.unfinished[:limit]

    def recover_stale(self, ttl_s):
        return 0


class TestJobCommand:
    @pytest.fixture(autouse=True)
    def _clear_job_command_env(self, monkeypatch):
        # The trigger resolves per instance from SLACK_JOB_COMMAND, so a real
        # value in the developer's shell would otherwise decide what this class
        # tests. Clear it and let `_frontend` pass the trigger explicitly.
        # (Function-scoped: monkeypatch cannot be requested by a class fixture.)
        monkeypatch.delenv("SLACK_JOB_COMMAND", raising=False)

    def _frontend(self, queue=None, job_command="$job"):
        with patch("claude_on_the_fly.slack.AsyncApp") as mock_app_cls:
            mock_app = MagicMock()
            mock_app.client = MagicMock()
            mock_app.client.chat_postMessage = AsyncMock(
                return_value={"ok": True, "ts": "99.9"}
            )
            mock_app_cls.return_value = mock_app
            fe = SlackFrontend(
                "xapp-tok",
                "xoxp-tok",
                "U_SELF",
                allowed_user_ids={"U_ALLOWED"},
                job_command=job_command,
                job_queue=queue,
            )
            fe._on_message = AsyncMock()
            return fe, mock_app

    async def test_job_command_enqueues_and_acks(self):
        queue = _FakeJobQueue()
        fe, mock_app = self._frontend(queue)
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job summarize the incident",
            }
        )

        # Exactly one job, with the task and the origin (carrying sender_id).
        assert len(queue.jobs) == 1
        job = queue.jobs[0]
        assert job.prompt == "summarize the incident"
        assert job.origin == {
            "channel": "C1",
            "thread_ts": "123.45",
            "sender_id": "U_ALLOWED",
        }
        # Acked immediately; no agent turn in the chat process.
        mock_app.client.chat_postMessage.assert_awaited()
        fe._on_message.assert_not_awaited()
        # No orphaned pending message/reaction (returned before _pending_msg).
        assert not fe._pending_msg
        # ts marked processed so a catchup re-ingest won't enqueue a duplicate.
        assert "123.45" in fe._processed_ts

    async def test_job_command_advances_catchup_watermark(self):
        # A $job in a channel with no normal traffic since restart must still
        # register the channel as active (with its watermark and type), or
        # _catchup never re-fetches it and a message lost during a disconnect is
        # silently dropped. Mirrors the normal path's bookkeeping.
        queue = _FakeJobQueue()
        fe, _ = self._frontend(queue)
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job summarize the incident",
            }
        )
        # Guard against vacuity: the normal message path sets both of these too
        # (slack.py, just below the job branch), so without an assertion that the
        # job branch is the one that ran, this test passes with the feature
        # entirely disabled.
        assert len(queue.jobs) == 1
        assert fe._active_channels["C1"] == "123.45"
        assert fe._channel_types["C1"] == "im"

    async def test_job_command_uses_thread_ts_when_in_thread(self):
        queue = _FakeJobQueue()
        fe, _ = self._frontend(queue)
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "200.0",
                "thread_ts": "100.0",
                "text": "$job do the thing",
            }
        )
        assert queue.jobs[0].origin["thread_ts"] == "100.0"

    async def test_empty_job_command_posts_usage_and_enqueues_nothing(self):
        queue = _FakeJobQueue()
        fe, mock_app = self._frontend(queue)
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job",
            }
        )
        assert queue.jobs == []
        mock_app.client.chat_postMessage.assert_awaited()  # usage notice
        fe._on_message.assert_not_awaited()

    async def test_usage_notice_names_the_configured_trigger(self):
        # The notice interpolates the configured trigger. Nothing else would
        # catch a regression to a hardcoded `$job`: the test above runs under
        # the default trigger and only asserts that *something* was posted, so
        # the notice would keep telling every operator on another trigger to
        # type a string their install does not answer to.
        queue = _FakeJobQueue()
        fe, mock_app = self._frontend(queue, job_command="!bg")
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "!bg",
            }
        )
        assert queue.jobs == []
        assert "!bg" in mock_app.client.chat_postMessage.await_args.kwargs["text"]

    async def test_job_command_enqueue_failure_is_reported(self):
        class _BoomQueue(_FakeJobQueue):
            def enqueue(self, job):
                raise RuntimeError("disk full")

        fe, mock_app = self._frontend(_BoomQueue())
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job something",
            }
        )
        # A failure notice is posted; no agent turn kicked off.
        mock_app.client.chat_postMessage.assert_awaited()
        fe._on_message.assert_not_awaited()

    async def test_non_job_message_still_reaches_agent(self):
        queue = _FakeJobQueue()
        fe, _ = self._frontend(queue)
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "just a normal question",
            }
        )
        assert queue.jobs == []
        fe._on_message.assert_awaited_once()

    async def test_disabled_trigger_degrades_to_an_ordinary_message(self, monkeypatch):
        # Turning the trigger off (SLACK_JOB_COMMAND=) puts the text back on
        # the ordinary path rather than dropping it silently. Both assertions flip when the trigger is set (the job
        # branch enqueues and returns before _on_message), so this discriminates.
        # Note it does NOT assert the ts is absent from _processed_ts: the normal
        # path marks it processed too, so that would hold either way.
        queue = _FakeJobQueue()
        fe, _ = self._frontend(queue, job_command="")
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job summarize the incident",
            }
        )
        assert queue.jobs == []
        fe._on_message.assert_awaited_once()

    async def test_custom_trigger_replaces_the_default(self):
        # The trigger is whatever the operator named — "$job" holds no special
        # status once it is read from the environment.
        queue = _FakeJobQueue()
        fe, _ = self._frontend(queue, job_command="!bg")
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "1.0",
                "text": "!bg do it",
            }
        )
        assert [job.prompt for job in queue.jobs] == ["do it"]

        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "2.0",
                "text": "$job do it",
            }
        )
        # The former hardcoded default is now just text, and goes to the agent.
        assert [job.prompt for job in queue.jobs] == ["do it"]
        fe._on_message.assert_awaited_once()

    async def test_no_queue_is_built_when_the_trigger_is_disabled(self, monkeypatch):
        # Nothing can enqueue with the feature off, so touching the queue at all
        # would be work — and a filesystem dependency — for no reason.
        made = MagicMock()
        monkeypatch.setattr("claude_on_the_fly.slack.make_queue", made)
        fe, _ = self._frontend(job_command="")
        assert fe._job_queue is None
        made.assert_not_called()

    async def test_queue_is_built_when_the_trigger_is_set(self, monkeypatch):
        sentinel = _FakeJobQueue()
        made = MagicMock(return_value=sentinel)
        monkeypatch.setattr("claude_on_the_fly.slack.make_queue", made)
        fe, _ = self._frontend()
        assert fe._job_queue is sentinel
        made.assert_called_once()


def test_job_command_reads_its_documented_env_var(monkeypatch):
    """Every other test passes the trigger explicitly, so a typo in the env var
    name — SLACK_JOBS_COMMAND in slack.py against SLACK_JOB_COMMAND in checks.py
    and the docs — would pass the entire suite. Pin which name the constructor
    reads, against a real environment.
    """
    with patch("claude_on_the_fly.slack.AsyncApp"):
        monkeypatch.setenv("SLACK_JOB_COMMAND", "!bg")
        with patch("claude_on_the_fly.slack.make_queue"):
            fe = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert fe._job_command == "!bg"

        # Absent means the default — the feature is on without configuration.
        monkeypatch.delenv("SLACK_JOB_COMMAND", raising=False)
        with patch("claude_on_the_fly.slack.make_queue"):
            fe = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert fe._job_command == DEFAULT_JOB_COMMAND

        # Present but blank is the opt-out, and the only one.
        monkeypatch.setenv("SLACK_JOB_COMMAND", "")
        fe = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert fe._job_command is None
        assert fe._job_queue is None


async def test_injected_queue_enables_the_feature_without_patching_globals():
    """The constructor seam has to work on its own. The intercept used to gate
    on an import-time module global, so passing a queue could not switch the
    feature on and every caller had to reach in and patch that too."""
    with patch("claude_on_the_fly.slack.AsyncApp"):
        queue = _FakeJobQueue()
        fe = SlackFrontend(
            "xapp-tok",
            "xoxp-tok",
            "U_SELF",
            allowed_user_ids={"U_ALLOWED"},
            job_command="$job",
            job_queue=queue,
        )
        fe._on_message = AsyncMock()
        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "1.0",
                "text": "$job do the thing",
            }
        )

    assert [job.prompt for job in queue.jobs] == ["do the thing"]
    fe._on_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bare-trigger job listing
# ---------------------------------------------------------------------------


def _row(job_id, prompt, channel, *, in_flight=False, age_s=0):
    from claude_on_the_fly.jobs.core import QueueRow

    return QueueRow(
        id=job_id,
        prompt=prompt,
        origin={"channel": channel},
        enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        in_flight=in_flight,
    )


class TestJobListing:
    def test_lists_this_channels_jobs_with_state_and_age(self):
        rows = [
            _row("1-a", "rebuild the index", "C1", in_flight=True, age_s=125),
            _row("2-b", "check the deploy", "C1", age_s=30),
        ]
        out = _render_job_list(rows, "C1", "$job")

        assert "2 job(s) from this channel" in out
        assert "`running` · 2m · rebuild the index" in out
        assert "`queued` · 30s · check the deploy" in out
        assert "Usage: `$job <task>`" in out

    def test_other_channels_are_counted_never_quoted(self):
        """A listing prints prompts verbatim, and a shared channel's queue holds
        work from threads its readers were never part of."""
        rows = [
            _row("1-a", "mine", "C1"),
            _row("2-b", "someone else's secret plan", "C2"),
            _row("3-c", "another", "C3"),
        ]
        out = _render_job_list(rows, "C1", "$job")

        assert "mine" in out
        assert "secret plan" not in out
        assert "2 job(s) queued from other channels" in out

    def test_empty_queue_says_so_and_still_shows_usage(self):
        out = _render_job_list([], "C1", "$job")
        assert "No jobs queued from this channel." in out
        assert "Usage: `$job <task>`" in out

    def test_listing_is_capped_and_says_how_many_it_hid(self):
        rows = [_row(f"{i}-a", f"job {i}", "C1") for i in range(25)]
        out = _render_job_list(rows, "C1", "$job")

        assert out.count("• ") == JOB_LIST_LIMIT
        assert f"…and {25 - JOB_LIST_LIMIT} more." in out

    def test_long_prompt_is_truncated_into_one_line(self):
        rows = [_row("1-a", "x" * 400 + "\nsecond line", "C1")]
        out = _render_job_list(rows, "C1", "$job")

        listed = next(line for line in out.split("\n") if line.startswith("• "))
        assert "\n" not in listed
        assert len(listed) < 140

    def test_missing_timestamp_degrades_to_one_cell(self):
        from claude_on_the_fly.jobs.core import QueueRow

        rows = [
            QueueRow(
                id="hand-written",
                prompt=None,
                origin={"channel": "C1"},
                enqueued_at=None,
                in_flight=False,
            )
        ]
        out = _render_job_list(rows, "C1", "$job")

        assert "`queued` · ? · (no prompt)" in out


class TestBareTriggerListsJobs(TestJobCommand):
    async def test_bare_trigger_posts_the_listing_not_just_usage(self):
        queue = _FakeJobQueue(
            unfinished=[
                _row("1-a", "rebuild the index", "C1", in_flight=True),
                _row("2-b", "elsewhere", "C-other"),
            ]
        )
        fe, mock_app = self._frontend(queue)

        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job",
            }
        )

        assert queue.jobs == []  # a listing must never enqueue
        posted = mock_app.client.chat_postMessage.call_args.kwargs["text"]
        assert "rebuild the index" in posted
        assert "elsewhere" not in posted
        assert "1 job(s) queued from other channels" in posted
        fe._on_message.assert_not_awaited()

    async def test_listing_survives_a_queue_that_cannot_be_read(self):
        """A chat turn has no business failing over a filesystem the listing is
        only a courtesy about."""

        class _BrokenQueue(_FakeJobQueue):
            def list_unfinished(self, limit):
                raise OSError("queue directory is gone")

        fe, mock_app = self._frontend(_BrokenQueue())

        await fe._ingest_event(
            {
                "user": "U_ALLOWED",
                "channel": "C1",
                "channel_type": "im",
                "ts": "123.45",
                "text": "$job",
            }
        )

        posted = mock_app.client.chat_postMessage.call_args.kwargs["text"]
        assert "No jobs queued from this channel." in posted
        assert "Usage:" in posted
