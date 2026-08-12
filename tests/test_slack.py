"""Tests for claude_on_the_fly.slack module."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from claude_on_the_fly import settings
from claude_on_the_fly import slack as slack_mod
from claude_on_the_fly.agent import Response
from claude_on_the_fly.slack import (
    CONTINUE_COMMAND,
    DEFAULT_JOB_COMMAND,
    DEFAULT_REPLY_SOFT_LIMIT,
    JOB_LIST_LIMIT,
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
        fe._workspace_names[42] = "dm-user-123"
        assert fe.workspace_name(42) == "slack/dm-user-123"

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
def frontend(monkeypatch):
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
        frontend._pinned_allowed_user_ids = {"*"}
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
        frontend._pinned_allowed_user_ids = {"*"}
        frontend._pinned_blocked_senders = {"U_BANNED"}
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

    async def test_an_untagged_channel_message_schedules_a_notice(self, frontend):
        """Dropped, but not silently: a thread the bot is already in gets told it
        needs a tag. Posted later (see TestMentionNotice), never inline."""
        session_id = _session_key("C1", "5.1")
        frontend._sessions[session_id] = ("C1", "5.1")
        event = {
            "ts": "5.2",
            "thread_ts": "5.1",
            "text": "and one more thing",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
        }

        await frontend._ingest_event(event)

        frontend._on_message.assert_not_awaited()
        frontend._app.client.chat_postMessage.assert_not_called()
        assert session_id in frontend._mention_notices
        frontend._cancel_mention_notice(session_id)

    async def test_an_untagged_message_in_an_unknown_thread_is_silent(self, frontend):
        """Ordinary channel chatter the bot was never part of. Answering it would make
        the bot talk in threads nobody invited it into."""
        event = {
            "ts": "5.4",
            "text": "unrelated chatter",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
        }

        await frontend._ingest_event(event)

        frontend._on_message.assert_not_awaited()
        frontend._app.client.chat_postMessage.assert_not_called()

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
        frontend._pinned_allowed_user_ids = {"*"}
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
        assert '[from-id: U_ALLOWED] [display: "testuser"]' in text

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
# send_progress
# ---------------------------------------------------------------------------


def _seed_progress_route(frontend, channel_type: str | None = "im") -> int:
    """Route + channel type + an ok response.

    The shared fixture's `chat_postMessage` has no `return_value`, so `resp["ts"]`
    would otherwise be a MagicMock; and its `conversations_info` stub answers with
    a channel that is neither an im nor an mpim, so an unseeded type cache
    resolves to "channel" and nothing posts.
    """
    session_id = _session_key("C1", "t1")
    frontend._sessions[session_id] = ("C1", "t1")
    if channel_type is not None:
        frontend._channel_types["C1"] = channel_type
    frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}
    return session_id


class TestSendProgress:
    async def test_posts_a_context_block_into_the_thread(self, frontend):
        session_id = _seed_progress_route(frontend)

        await frontend.send_progress(session_id, "still working")

        kwargs = frontend._app.client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "t1"
        assert kwargs["blocks"][0]["type"] == "context"
        assert kwargs["text"].startswith(slack_mod.INTERIM_PREFIX)
        assert "still working" in kwargs["blocks"][0]["elements"][0]["text"]

    async def test_records_the_ts_so_it_is_not_re_ingested(self, frontend):
        """Under a user token our own post comes back as an event, and `_catchup`
        re-reads it after a reconnect without passing Bolt's self-event filter."""
        session_id = _seed_progress_route(frontend)

        await frontend.send_progress(session_id, "working")
        assert "99.0" in frontend._our_sent_timestamps

        await frontend._ingest_event(
            {"ts": "99.0", "channel": "C1", "user": "U_ALLOWED", "text": "working"}
        )
        frontend._on_message.assert_not_awaited()

    async def test_does_not_count_against_the_reply_budget(self, frontend):
        session_id = _seed_progress_route(frontend)

        for _ in range(3):
            await frontend.send_progress(session_id, "working")
        assert frontend._reply_counts.get(session_id, 0) == 0

        await frontend.send(session_id, Response(body="x"))
        assert frontend._reply_counts[session_id] == 1

    async def test_a_group_dm_is_allowed(self, frontend):
        session_id = _seed_progress_route(frontend, "mpim")

        await frontend.send_progress(session_id, "working")

        frontend._app.client.chat_postMessage.assert_awaited_once()

    async def test_a_shared_channel_gets_no_progress(self, frontend, caplog):
        """Interim posts bypass the reply budget, so nothing would cap how much
        narration a heavy turn pushes at bystanders."""
        session_id = _seed_progress_route(frontend, "channel")

        with caplog.at_level("DEBUG", logger="claude_on_the_fly.slack"):
            await frontend.send_progress(session_id, "working")

        frontend._app.client.chat_postMessage.assert_not_awaited()
        assert "outside a DM/group DM" in caplog.text

    async def test_an_unknown_channel_type_gets_no_progress(self, frontend):
        """Fail-closed: a missed progress line costs one silent turn, a wrong one
        puts narration in a team channel."""
        session_id = _seed_progress_route(frontend, channel_type=None)
        frontend._app.client.conversations_info.side_effect = Exception("no such")

        await frontend.send_progress(session_id, "working")

        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_no_session_posts_nothing(self, frontend):
        await frontend.send_progress(4242, "working")

        frontend._app.client.chat_postMessage.assert_not_awaited()

    async def test_silenced_sender_gets_no_progress(self, frontend):
        """A silent sender is contracted to get no reply; forwarding its narration
        would break that. The type cache is left empty on purpose, so the absent
        `conversations_info` call proves the cheap guard runs first."""
        session_id = _seed_progress_route(frontend, channel_type=None)
        frontend._in_flight_reply_suppressed[session_id] = True

        await frontend.send_progress(session_id, "working")

        frontend._app.client.chat_postMessage.assert_not_awaited()
        frontend._app.client.conversations_info.assert_not_awaited()

    async def test_markdown_is_converted(self, frontend):
        session_id = _seed_progress_route(frontend)

        await frontend.send_progress(session_id, "**bold**")

        assert (
            "*bold*" in frontend._app.client.chat_postMessage.call_args.kwargs["text"]
        )

    async def test_a_very_long_line_is_truncated_not_dropped(self, frontend):
        """One honest truncated line beats fourteen blocks of progress noise."""
        session_id = _seed_progress_route(frontend, "im")

        await frontend.send_progress(session_id, "x" * 5000)

        rendered = frontend._app.client.chat_postMessage.call_args.kwargs["blocks"][0][
            "elements"
        ][0]["text"]
        assert len(rendered) < SLACK_BLOCK_LIMIT
        assert "more characters" in rendered
        assert "xxxxxxxxxx" in rendered

    async def test_a_slack_error_is_swallowed(self, frontend, caplog):
        session_id = _seed_progress_route(frontend)
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=SlackApiError("nope", {"ok": False, "error": "rate_limited"})
        )

        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            await frontend.send_progress(session_id, "working")

        assert "failed to post" in caplog.text

    async def test_a_not_ok_response_records_nothing(self, frontend):
        session_id = _seed_progress_route(frontend)
        frontend._app.client.chat_postMessage.return_value = {
            "ok": False,
            "error": "channel_not_found",
        }

        await frontend.send_progress(session_id, "working")

        assert list(frontend._our_sent_timestamps) == []


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
        assert frontend._workspace_names[100] == "dm-hoss-123-456"
        assert frontend._channel_contexts[100] == "dm (private)"

    async def test_two_messages_in_one_second_get_separate_workspaces(self, frontend):
        """`_session_key` hashes the full thread_ts, so sub-second-apart
        messages are separate sessions. Their workspaces must be separate too:
        the directory is the agent's cwd and where `_save_files` writes Slack
        attachments, so sharing one lets concurrent sessions overwrite or
        cross-read each other's downloads. Slack emits duplicate notifications
        inside one second routinely."""
        await frontend._resolve_session_metadata(
            101, "bot", "D1", "im", "1786342813.662689"
        )
        await frontend._resolve_session_metadata(
            102, "bot", "D1", "im", "1786342813.872239"
        )
        assert frontend._workspace_names[101] != frontend._workspace_names[102]

    async def test_thread_ts_fraction_survives_in_a_channel_workspace(self, frontend):
        """Every workspace name runs through the same `short_ts`, so the
        channel and mpim branches must not collide either."""
        frontend._app.client.conversations_info.return_value = {
            "channel": {"name": "general", "is_mpim": False, "is_private": False}
        }
        await frontend._resolve_session_metadata(
            203, "hoss", "C1", "channel", "1786342813.662689"
        )
        assert frontend._workspace_names[203] == "general-1786342813-662689"

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
# persona_source
# ---------------------------------------------------------------------------


class TestPersonaSource:
    """The keys `persona_source` hands to `agent.persona_for`. What those keys then
    resolve to is `TestPersonaFor` in test_agent.py."""

    def _keys(self, frontend, chat_id: int) -> tuple[str, ...]:
        with patch("claude_on_the_fly.slack.persona_for", return_value=None) as spy:
            assert frontend.persona_source(chat_id) is None
        return spy.call_args[0][1]

    async def test_a_channel_is_keyed_by_id_then_name(self, frontend):
        frontend._remember_session(200, "C1", "1.0")
        await frontend._resolve_session_metadata(200, "hoss", "C1", "channel", "1.0")
        assert self._keys(frontend, 200) == ("C1", "general")

    async def test_an_unresolvable_channel_is_still_keyed_by_id(self, frontend):
        """Not by the sender: it is a channel whose name we could not read, and
        keying a channel on whoever spoke last would flip its persona mid-thread."""
        frontend._app.client.conversations_info.side_effect = Exception("boom")
        frontend._remember_session(400, "C99", "5.0")
        frontend._session_sender_ids[400] = "U_ALLOWED"
        await frontend._resolve_session_metadata(400, "hoss", "C99", "channel", "5.0")
        assert self._keys(frontend, 400) == ("C99", "C99")

    async def test_a_dm_is_keyed_by_channel_then_sender_then_dm(self, frontend):
        frontend._remember_session(100, "D1", None)
        frontend._session_sender_ids[100] = "U_ALLOWED"
        await frontend._resolve_session_metadata(100, "hoss", "D1", "im", "")
        assert self._keys(frontend, 100) == ("D1", "U_ALLOWED", "dm")

    async def test_a_group_dm_is_keyed_like_a_dm(self, frontend):
        frontend._app.client.conversations_info.return_value = {
            "channel": {"name": "mpdm-a-b", "is_mpim": True}
        }
        frontend._app.client.conversations_members.return_value = {"members": ["U_A"]}
        frontend._remember_session(300, "G1", "1.0")
        frontend._session_sender_ids[300] = "U_A"
        await frontend._resolve_session_metadata(300, "hoss", "G1", "mpim", "1.0")
        assert self._keys(frontend, 300) == ("G1", "U_A", "dm")

    async def test_a_dm_with_no_known_sender_drops_that_key(self, frontend):
        """A message with no `user` field (a bot post) resolves the session without
        a sender id. The empty key must not reach the config lookup."""
        frontend._remember_session(101, "D2", None)
        await frontend._resolve_session_metadata(101, "hoss", "D2", "im", "")
        assert self._keys(frontend, 101) == ("D2", "dm")

    def test_an_unknown_session_asks_for_the_dm_default(self, frontend):
        assert self._keys(frontend, 999) == ("dm",)

    async def test_the_resolved_file_is_returned(self, frontend, tmp_path):
        persona = tmp_path / "oncall.md"
        persona.write_text("# oncall")
        frontend._remember_session(200, "C1", "1.0")
        await frontend._resolve_session_metadata(200, "hoss", "C1", "channel", "1.0")
        with patch("claude_on_the_fly.slack.persona_for", return_value=persona):
            assert frontend.persona_source(200) == persona

    async def test_a_forgotten_session_drops_its_channel_name(self, frontend):
        frontend._remember_session(200, "C1", "1.0")
        await frontend._resolve_session_metadata(200, "hoss", "C1", "channel", "1.0")
        frontend._forget_session(200)
        assert 200 not in frontend._channel_names


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
        mock_resp.content.read = AsyncMock(return_value=content)

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

    async def test_a_download_over_the_cap_is_refused(self, tmp_path, monkeypatch):
        """The read asks for one byte past the cap so the overflow is visible
        without buffering the whole body."""
        monkeypatch.setattr("claude_on_the_fly.slack.MAX_ATTACHMENT_BYTES", 8)
        dest = tmp_path / "big.bin"
        mock_ctx, _ = self._mock_aiohttp(b"x" * 9)

        with (
            patch(
                "claude_on_the_fly.slack.aiohttp.ClientSession", return_value=mock_ctx
            ),
            pytest.raises(ValueError, match="exceeds 8 bytes"),
        ):
            await SlackFrontend._download_file(
                "https://example.com/f", dest, "xoxp-tok"
            )

        assert not dest.exists()

    async def test_replaces_symlink_without_writing_through_it(self, tmp_path):
        target = tmp_path / "outside.txt"
        target.write_text("original")
        dest = tmp_path / "uploaded.txt"
        dest.symlink_to(target)
        mock_ctx, _ = self._mock_aiohttp(b"new bytes")

        with patch(
            "claude_on_the_fly.slack.aiohttp.ClientSession", return_value=mock_ctx
        ):
            await SlackFrontend._download_file(
                "https://example.com/f", dest, "xoxp-tok"
            )

        assert target.read_text() == "original"
        assert dest.read_bytes() == b"new bytes"
        assert not dest.is_symlink()


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
# Forgot-to-tag notice (channel threads only)
# ---------------------------------------------------------------------------


class TestMentionNotice:
    """One notice, and only for somebody who forgot the tag *and* left. While they
    are still typing they are still watching, and a notice read on arrival is one
    they never register."""

    def _event(self, ts: str, text: str = "and one more thing") -> dict:
        return {
            "ts": ts,
            "thread_ts": "t1",
            "text": text,
            "channel": "C1",
            "channel_type": "channel",
            "user": "U_ALLOWED",
        }

    def _live_thread(self, frontend) -> int:
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "n.0"}
        return session_id

    async def test_an_untagged_message_is_answered_after_the_wait(
        self, frontend, monkeypatch
    ):
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 0)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        # Held back, not posted inline.
        frontend._app.client.chat_postMessage.assert_not_called()
        await frontend._mention_notices[session_id]

        note = frontend._app.client.chat_postMessage.call_args[1]["text"]
        assert note.startswith("<@U_ALLOWED> ")  # pings whoever forgot
        assert "<@U_SELF>" in note  # and names the tag to use
        assert session_id in frontend._mention_hinted
        frontend._on_message.assert_not_awaited()

    async def test_the_wait_is_the_configured_one(self, frontend, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(slack_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 120.0)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        await frontend._mention_notices[session_id]

        assert slept == [120.0]

    async def test_another_untagged_message_restarts_the_wait(
        self, frontend, monkeypatch
    ):
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 3600)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        first = frontend._mention_notices[session_id]
        await asyncio.sleep(0)
        await frontend._ingest_event(self._event("t3", "hello?"))
        second = frontend._mention_notices[session_id]

        assert second is not first
        with pytest.raises(asyncio.CancelledError):
            await first
        # The replaced task's cleanup must not take the reschedule down with it.
        assert frontend._mention_notices[session_id] is second
        frontend._app.client.chat_postMessage.assert_not_called()

    async def test_a_tagged_message_cancels_it(self, frontend, monkeypatch):
        """They corrected themselves. Saying it anyway is the bot lecturing."""
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 3600)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        pending = frontend._mention_notices[session_id]
        await asyncio.sleep(0)
        await frontend._ingest_event(self._event("t3", "<@U_SELF> sorry, this"))

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not frontend._mention_notices
        assert session_id not in frontend._mention_hinted  # still tellable later
        frontend._on_message.assert_awaited_once()

    async def test_it_is_said_once_per_thread(self, frontend, monkeypatch):
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 0)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        await frontend._mention_notices[session_id]
        await frontend._ingest_event(self._event("t3", "still forgetting"))

        assert not frontend._mention_notices
        assert frontend._app.client.chat_postMessage.call_count == 1

    async def test_a_thread_the_bot_is_not_in_gets_nothing(self, frontend):
        """Ordinary channel chatter. Answering it would make the bot talk in
        threads nobody invited it into."""
        await frontend._ingest_event(self._event("t2"))

        assert not frontend._mention_notices
        frontend._app.client.chat_postMessage.assert_not_called()

    async def test_an_evicted_thread_drops_its_notice(self, frontend, monkeypatch):
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 3600)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        pending = frontend._mention_notices[session_id]
        frontend._forget_session(session_id)

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not frontend._mention_notices

    async def test_stop_drops_it_too(self, frontend, monkeypatch):
        monkeypatch.setattr(slack_mod, "MENTION_NOTICE_DELAY_SECONDS", 3600)
        session_id = self._live_thread(frontend)

        await frontend._ingest_event(self._event("t2"))
        pending = frontend._mention_notices[session_id]
        await frontend.stop()

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not frontend._mention_notices


# ---------------------------------------------------------------------------
# Reply soft-limit gate
# ---------------------------------------------------------------------------


@pytest.fixture
def no_notice_delay(monkeypatch):
    """Post the gate notice as soon as its task is awaited."""
    monkeypatch.setattr(slack_mod, "reply_limit_notice_seconds", lambda: 0)


class TestReplySoftLimit:
    def _dm_event(self, ts: str, text: str) -> dict:
        return {
            "ts": ts,
            "text": text,
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }

    async def test_gates_inbound_when_over_limit(self, frontend, no_notice_delay):
        session_id = _session_key("D1", "200.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "w.0"}

        await frontend._ingest_event(self._dm_event("200.0", "another question"))

        frontend._on_message.assert_not_awaited()
        # Held back, not posted inline: `_ingest_event` returns before the notice
        # task has had a chance to run.
        frontend._app.client.chat_postMessage.assert_not_called()

        await frontend._gate_notices[session_id]

        warning = frontend._app.client.chat_postMessage.call_args[1]["text"]
        assert CONTINUE_COMMAND in warning
        # Addressed to the sender, so it pings instead of relying on the thread
        # being unread.
        assert warning.startswith("<@U_ALLOWED> ")
        assert session_id not in frontend._gate_notices

    async def test_the_notice_waits_the_configured_delay(self, frontend, monkeypatch):
        """The delay is the whole point of the change: a notice that lands while the
        sender is still in the thread is marked read on arrival and never seen."""
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(slack_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(slack_mod, "reply_limit_notice_seconds", lambda: 4.2)
        session_id = _session_key("D1", "204.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "w.1"}

        await frontend._ingest_event(self._dm_event("204.0", "hello?"))
        await frontend._gate_notices[session_id]

        assert slept == [4.2]

    async def test_a_second_gated_message_restarts_the_wait(
        self, frontend, monkeypatch
    ):
        """Debounce. A burst of messages means the sender is still typing, and a notice
        that lands mid-burst is read on arrival — the failure the delay exists to
        avoid. The wait has to fall after their *last* message, and they are still
        told only once."""
        monkeypatch.setattr(slack_mod, "reply_limit_notice_seconds", lambda: 3600)
        session_id = _session_key("D1", "205.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "w.2"}

        await frontend._ingest_event(self._dm_event("205.0", "one"))
        first = frontend._gate_notices[session_id]
        await asyncio.sleep(0)  # let it reach its sleep, so cancelling runs its finally
        await frontend._ingest_event(
            self._dm_event("205.1", "two") | {"thread_ts": "205.0"}
        )
        second = frontend._gate_notices[session_id]

        assert second is not first
        with pytest.raises(asyncio.CancelledError):
            await first
        # The replaced task's cleanup must not take the reschedule down with it.
        assert frontend._gate_notices[session_id] is second
        frontend._app.client.chat_postMessage.assert_not_called()

        monkeypatch.setattr(slack_mod, "reply_limit_notice_seconds", lambda: 0)
        await frontend._ingest_event(
            self._dm_event("205.2", "three") | {"thread_ts": "205.0"}
        )
        await frontend._gate_notices[session_id]
        assert frontend._app.client.chat_postMessage.call_count == 1
        assert session_id not in frontend._gate_deadlines

    async def test_the_debounce_cannot_defer_past_the_ceiling(
        self, frontend, monkeypatch
    ):
        """Otherwise somebody typing every three seconds never learns the thread is
        gated."""
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(slack_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(slack_mod, "reply_limit_notice_seconds", lambda: 4.2)
        monkeypatch.setattr(slack_mod, "REPLY_LIMIT_NOTICE_MAX_HOLD", 0.0)
        session_id = _session_key("D1", "209.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "w.3"}

        await frontend._ingest_event(self._dm_event("209.0", "hello?"))
        await frontend._gate_notices[session_id]

        # Ceiling already spent, so the notice goes out now rather than in 4.2s.
        assert slept == [0.0]
        frontend._app.client.chat_postMessage.assert_called_once()

    async def test_continue_cancels_a_pending_notice(self, frontend):
        """Otherwise it arrives after the thread resumed and tells the user to send
        what they just sent."""
        session_id = _session_key("D1", "206.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT

        await frontend._ingest_event(self._dm_event("206.0", "hello?"))
        pending = frontend._gate_notices[session_id]
        await frontend._ingest_event(
            self._dm_event("206.1", CONTINUE_COMMAND) | {"thread_ts": "206.0"}
        )

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert session_id not in frontend._gate_notices
        frontend._app.client.chat_postMessage.assert_not_called()

    async def test_stop_cancels_pending_notices(self, frontend):
        session_id = _session_key("D1", "207.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT

        await frontend._ingest_event(self._dm_event("207.0", "hello?"))
        pending = frontend._gate_notices[session_id]
        await frontend.stop()

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not frontend._gate_notices

    async def test_forgetting_a_session_cancels_its_notice(self, frontend):
        session_id = _session_key("D1", "208.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT

        await frontend._ingest_event(self._dm_event("208.0", "hello?"))
        pending = frontend._gate_notices[session_id]
        frontend._forget_session(session_id)

        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not frontend._gate_notices

    async def test_under_limit_processes_normally(self, frontend):
        session_id = _session_key("D1", "201.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT - 1

        await frontend._ingest_event(self._dm_event("201.0", "still going"))

        frontend._on_message.assert_awaited_once()

    async def test_continue_resets_and_processes_remainder(self, frontend):
        session_id = _session_key("D1", "202.0")
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT

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
        frontend._reply_counts[session_id] = DEFAULT_REPLY_SOFT_LIMIT

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
        with patch("claude_on_the_fly.slack.session_cap", lambda: 2):
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
        with patch("claude_on_the_fly.slack.session_cap", lambda: 2):
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
        with patch("claude_on_the_fly.slack.slash_command", lambda: "/cof-hoss"):
            bot_frontend._register_app_interactions()
        bot_frontend._app.command.assert_called_once_with("/cof-hoss")

    def test_skips_the_command_when_unset(self, bot_frontend):
        with patch("claude_on_the_fly.slack.slash_command", lambda: None):
            bot_frontend._register_app_interactions()
        bot_frontend._app.command.assert_not_called()

    @pytest.mark.parametrize("command", ["/cof-hoss", None])
    def test_picker_and_shortcut_register_either_way(self, bot_frontend, command):
        with patch("claude_on_the_fly.slack.slash_command", lambda: command):
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

    async def test_user_token_still_registers_suggestion_handler(self, frontend):
        """Buttons render on every reply and block_actions payloads reach a
        user-token install too, so the suggestion handler must be registered
        regardless of token kind (a tap was previously a 404)."""
        frontend._on_suggestion_action = AsyncMock()
        with patch("claude_on_the_fly.slack.AsyncSocketModeHandler") as handler_cls:
            handler_cls.return_value.start_async = AsyncMock()
            await frontend.start(AsyncMock())
        patterns = [
            call.args[0].pattern for call in frontend._app.action.call_args_list
        ]
        assert r"^cotf-sugg:" in patterns
        action_cbs = [
            call.args[0]
            for call in frontend._app.action.return_value.call_args_list
            if not isinstance(call.args[0], MagicMock)
        ]
        assert len(action_cbs) == 1
        ack = AsyncMock()
        await action_cbs[0](ack, {"user": {"id": "U_ALLOWED"}})
        assert ack.await_count == 1
        frontend._on_suggestion_action.assert_awaited_once()


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


class TestCompactPrefix:
    async def test_queues_a_compaction_instead_of_a_reply(self, frontend):
        """It goes through the queue so it inherits the reaction and the live
        status a reply gets. A large thread takes minutes to compact, and silence
        is indistinguishable from a hung daemon."""
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        event = {
            "ts": "9.0",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.on_compact.assert_awaited_once_with(_session_key("D1", "9.0"))
        frontend._on_message.assert_not_awaited()

    async def test_targets_the_threads_own_session(self, frontend):
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        event = {
            "ts": "9.9",
            "thread_ts": "5.0",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.on_compact.assert_awaited_once_with(_session_key("D1", "5.0"))

    async def test_records_catchup_bookkeeping(self, frontend):
        """The branch returns before the normal path does this, so a reconnect
        would otherwise re-ingest the trigger and compact a second time."""
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        event = {
            "ts": "9.0",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        assert "9.0" in frontend._processed_ts
        assert frontend._active_channels["D1"] == "9.0"

    async def test_works_when_the_thread_is_over_its_reply_budget(self, frontend):
        """Checked ahead of the soft-limit gate: a thread that has hit the cap is
        exactly the one that most needs compacting."""
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        session = _session_key("D1", "9.0")
        frontend._reply_counts[session] = 10_000
        event = {
            "ts": "9.0",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.on_compact.assert_awaited_once()

    async def test_says_so_when_no_orchestrator_is_attached(self, frontend):
        frontend._orchestrator = None
        event = {
            "ts": "9.0",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        frontend._app.client.chat_postMessage.assert_awaited()
        frontend._on_message.assert_not_awaited()

    async def test_trailing_text_is_an_ordinary_message(self, frontend):
        """Exact match only, like $stop. "$compact the notes" is a request about
        notes, not a compaction."""
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        event = {
            "ts": "9.0",
            "text": "$compact the meeting notes",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }
        await frontend._ingest_event(event)
        orch.on_compact.assert_not_awaited()
        frontend._on_message.assert_awaited_once()


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
        bot_frontend._pinned_allowed_user_ids = {"U_OK"}
        ack, respond = AsyncMock(), AsyncMock()
        command = {"text": "simplify", "channel_id": "D1", "user_id": "U_BAD"}
        await bot_frontend._handle_slash_command(
            ack, command, {"trigger_id": "t"}, respond
        )
        respond.assert_awaited_with("Not authorized.")
        bot_frontend._on_message.assert_not_awaited()

    async def test_blocked_sender_denied_even_with_wildcard(self, bot_frontend):
        # bot_frontend allows "*"; a blocked id must still be refused.
        bot_frontend._pinned_blocked_senders = {"U_BAD"}
        ack, respond = AsyncMock(), AsyncMock()
        command = {"text": "simplify", "channel_id": "D1", "user_id": "U_BAD"}
        await bot_frontend._handle_slash_command(
            ack, command, {"trigger_id": "t"}, respond
        )
        respond.assert_awaited_with("Not authorized.")
        bot_frontend._on_message.assert_not_awaited()

    async def test_shortcut_denied_for_unlisted_user(self, bot_frontend):
        bot_frontend._pinned_allowed_user_ids = {"U_OK"}
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
        bot_frontend._pinned_allowed_user_ids = {"U_OK"}
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

    async def test_ambiguous_error_fails_closed(self, bot_frontend):
        bot_frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("x", {"ok": False, "error": "ratelimited"})
        )
        assert await bot_frontend._is_bot_conversation("DY") is False
        assert "DY" not in bot_frontend._own_dm

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
        enqueued_at=datetime.now(UTC) - timedelta(seconds=age_s),
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


class TestCompactResolvesTheWorkspace:
    """The live bug: `$compact` reported "no session yet" on threads days deep.

    `workspace_name()` reads `_workspace_names`, filled in only by
    `_resolve_session_metadata` — and the intercept returned before the normal
    path reached it, so the compaction looked for a session under
    `slack/<session_key>`, a directory nothing was ever created in.
    """

    def _event(self) -> dict:
        return {
            "ts": "1785236668.157229",
            "thread_ts": "1784899718.993159",
            "text": "$compact",
            "channel": "D1",
            "channel_type": "im",
            "user": "U_ALLOWED",
        }

    async def test_workspace_name_is_resolved_before_queueing(self, frontend):
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        session = _session_key("D1", "1784899718.993159")

        await frontend._ingest_event(self._event())

        name = frontend.workspace_name(session)
        assert name != f"slack/{session}", "fell back to the session key"
        # sender + the whole thread ts, fraction included
        assert name == "slack/dm-testuser-1784899718-993159"

    async def test_it_matches_what_an_ordinary_message_would_produce(self, frontend):
        """Same thread, same workspace, whichever path got there first — else the
        compaction targets a different session than the conversation."""
        orch = MagicMock()
        orch.on_compact = AsyncMock()
        frontend._orchestrator = orch
        session = _session_key("D1", "1784899718.993159")

        ordinary = dict(self._event(), ts="1785236600.000000", text="hello")
        await frontend._ingest_event(ordinary)
        from_message = frontend.workspace_name(session)

        frontend._workspace_names.clear()
        frontend._sender_names.clear()
        await frontend._ingest_event(self._event())

        assert frontend.workspace_name(session) == from_message


class TestRetiredApprovalCard:
    """The spent permission card shares the thread with the answer, so it has to
    read as a status line rather than a second reply."""

    async def test_collapses_to_a_single_context_line(self, frontend):
        from claude_on_the_fly.approvals import ApprovalRequest

        frontend._app.client.chat_update = AsyncMock()
        await frontend._retire_approval(
            "C1",
            "1785382860.1",
            ApprovalRequest(kind="host", subject="pypi.org:443", detail="d"),
            True,
        )
        blocks = frontend._app.client.chat_update.await_args.kwargs["blocks"]
        assert len(blocks) == 1
        # context renders small and grey; section competes with the real reply.
        assert blocks[0]["type"] == "context"
        text = blocks[0]["elements"][0]["text"]
        assert text == "Permission *approved*: `pypi.org:443`"
        # No buttons survive, so the prompt cannot be answered twice.
        assert "actions" not in [b["type"] for b in blocks]

    async def test_denial_reads_the_same_way(self, frontend):
        from claude_on_the_fly.approvals import ApprovalRequest

        frontend._app.client.chat_update = AsyncMock()
        await frontend._retire_approval(
            "C1",
            "1785382860.1",
            ApprovalRequest(kind="host", subject="evil.example:443", detail="d"),
            False,
        )
        kwargs = frontend._app.client.chat_update.await_args.kwargs
        assert kwargs["blocks"][0]["type"] == "context"
        assert (
            kwargs["blocks"][0]["elements"][0]["text"]
            == "Permission *denied*: `evil.example:443`"
        )
        # The notification fallback stays readable on its own.
        assert kwargs["text"] == "Permission denied: evil.example:443"

    async def test_api_failure_does_not_raise(self, frontend):
        """A retire failure must not propagate: the grant decision is already
        made, and losing the cosmetic update is not worth failing the turn."""
        from slack_sdk.errors import SlackApiError

        from claude_on_the_fly.approvals import ApprovalRequest

        frontend._app.client.chat_update = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "message_not_found"})
        )
        await frontend._retire_approval(
            "C1", "1.1", ApprovalRequest(kind="host", subject="a:443", detail="d"), True
        )


# ---------------------------------------------------------------------------
# The approval card end to end
# ---------------------------------------------------------------------------


def _request(subject="pypi.org:443", detail="agent asked for pypi.org:443", **kwargs):
    from claude_on_the_fly.approvals import ApprovalRequest

    return ApprovalRequest(kind="host", subject=subject, detail=detail, **kwargs)


class TestAskApprovalRefusals:
    async def test_no_session_means_nobody_to_ask(self, frontend, caplog):
        """Sessionless work (cron, the job queue) has nobody watching its thread, so
        it denies instead of falling back to a channel a fallback would quietly
        grant network access in."""
        frontend._is_bot_token = True
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert await frontend.ask_approval(_request(), chat_id=None) is False
        assert "approval denied" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_an_unknown_chat_is_refused(self, frontend):
        frontend._is_bot_token = True
        assert await frontend.ask_approval(_request(), chat_id=999) is False

    async def test_a_user_token_install_cannot_receive_the_click(self, frontend):
        """Interaction payloads only reach a bot-token install, so asking would hang
        until the caller's timeout and then deny anyway."""
        frontend._is_bot_token = False
        frontend._sessions[1] = ("C1", "1785382860.1")
        assert await frontend.ask_approval(_request(), chat_id=1) is False
        frontend._app.client.chat_postMessage.assert_not_awaited()


class TestAskApprovalHappyPath:
    def _wire(self, frontend):
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "1785382999.1"}
        )
        frontend._app.client.chat_update = AsyncMock()

    async def _answer(self, frontend, granted: bool):
        """Click the posted card the way Slack would."""
        for _ in range(200):
            await asyncio.sleep(0.005)
            if frontend._pending_approvals:
                break
        nonce = next(iter(frontend._pending_approvals))
        await frontend._on_approval_action(
            {
                "user": {"id": "U_ALLOWED"},
                "actions": [
                    {
                        "action_id": "cotf-grant" if granted else "cotf-deny",
                        "value": nonce,
                    }
                ],
            }
        )

    async def test_a_grant_click_resolves_the_wait(self, frontend):
        self._wire(frontend)
        task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
        await self._answer(frontend, True)
        assert await asyncio.wait_for(task, timeout=2) is True
        assert frontend._pending_approvals == {}

    async def test_a_deny_click_resolves_the_wait(self, frontend):
        self._wire(frontend)
        task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
        await self._answer(frontend, False)
        assert await asyncio.wait_for(task, timeout=2) is False

    async def test_the_card_is_retired_after_the_click(self, frontend):
        self._wire(frontend)
        task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
        await self._answer(frontend, True)
        await asyncio.wait_for(task, timeout=2)
        frontend._app.client.chat_update.assert_awaited_once()

    async def test_a_timeout_retires_the_card_too(self, frontend):
        """The cancellation used to skip the retire, leaving a spent card with
        live-looking Approve/Deny buttons in the thread forever: tapping either then
        logged "unknown or settled nonce" and did nothing."""
        self._wire(frontend)
        task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if frontend._pending_approvals:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        frontend._app.client.chat_update.assert_awaited_once()
        # And the nonce is gone, so a late click cannot be answered.
        assert frontend._pending_approvals == {}

    async def test_a_post_that_never_lands_leaves_no_pending_nonce(self, frontend):
        """Nothing will ever answer a card that was not posted, so the entry would
        sit in _pending_approvals for the life of the daemon."""
        from slack_sdk.errors import SlackApiError

        self._wire(frontend)
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "channel_not_found"})
        )
        with pytest.raises(SlackApiError):
            await frontend.ask_approval(_request(), chat_id=1)
        assert frontend._pending_approvals == {}

    async def test_an_unexpected_retire_failure_is_logged_not_swallowed(
        self, frontend, caplog
    ):
        """`_retire_approval` already logs the Slack errors it expects, so anything
        arriving at the outer handler is a surprise and this line is the only place
        it would be visible."""
        self._wire(frontend)
        frontend._retire_approval = AsyncMock(side_effect=RuntimeError("boom"))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
            await self._answer(frontend, True)
            assert await asyncio.wait_for(task, timeout=2) is True
        assert "retiring the approval prompt failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestApprovalCardCannotBeRestyledByTheAgent:
    """Parts of a subject and detail are agent-reachable: a broker route-scope
    request carries the path tail the agent asked for. Rendered as mrkdwn, the agent
    can close the code span and forge a verdict line above the real subject."""

    def _blocks(self, frontend, request):
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "1785382999.1"}
        )
        asyncio.get_event_loop()
        return request

    async def test_a_subject_cannot_close_its_own_code_span(self, frontend):
        injected = "/anthropic/v1`\n*Permission APPROVED*\n`x"
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "1785382999.1"}
        )
        frontend._app.client.chat_update = AsyncMock()
        task = asyncio.create_task(
            frontend.ask_approval(_request(subject=injected), chat_id=1)
        )
        for _ in range(200):
            await asyncio.sleep(0.005)
            if frontend._app.client.chat_postMessage.await_args:
                break
        blocks = frontend._app.client.chat_postMessage.await_args.kwargs["blocks"]
        headline = blocks[0]["text"]["text"]
        # Exactly the two that open and close the span the template writes. The
        # injected text stays inside it and renders as literal characters, so the
        # forged verdict is visibly part of the subject rather than a line of its
        # own above it.
        assert headline.count("`") == 2, headline
        # And no newline, or the tail would escape the span and render as mrkdwn.
        assert headline.count("\n") == 1, headline
        assert headline.startswith("*Permission request*\n`")
        assert headline.endswith("`")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def test_the_card_says_the_command_once(self):
        """The regression this guards: the footer carried the subject, then a
        `Covers:` line in front of it, so a card read "(Bash)" / "chmod 700 ." /
        "Covers: bash:chmod 700 .  bash:chmod" -- the same command three times on a
        phone screen. The key and the full command are in the log instead."""
        from claude_on_the_fly.slack import _approval_footer, _approval_headline

        request = _request(
            subject="bash:chmod",
            detail="chmod 700 .",
            origin="Bash",
            scope="bash:chmod 700 .",
        )
        card = f"{_approval_headline(request)}\n{request.detail}\n{_approval_footer(request)}"
        assert card.count("chmod") == 1
        assert "Covers:" not in card
        assert "bash:chmod" not in _approval_footer(request)

    def test_an_egress_card_still_names_its_host_once(self):
        """A no-origin request puts the subject in the headline, so dropping the
        footer's copy must not leave the host off the card entirely."""
        from claude_on_the_fly.slack import _approval_footer, _approval_headline

        request = _request()
        assert "pypi.org:443" in _approval_headline(request)
        assert "pypi.org" not in _approval_footer(request)

    def test_a_retired_card_records_the_command_not_the_program(self):
        """The complaint this fixes: an approved card collapsed to "Permission
        approved: bash:chmod", which does not say which file was chmodded. The
        subject is program-scoped because it is the grant key; the scope has args."""
        from claude_on_the_fly.slack import _decided_text

        request = _request(
            subject="bash:chmod", scope="bash:chmod 700 /Users/user/ws", origin="Bash"
        )
        assert _decided_text(request) == "bash:chmod 700 /Users/user/ws"

    def test_a_retired_card_falls_back_to_the_subject(self):
        """An egress subject is already the whole decision, so it needs no scope and
        must still produce a record."""
        from claude_on_the_fly.slack import _decided_text

        assert _decided_text(_request()) == "pypi.org:443"

    def test_a_readable_subject_keeps_the_headline(self):
        """An egress or command request names a host or a binary, which is exactly
        what the operator is deciding about, so those cards must not change."""
        from claude_on_the_fly.slack import _approval_footer, _approval_headline

        request = _request()
        assert _approval_headline(request) == "*Permission request*\n`pypi.org:443`"
        assert "pypi.org" not in _approval_footer(request)

    def test_an_agent_reachable_origin_cannot_break_out_of_the_headline(self):
        """origin is cotf-authored today, but it lands in mrkdwn beside a code span,
        so it goes through the same literalising the subject does. A backtick or a
        newline here would let the agent style the operator's own prompt."""
        from claude_on_the_fly.slack import _approval_headline

        headline = _approval_headline(
            _request(origin="Bash`\n*APPROVED*, asked by claude")
        )
        assert "\n" not in headline
        assert headline.count("`") == 0

    async def test_the_detail_is_posted_where_slack_parses_no_markup(self, frontend):
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "1785382999.1"}
        )
        frontend._app.client.chat_update = AsyncMock()
        task = asyncio.create_task(
            frontend.ask_approval(
                _request(detail="GET /anthropic/v1/*bold*\n>quote"), chat_id=1
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.005)
            if frontend._app.client.chat_postMessage.await_args:
                break
        blocks = frontend._app.client.chat_postMessage.await_args.kwargs["blocks"]
        detail_block = blocks[1]
        assert detail_block["text"]["type"] == "plain_text"
        # Verbatim, because it states what the sandbox actually observed.
        assert detail_block["text"]["text"] == "GET /anthropic/v1/*bold*\n>quote"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_a_retired_card_escapes_the_subject_too(self, frontend):
        frontend._app.client.chat_update = AsyncMock()
        await frontend._retire_approval(
            "C1", "1.1", _request(subject="host`*fake*`:443"), True
        )
        text = frontend._app.client.chat_update.await_args.kwargs["blocks"][0][
            "elements"
        ][0]["text"]
        assert "`*fake*`" not in text
        assert text == "Permission *approved*: `host'*fake*':443`"


class TestFitBlock:
    def test_short_text_is_untouched(self):
        assert slack_mod._fit_block("GET /v1/messages") == "GET /v1/messages"

    def test_an_over_long_detail_says_it_was_cut(self):
        """The detail is the one thing the operator has to read before granting, and
        the tail is where a suspicious path or an unexpected method would sit. A
        silent truncation is the worst possible failure here."""
        detail = "x" * 4000
        fitted = slack_mod._fit_block(detail)
        assert len(fitted) < 3000, "must fit Slack's section limit"
        assert "more characters" in fitted
        assert "1100" in fitted

    def test_exactly_at_the_limit_is_not_marked(self):
        assert "more characters" not in slack_mod._fit_block("x" * 2900)


class TestLiteral:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pypi.org:443", "pypi.org:443"),
            ("host`x`:443", "host'x':443"),
            ("multi\nline", "multi line"),
            ("  padded  ", "padded"),
        ],
    )
    def test_a_code_span_stays_a_code_span(self, raw, expected):
        assert slack_mod._literal(raw) == expected


class TestApprovalClickAuthorisation:
    async def test_a_bystander_cannot_answer_the_prompt(self, frontend, caplog):
        """Landing in a shared channel is only safe because the clicker is
        re-checked: bystanders may read the prompt but must not grant the agent a
        host."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        frontend._pending_approvals["abc"] = future
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            await frontend._on_approval_action(
                {
                    "user": {"id": "U_STRANGER"},
                    "actions": [{"action_id": "cotf-grant", "value": "abc"}],
                }
            )
        assert not future.done(), "a bystander decided the grant"
        assert "ignoring approval click from U_STRANGER" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_payload_with_no_actions_is_ignored(self, frontend):
        await frontend._on_approval_action({"user": {"id": "U_ALLOWED"}})

    async def test_a_click_on_an_unknown_nonce_is_ignored(self, frontend, caplog):
        with caplog.at_level("INFO", logger="claude_on_the_fly.slack"):
            await frontend._on_approval_action(
                {
                    "user": {"id": "U_ALLOWED"},
                    "actions": [{"action_id": "cotf-grant", "value": "gone"}],
                }
            )
        assert "unknown or settled nonce" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_second_click_on_a_settled_prompt_is_ignored(self, frontend):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result(True)
        frontend._pending_approvals["abc"] = future
        await frontend._on_approval_action(
            {
                "user": {"id": "U_ALLOWED"},
                "actions": [{"action_id": "cotf-deny", "value": "abc"}],
            }
        )
        assert future.result() is True, "the answer changed after settling"


class TestSuggestionActions:
    """Preset one-tap follow-up buttons rendered under every reply."""

    @staticmethod
    def _tap(
        action_id: str = "cotf-sugg:0",
        label: str = "what can you do?",
        user_id: str = "U_ALLOWED",
        channel: str = "C1",
        thread_ts: str | None = "t1",
    ) -> dict:
        return {
            "user": {"id": user_id, "name": "hoss"},
            "channel": {"id": channel},
            "message": {
                "ts": "99.1",
                "text": "hello",
                **({"thread_ts": thread_ts} if thread_ts is not None else {}),
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": lbl},
                                "action_id": f"cotf-sugg:{i}",
                            }
                            for i, lbl in enumerate(
                                ["what can you do?", "summarize the conversation"]
                            )
                        ],
                    },
                ],
            },
            "actions": [
                {
                    "action_id": action_id,
                    "text": {"type": "plain_text", "text": label},
                }
            ],
        }

    async def test_send_renders_suggestions(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(
            session_id, Response(body="hello", suggestions=["alpha?", "beta?"])
        )

        blocks = frontend._app.client.chat_postMessage.call_args[1]["blocks"]
        actions = next(b for b in blocks if b["type"] == "actions")
        labels = [e["text"]["text"] for e in actions["elements"]]
        assert labels == ["alpha?", "beta?"]

    async def test_send_no_buttons_when_suggestions_empty(self, frontend):
        session_id = _session_key("C1", "t1")
        frontend._sessions[session_id] = ("C1", "t1")
        frontend._app.client.chat_postMessage.return_value = {"ok": True, "ts": "99.0"}

        await frontend.send(session_id, Response(body="hello", suggestions=[]))

        blocks = frontend._app.client.chat_postMessage.call_args[1]["blocks"]
        assert all(b["type"] != "actions" for b in blocks)

    async def test_tap_sends_label_as_next_message(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap())

        frontend._on_message.assert_awaited_once()
        text = frontend._on_message.await_args.args[1]
        assert "what can you do?" in text
        assert text.startswith("[from-id: U_ALLOWED]")

    async def test_tap_uses_thread_ts_for_session_match(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(
            self._tap(action_id="cotf-sugg:1", label="summarize the conversation")
        )

        assert "summarize the conversation" in frontend._on_message.await_args.args[1]

    async def test_unauthorized_tap_is_ignored(self, frontend, caplog):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            await frontend._on_suggestion_action(self._tap(user_id="U_STRANGER"))
        frontend._on_message.assert_not_awaited()
        assert "ignoring suggestion click from U_STRANGER" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_tap_without_a_label_is_dropped(self, frontend, caplog):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        body = self._tap()
        del body["actions"][0]["text"]
        with caplog.at_level("INFO", logger="claude_on_the_fly.slack"):
            await frontend._on_suggestion_action(body)
        frontend._on_message.assert_not_awaited()
        assert "without a label" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_generated_label_is_sent_verbatim(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap(label="alpha?"))

        text = frontend._on_message.await_args.args[1]
        assert "alpha?" in text
        update = frontend._app.client.chat_update.await_args.kwargs
        status = next(b for b in update["blocks"] if b["type"] == "context")
        assert status["elements"][0]["text"] == "✓ alpha?"

    async def test_tap_survives_a_restart_that_emptied_the_session_map(
        self, frontend
    ) -> None:
        # A daemon restart drops _sessions, so a button drawn by the previous
        # process taps into an empty map. The session id is derived from the
        # tap's own (channel, thread_ts), so the turn still dispatches.
        frontend._sessions.clear()
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap())

        frontend._on_message.assert_awaited_once()
        chat_id = frontend._on_message.await_args.args[0]
        assert chat_id == _session_key("C1", "t1")
        # Re-registered, so send() can route the reply back into the thread.
        assert frontend._sessions[chat_id] == ("C1", "t1")

    async def test_tap_in_an_unknown_channel_routes_to_its_own_thread(self, frontend):
        # No session for C_OTHER, and no scan to fail: the tap is authorized and
        # names its own thread, so it gets one.
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap(channel="C_OTHER"))

        assert frontend._on_message.await_args.args[0] == _session_key("C_OTHER", "t1")

    async def test_tap_with_no_handler_wired_is_logged(self, frontend, caplog):
        frontend._on_message = None
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            await frontend._on_suggestion_action(self._tap())
        assert "no message handler wired" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_tap_with_no_actions_is_ignored(self, frontend):
        await frontend._on_suggestion_action({"user": {"id": "U_ALLOWED"}})

    async def test_tap_marks_and_retires_the_button(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap())

        update = frontend._app.client.chat_update.await_args.kwargs
        assert update["channel"] == "C1"
        assert update["ts"] == "99.1"
        assert all(b["type"] != "actions" for b in update["blocks"])
        status = next(b for b in update["blocks"] if b["type"] == "context")
        assert status["elements"][0]["text"] == "✓ what can you do?"
        frontend._on_message.assert_awaited_once()

    async def test_tap_without_message_blocks_skips_the_mark(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()
        body = self._tap()
        del body["message"]["blocks"]

        await frontend._on_suggestion_action(body)

        frontend._app.client.chat_update.assert_not_awaited()
        frontend._on_message.assert_awaited_once()

    async def test_mark_failure_is_logged_and_tap_still_sends(
        self, frontend, caplog
    ) -> None:
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "message_not_found"})
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            await frontend._on_suggestion_action(self._tap())
        frontend._on_message.assert_awaited_once()
        assert "could not retire suggestion menu" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_second_tap_on_the_same_message_is_dropped(self, frontend):
        # The retire is a cosmetic chat_update that can fail; the spent guard
        # is what stops a re-tap from dispatching a second turn.
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "message_not_found"})
        )

        await frontend._on_suggestion_action(self._tap())
        await frontend._on_suggestion_action(self._tap())

        frontend._on_message.assert_awaited_once()

    async def test_tap_fills_the_pending_message_queue(self, frontend):
        # The tap turn mirrors typed-message bookkeeping so notify_start pairs
        # reactions with this turn's own message, not the next typed one's.
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()

        await frontend._on_suggestion_action(self._tap())

        chat_id = frontend._session_id_for(self._tap())
        assert frontend._pending_msg[chat_id] == deque([("C1", "99.1")])
        assert frontend._pending_reply_suppressed[chat_id] == deque([False])

    async def test_spent_menus_cap_evicts_the_oldest(self, frontend):
        frontend._sessions[_session_key("C1", "t1")] = ("C1", "t1")
        frontend._app.client.chat_update = AsyncMock()
        for i in range(slack_mod.SUGGESTION_SPENT_CAP):
            frontend._spent_menus[(f"C{i}", "1.0")] = None

        await frontend._on_suggestion_action(self._tap())

        assert len(frontend._spent_menus) == slack_mod.SUGGESTION_SPENT_CAP
        assert ("C0", "1.0") not in frontend._spent_menus
        assert ("C1", "99.1") in frontend._spent_menus
        frontend._on_message.assert_awaited_once()


class TestApprovalCardDoesNotUnfurl:
    async def test_the_destination_being_gated_is_not_fetched(self, frontend):
        """An unfurl would have Slack fetch the very host being gated, before any
        decision is made."""
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "1785382999.1"}
        )
        frontend._app.client.chat_update = AsyncMock()
        task = asyncio.create_task(frontend.ask_approval(_request(), chat_id=1))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if frontend._app.client.chat_postMessage.await_args:
                break
        kwargs = frontend._app.client.chat_postMessage.await_args.kwargs
        assert kwargs["unfurl_links"] is False
        assert kwargs["unfurl_media"] is False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Degrading rather than failing
# ---------------------------------------------------------------------------


class TestJobQueueConstruction:
    def test_an_unrecognised_queue_kind_disables_jobs_not_slack(
        self, monkeypatch, caplog
    ):
        """`make_queue()` raises on an unrecognised JOBS_QUEUE_KIND. Now that the
        trigger is on by default, that must degrade to "no background jobs" rather
        than "no Slack"."""
        monkeypatch.setattr(
            slack_mod, "make_queue", lambda: (_ for _ in ()).throw(ValueError("nope"))
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert slack_mod._build_job_queue() is None
        assert "background jobs disabled" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_no_queue_means_nothing_to_list(self, frontend):
        frontend._job_queue = None
        assert frontend._read_unfinished_jobs() == []

    def test_a_queue_read_error_degrades_to_nothing_to_show(self, frontend, caplog):
        """A listing is a courtesy: the queue lives on a filesystem a chat turn has
        no business failing over."""
        frontend._job_queue = MagicMock()
        frontend._job_queue.list_unfinished.side_effect = OSError("nfs timeout")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert frontend._read_unfinished_jobs() == []
        assert "could not read the job queue" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestRichTextFlattening:
    def test_nested_element_groups_are_flattened(self):
        """A rich_text_section can nest further groups, and dropping them loses the
        message body the agent is meant to read."""
        assert (
            slack_mod._flatten_rich_elements(
                [
                    {"type": "text", "text": "see "},
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "this "},
                            {"type": "user", "user_id": "U1"},
                        ],
                    },
                ]
            )
            == "see this <@U1>"
        )

    def test_unknown_leaf_types_contribute_nothing(self):
        assert (
            slack_mod._flatten_rich_elements([{"type": "emoji", "name": "wave"}]) == ""
        )


class TestForwardExtraction:
    def test_an_attachment_with_only_blocks_still_yields_its_text(self):
        """Share-message payloads sometimes carry the body in blocks rather than in
        the flat `text` field."""
        forwards = slack_mod._extract_forwards(
            {
                "attachments": [
                    {
                        "channel_id": "C9",
                        "ts": "1785382860.1",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "forwarded body"},
                            }
                        ],
                    }
                ]
            }
        )
        assert [f["text"] for f in forwards] == ["forwarded body"]

    def test_a_rich_text_quote_is_a_forward(self):
        forwards = slack_mod._extract_forwards(
            {
                "blocks": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_quote",
                                "elements": [{"type": "text", "text": "quoted body"}],
                            }
                        ],
                    }
                ]
            }
        )
        assert [f["text"] for f in forwards] == ["quoted body"]

    def test_an_empty_quote_is_not_a_forward(self):
        assert (
            slack_mod._extract_forwards(
                {
                    "blocks": [
                        {
                            "type": "rich_text",
                            "elements": [{"type": "rich_text_quote", "elements": []}],
                        }
                    ]
                }
            )
            == []
        )

    def test_a_non_quote_element_is_skipped(self):
        assert (
            slack_mod._extract_forwards(
                {
                    "blocks": [
                        {
                            "type": "rich_text",
                            "elements": [
                                {
                                    "type": "rich_text_section",
                                    "elements": [{"type": "text", "text": "ordinary"}],
                                }
                            ],
                        }
                    ]
                }
            )
            == []
        )


class TestDescribe:
    def test_the_app_token_is_redacted(self, frontend):
        """This goes straight into the startup log."""
        described = frontend.describe()
        assert "xapp-tok" not in described["app_token"]
        assert described["token_kind"] == "user"
        assert described["user_id"] == "U_SELF"
        # The self user id is added at construction so the operator can always
        # reach their own bot.
        assert described["allowed_users"] == "U_ALLOWED,U_SELF"

    def test_allow_all_is_shown_as_a_star(self, frontend):
        frontend._pinned_allowed_user_ids = {"*"}
        assert frontend.describe()["allowed_users"] == "*"

    def test_an_empty_allowlist_still_carries_the_tokens_own_id(self, frontend):
        """The gotcha this makes visible: with a bot token that id is the BOT's, so an
        operator whose list is empty has allowed nobody but the app, and their own DMs
        do not get through. `describe` says so at startup rather than leaving it to be
        discovered."""
        frontend._pinned_allowed_user_ids = set()
        assert frontend.describe()["allowed_users"] == "U_SELF"

    def test_a_blank_user_id_reads_as_none(self, frontend):
        """`user_id` comes from Slack auth.test, so this is unreachable in a real
        deployment -- but it is the one path that makes the field empty, and the
        `<none>` placeholder is there so a missing value never renders as a blank."""
        frontend._pinned_allowed_user_ids = set()
        frontend._user_id = ""
        assert frontend.describe()["allowed_users"] == "<none>"


class TestNumericLimits:
    """Read per use, so an edit lands without a restart."""

    def test_unset_is_the_default(self, operator_settings, monkeypatch):
        monkeypatch.delenv("SLACK_SESSION_CAP", raising=False)
        monkeypatch.delenv("SLACK_REPLY_SOFT_LIMIT", raising=False)
        assert slack_mod.session_cap() == slack_mod.DEFAULT_SESSION_CAP
        assert slack_mod.reply_soft_limit() == slack_mod.DEFAULT_REPLY_SOFT_LIMIT

    def test_a_configured_value_is_used(self, operator_settings):
        operator_settings.write_text("slack:\n  session_cap: 7\n")
        assert slack_mod.session_cap() == 7

    def test_junk_falls_back_and_says_so(self, operator_settings, caplog):
        """A memory bound is not worth refusing to serve a message over, but a typo
        that silently reverted to 1000 would look like a working setting."""
        operator_settings.write_text("slack:\n  session_cap: lots\n")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert slack_mod.session_cap() == slack_mod.DEFAULT_SESSION_CAP
        assert "is not a number" in caplog.text

    def test_zero_or_negative_falls_back_and_says_so(self, operator_settings, caplog):
        """A cap of 0 would evict every session the moment it was created, which is a
        daemon that appears to have no memory at all."""
        operator_settings.write_text("slack:\n  session_cap: 0\n")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert slack_mod.session_cap() == slack_mod.DEFAULT_SESSION_CAP
        assert "must be positive" in caplog.text

    def test_the_notice_delay_defaults_and_takes_a_fraction(
        self, operator_settings, monkeypatch
    ):
        monkeypatch.delenv("SLACK_REPLY_LIMIT_NOTICE_SECONDS", raising=False)
        operator_settings.write_text("slack: {}\n")
        assert (
            slack_mod.reply_limit_notice_seconds()
            == slack_mod.DEFAULT_REPLY_LIMIT_NOTICE_SECONDS
        )
        operator_settings.write_text("slack:\n  reply_limit_notice_seconds: 1.5\n")
        assert slack_mod.reply_limit_notice_seconds() == 1.5

    def test_a_zero_notice_delay_is_honoured(self, operator_settings):
        """Unlike the counts, 0 is a real choice here: post it immediately."""
        operator_settings.write_text("slack:\n  reply_limit_notice_seconds: 0\n")
        assert slack_mod.reply_limit_notice_seconds() == 0

    def test_a_junk_notice_delay_falls_back_and_says_so(
        self, operator_settings, caplog
    ):
        operator_settings.write_text("slack:\n  reply_limit_notice_seconds: soon\n")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert (
                slack_mod.reply_limit_notice_seconds()
                == slack_mod.DEFAULT_REPLY_LIMIT_NOTICE_SECONDS
            )
        assert "is not a number" in caplog.text

    def test_a_negative_notice_delay_falls_back_and_says_so(
        self, operator_settings, caplog
    ):
        operator_settings.write_text("slack:\n  reply_limit_notice_seconds: -3\n")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert (
                slack_mod.reply_limit_notice_seconds()
                == slack_mod.DEFAULT_REPLY_LIMIT_NOTICE_SECONDS
            )
        assert "cannot be negative" in caplog.text

    def test_a_nan_notice_delay_falls_back_and_says_so(self, operator_settings, caplog):
        """`float()` accepts "nan", and it defeats the ceiling: min(nan, 30) is nan
        and asyncio.sleep(nan) never wakes, so every thread's notice would be lost
        until a restart."""
        operator_settings.write_text("slack:\n  reply_limit_notice_seconds: nan\n")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert (
                slack_mod.reply_limit_notice_seconds()
                == slack_mod.DEFAULT_REPLY_LIMIT_NOTICE_SECONDS
            )
        assert "is not a finite number" in caplog.text


class TestJobCommand:
    def test_absent_means_the_default(self, operator_settings, monkeypatch):
        monkeypatch.delenv("SLACK_JOB_COMMAND", raising=False)
        operator_settings.write_text("slack: {}\n")
        assert slack_mod._resolve_job_command() == slack_mod.DEFAULT_JOB_COMMAND

    def test_empty_means_off(self, operator_settings):
        """The opt-out. Absent and present-but-blank have to stay distinguishable, or
        an install cannot say "no background jobs" at all."""
        operator_settings.write_text("slack:\n  job_command: ''\n")
        assert slack_mod._resolve_job_command() is None

    def test_a_value_renames_the_trigger(self, operator_settings):
        operator_settings.write_text("slack:\n  job_command: '!bg'\n")
        assert slack_mod._resolve_job_command() == "!bg"


class TestSlashCommand:
    def test_unset_registers_none(self, operator_settings, monkeypatch):
        """The skill picker is reached from a message's "..." shortcut instead."""
        monkeypatch.delenv("SLACK_SLASH_COMMAND", raising=False)
        operator_settings.write_text("slack: {}\n")
        assert slack_mod.slash_command() is None

    def test_a_configured_command_is_returned(self, operator_settings):
        operator_settings.write_text("slack:\n  slash_command: /cotf\n")
        assert slack_mod.slash_command() == "/cotf"

    def test_it_is_in_the_restart_required_set(self):
        """Registering it is a handshake with Slack, so a live reload would point the
        daemon at a command nothing is listening for."""
        assert "slack.slash_command" in settings.RESTART_REQUIRED


class TestSenderLists:
    """Access control, read per message so an edit needs no restart.

    This coverage used to live on `preflight.run_slack`, which resolved the lists at
    startup and handed them to the frontend pinned. It moved here with the behaviour.
    """

    @pytest.fixture
    def live(self, frontend, monkeypatch, operator_settings):
        """A frontend with nothing pinned, so every list reads from the config."""
        for var in (
            "SLACK_ALLOWED_SENDER_IDS",
            "SLACK_BLOCKED_SENDER_IDS",
            "SLACK_SILENT_SENDER_IDS",
            "SLACK_ALLOWED_USER_IDS",
            "SLACK_BLOCKED_USER_IDS",
            "SLACK_ALLOWED_BOT_IDS",
        ):
            monkeypatch.delenv(var, raising=False)
        frontend._pinned_allowed_user_ids = None
        frontend._pinned_blocked_senders = None
        frontend._pinned_allowed_bot_ids = None
        frontend._pinned_silent_sender_ids = None
        return frontend

    def test_one_list_splits_by_slack_id_prefix(self, live, operator_settings):
        operator_settings.write_text(
            "slack:\n  allowed_senders: [U1, B2, W3, B4]\n  blocked_senders: [U9, B8]\n"
        )
        assert live._allowed_user_ids == {"U1", "W3", "U_SELF"}
        assert live._allowed_bot_ids == {"B2", "B4"}
        assert live._blocked_senders == {"U9", "B8"}

    def test_a_wildcard_allows_any_human(self, live, operator_settings):
        operator_settings.write_text("slack:\n  allowed_senders: ['*']\n")
        assert live._allow_all_senders is True

    def test_absent_lists_are_empty_but_for_the_tokens_own_id(
        self, live, operator_settings
    ):
        operator_settings.write_text("slack: {}\n")
        assert live._allowed_user_ids == {"U_SELF"}
        assert live._allowed_bot_ids == set()
        assert live._blocked_senders == set()
        assert live._silent_sender_ids == set()
        assert live._allow_all_senders is False

    def test_an_empty_list_is_empty(self, live, operator_settings):
        operator_settings.write_text("slack:\n  allowed_senders: []\n")
        assert live._allowed_user_ids == {"U_SELF"}

    def test_deprecated_split_lists_still_merge(self, live, monkeypatch):
        """The old user/bot allowlists combine into the one list, so an install that
        predates the merge keeps working."""
        monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", "U1, U2")
        monkeypatch.setenv("SLACK_ALLOWED_BOT_IDS", "B1")
        assert live._allowed_user_ids == {"U1", "U2", "U_SELF"}
        assert live._allowed_bot_ids == {"B1"}

    def test_the_new_name_wins_over_the_deprecated_one(
        self, live, monkeypatch, operator_settings
    ):
        operator_settings.write_text("slack:\n  allowed_senders: [U1]\n")
        monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", "U_OLD")
        assert live._allowed_user_ids == {"U1", "U_SELF"}

    def test_an_env_var_still_wins_over_the_file(
        self, live, monkeypatch, operator_settings
    ):
        """.env backward compatibility: an operator who never edits the yaml keeps
        exactly the access control they configured."""
        operator_settings.write_text("slack:\n  allowed_senders: [U_FILE]\n")
        monkeypatch.setenv("SLACK_ALLOWED_SENDER_IDS", "U_ENV")
        assert live._allowed_user_ids == {"U_ENV", "U_SELF"}

    def test_adding_a_sender_needs_no_restart(self, live, operator_settings):
        """The point of the whole change."""
        operator_settings.write_text("slack:\n  allowed_senders: [U1]\n")
        assert live._allowed_user_ids == {"U1", "U_SELF"}
        operator_settings.write_text("slack:\n  allowed_senders: [U1, U2]\n")
        assert live._allowed_user_ids == {"U1", "U2", "U_SELF"}

    def test_a_pinned_list_ignores_the_file(self, frontend, operator_settings):
        """Every other test in this module pins its lists at construction, and that
        has to keep meaning what it says."""
        operator_settings.write_text("slack:\n  allowed_senders: [U_FILE]\n")
        assert frontend._allowed_user_ids == {"U_ALLOWED", "U_SELF"}


class TestChannelType:
    async def test_a_cached_kind_is_not_refetched(self, frontend):
        frontend._channel_types["C1"] = "im"
        assert await frontend._channel_type("C1") == "im"
        frontend._app.client.conversations_info.assert_not_awaited()

    async def test_a_lookup_failure_is_an_empty_kind(self, frontend, caplog):
        frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "channel_not_found"})
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert await frontend._channel_type("C1") == ""
        assert "channel_type: failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            ({"is_im": True}, "im"),
            ({"is_mpim": True}, "mpim"),
            ({"is_group": True}, "group"),
            ({"is_private": True}, "group"),
            ({}, "channel"),
        ],
    )
    async def test_every_conversation_shape_gets_a_kind(
        self, frontend, flags, expected
    ):
        frontend._app.client.conversations_info = AsyncMock(
            return_value={"channel": flags}
        )
        assert await frontend._channel_type("C1") == expected
        # Cached, so a busy channel costs one API call rather than one per message.
        assert frontend._channel_types["C1"] == expected


class TestIsBotConversation:
    async def test_a_cached_answer_is_not_refetched(self, frontend):
        frontend._own_dm["D1"] = (False, time.monotonic() + 60)
        assert await frontend._is_bot_conversation("D1") is False
        frontend._app.client.conversations_info.assert_not_awaited()

    async def test_an_unexpected_error_fails_closed(self, frontend, caplog):
        """A membership lookup failure must not authorize an unknown DM."""
        frontend._app.client.conversations_info = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert await frontend._is_bot_conversation("D1") is False
        assert "D1" not in frontend._own_dm
        assert "is_bot_conversation" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize("code", ["channel_not_found", "not_in_channel"])
    async def test_a_definitive_not_a_member_error_is_false(self, frontend, code):
        frontend._app.client.conversations_info = AsyncMock(
            side_effect=SlackApiError("nope", {"error": code})
        )
        assert await frontend._is_bot_conversation("D1") is False


class TestSkillPickerModal:
    async def test_no_trigger_id_means_no_modal(self, frontend):
        await frontend._open_skill_picker("", "C1", "U_ALLOWED")
        frontend._app.client.views_open.assert_not_called()

    async def test_a_skill_probe_failure_still_opens_the_modal(self, frontend, caplog):
        """An empty menu beats a modal that never appears."""
        frontend._app.client.views_open = AsyncMock()
        with (
            patch.object(
                slack_mod,
                "cached_skills",
                AsyncMock(side_effect=RuntimeError("cli gone")),
            ),
            caplog.at_level("ERROR", logger="claude_on_the_fly.slack"),
        ):
            await frontend._open_skill_picker("T1", "C1", "U_ALLOWED")
        frontend._app.client.views_open.assert_awaited_once()
        assert "failed to load skills" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_views_open_failure_is_logged_not_raised(self, frontend, caplog):
        frontend._app.client.views_open = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "expired_trigger_id"})
        )
        with (
            patch.object(slack_mod, "cached_skills", AsyncMock(return_value=[])),
            caplog.at_level("ERROR", logger="claude_on_the_fly.slack"),
        ):
            await frontend._open_skill_picker("T1", "C1", "U_ALLOWED")
        assert "views_open failed" in "\n".join(r.getMessage() for r in caplog.records)


class TestWarmSkills:
    async def test_a_warm_failure_does_not_take_the_daemon_down(self, frontend, caplog):
        """Slack gives an options request 3s, so warming is what stops the first
        picker showing an empty menu. Failing it is a slow first picker, not a dead
        frontend."""
        with (
            patch.object(
                slack_mod, "cached_skills", AsyncMock(side_effect=RuntimeError("boom"))
            ),
            caplog.at_level("ERROR", logger="claude_on_the_fly.slack"),
        ):
            await frontend._warm_skills()
        assert "skill warm failed" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_a_successful_warm_reports_the_count(self, frontend, caplog):
        with (
            patch.object(
                slack_mod, "cached_skills", AsyncMock(return_value=[("a", "b")])
            ),
            caplog.at_level("INFO", logger="claude_on_the_fly.slack"),
        ):
            await frontend._warm_skills()
        assert "warmed 1 skills" in "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Skill picker option groups
# ---------------------------------------------------------------------------


class TestOneLine:
    def test_whitespace_is_collapsed(self):
        assert slack_mod._one_line("two\n  lines   here") == "two lines here"

    def test_a_short_description_is_untouched(self):
        assert slack_mod._one_line("short") == "short"

    def test_a_long_description_is_cut_at_a_word_boundary(self):
        """Slack renders these in a menu, so a mid-word cut reads as corruption."""
        text = " ".join(["word"] * 40)
        out = slack_mod._one_line(text)
        assert out.endswith("…")
        assert len(out) <= slack_mod.SKILL_DESC_MAXLEN + 1
        assert "wor…" not in out, "cut mid-word"

    def test_a_single_unbroken_word_is_cut_anyway(self):
        out = slack_mod._one_line("x" * 200)
        assert out == "x" * slack_mod.SKILL_DESC_MAXLEN + "…"


class TestSkillOptionGroups:
    def test_unnamespaced_skills_group_under_user(self):
        groups = slack_mod._skill_option_groups([("review", "Review a diff")])
        assert groups[0]["label"]["text"] == "user"
        assert groups[0]["options"][0]["value"] == "review"
        assert groups[0]["options"][0]["text"]["text"] == "review"

    def test_a_namespaced_skill_groups_under_its_plugin(self):
        """The value stays the full `plugin:skill` name so the forward matches what
        the agent expects, while the label drops the namespace noise."""
        groups = slack_mod._skill_option_groups([("gf-github:pr-create", "Open a PR")])
        assert groups[0]["label"]["text"] == "gf-github"
        assert groups[0]["options"][0]["text"]["text"] == "pr-create"
        assert groups[0]["options"][0]["value"] == "gf-github:pr-create"

    def test_a_description_becomes_the_option_subtitle(self):
        groups = slack_mod._skill_option_groups([("review", "Review a diff")])
        assert groups[0]["options"][0]["description"]["text"] == "Review a diff"

    def test_a_skill_without_a_description_gets_no_subtitle(self):
        groups = slack_mod._skill_option_groups([("review", "")])
        assert "description" not in groups[0]["options"][0]

    def test_groups_and_options_are_sorted(self):
        groups = slack_mod._skill_option_groups([("z:b", ""), ("a:y", ""), ("a:x", "")])
        assert [g["label"]["text"] for g in groups] == ["a", "z"]
        assert [o["text"]["text"] for o in groups[0]["options"]] == ["x", "y"]

    def test_options_are_capped_at_slacks_limit(self):
        """Slack caps a static_select at 100 options per group and 100 groups."""
        groups = slack_mod._skill_option_groups(
            [(f"p:s{i:03d}", "") for i in range(150)]
        )
        assert len(groups[0]["options"]) == 100

    def test_groups_are_capped_too(self):
        groups = slack_mod._skill_option_groups(
            [(f"p{i:03d}:s", "") for i in range(150)]
        )
        assert len(groups) == 100

    def test_long_names_are_truncated_to_slacks_field_limit(self):
        groups = slack_mod._skill_option_groups([("p:" + "x" * 200, "")])
        assert len(groups[0]["options"][0]["text"]["text"]) == 75
        assert len(groups[0]["options"][0]["value"]) == 75


# ---------------------------------------------------------------------------
# Registered Slack handlers
# ---------------------------------------------------------------------------


class TestRegisteredHandlers:
    """The decorated callbacks are thin adapters, but an adapter wired to the wrong
    method is invisible until a real Slack event arrives."""

    async def test_the_message_handler_forwards_to_ingest(self, frontend):
        frontend._ingest_event = AsyncMock()
        frontend._handler = MagicMock()
        with patch.object(slack_mod, "AsyncSocketModeHandler") as handler_cls:
            handler_cls.return_value.start_async = AsyncMock()
            await frontend.start(AsyncMock())
        # `event(...)` returns the same mock decorator for every registration, so
        # take the first call: the message handler is registered before "hello".
        registered = frontend._app.event.return_value.call_args_list[0][0][0]
        await registered({"type": "message", "text": "hi"})
        frontend._ingest_event.assert_awaited_once_with(
            {"type": "message", "text": "hi"}
        )

    async def test_the_bot_token_surface_is_registered_and_wired(self, frontend):
        """Slash commands, the picker, the shortcut, and the approval buttons only
        reach a bot-token install, so a user token must not register them. The
        suggestion buttons are the exception: they render on every reply, so their
        handler lives in start() and is not token-gated."""
        frontend._is_bot_token = True
        frontend._handle_slash_command = AsyncMock()
        frontend._handle_picker_submit = AsyncMock()
        frontend._handle_run_skill_shortcut = AsyncMock()
        frontend._on_approval_action = AsyncMock()
        frontend._on_suggestion_action = AsyncMock()
        with patch.object(slack_mod, "slash_command", lambda: "/cotf"):
            frontend._register_app_interactions()

        command_cb = frontend._app.command.return_value.call_args[0][0]
        await command_cb("ack", "command", "body", "respond")
        frontend._handle_slash_command.assert_awaited_once()

        view_cb = frontend._app.view.return_value.call_args[0][0]
        await view_cb("ack", "view")
        frontend._handle_picker_submit.assert_awaited_once()

        shortcut_cb = frontend._app.shortcut.return_value.call_args[0][0]
        await shortcut_cb("ack", "shortcut")
        frontend._handle_run_skill_shortcut.assert_awaited_once()

        # Only the approval action registers here; the suggestion handler
        # moved to start() so user-token installs get it too.
        action_cbs = [
            call.args[0]
            for call in frontend._app.action.return_value.call_args_list
            if not isinstance(call.args[0], MagicMock)
        ]
        assert len(action_cbs) == 1  # approval only
        patterns = [
            call.args[0].pattern for call in frontend._app.action.call_args_list
        ]
        assert r"^cotf-sugg:" not in patterns
        ack = AsyncMock()
        await action_cbs[0](ack, {"user": {"id": "U_ALLOWED"}})
        assert ack.await_count == 1
        frontend._on_approval_action.assert_awaited_once()

    def test_the_slash_command_is_opt_in(self, frontend):
        """It is workspace-global, so registering it unasked would collide with
        another install."""
        frontend._is_bot_token = True
        with patch.object(slack_mod, "slash_command", lambda: None):
            frontend._register_app_interactions()
        frontend._app.command.assert_not_called()


class TestIngestSubtypeFiltering:
    async def test_an_unhandled_subtype_is_dropped(self, frontend, caplog):
        """channel_join, message_changed and friends are not user messages, and
        forwarding them would have the agent answer Slack's own bookkeeping."""
        frontend._on_message = AsyncMock()
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.slack"):
            await frontend._ingest_event(
                {"type": "message", "subtype": "channel_join", "user": "U_ALLOWED"}
            )
        frontend._on_message.assert_not_awaited()
        assert "skipped: subtype=channel_join" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestSetStatusWithoutAThread:
    async def test_a_session_with_no_thread_gets_no_status(self, frontend):
        """assistant.threads.setStatus needs a thread to attach to."""
        frontend._is_bot_token = True
        frontend._sessions[1] = ("C1", None)
        frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await frontend._set_status(1, "is thinking…")
        frontend._app.client.assistant_threads_setStatus.assert_not_awaited()

    async def test_an_unknown_session_gets_no_status(self, frontend):
        frontend._is_bot_token = True
        frontend._app.client.assistant_threads_setStatus = AsyncMock()
        await frontend._set_status(999, "is thinking…")
        frontend._app.client.assistant_threads_setStatus.assert_not_awaited()


# ---------------------------------------------------------------------------
# Delivery failures
# ---------------------------------------------------------------------------


class TestSendFallsBackToDm:
    """A reply the user never sees is the worst outcome here, so a channel the bot
    was removed from or that got archived falls back to a DM rather than dropping the
    turn's whole answer."""

    def _wire(self, frontend):
        frontend._sessions[1] = ("C1", "1785382860.1")
        frontend._session_sender_ids[1] = "U_ALLOWED"
        frontend._app.client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D1"}}
        )

    @pytest.mark.parametrize(
        "code", ["not_in_channel", "is_archived", "channel_not_found"]
    )
    async def test_a_fallback_error_reroutes_to_the_dm(self, frontend, code):
        self._wire(frontend)
        posts: list[dict] = []

        async def post(**kwargs):
            posts.append(kwargs)
            if kwargs["channel"] == "C1":
                raise SlackApiError("nope", {"error": code})
            return {"ok": True, "ts": "1785382999.1"}

        frontend._app.client.chat_postMessage = AsyncMock(side_effect=post)
        assert await frontend.send(1, Response(body="the answer")) == []
        assert [p["channel"] for p in posts] == ["C1", "D1"]
        assert "couldn't post my reply" in posts[1]["text"]
        assert "the answer" in posts[1]["text"]
        # Link previews stay off on replies and on the fallback DM: the links
        # are for the user to click, not for Slack to fetch.
        assert posts[0]["unfurl_links"] is False
        assert posts[0]["unfurl_media"] is False
        assert posts[1]["unfurl_links"] is False
        assert posts[1]["unfurl_media"] is False

    async def test_a_non_fallback_error_is_just_logged(self, frontend, caplog):
        """A rate limit or a bad payload is not a routing problem, and DMing on
        every error would double-post whenever the channel post was fine."""
        self._wire(frontend)
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "ratelimited"})
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend.send(1, Response(body="x")) == []
        assert "slack api error ratelimited" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_an_unexpected_exception_is_logged_not_raised(self, frontend, caplog):
        self._wire(frontend)
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend.send(1, Response(body="x")) == []
        assert "failed to post message" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_not_ok_response_also_falls_back(self, frontend):
        """Slack can answer 200 with ok:false, which is the same problem as raising."""
        self._wire(frontend)
        posts: list[dict] = []

        async def post(**kwargs):
            posts.append(kwargs)
            if kwargs["channel"] == "C1":
                return {"ok": False, "error": "not_in_channel"}
            return {"ok": True, "ts": "1785382999.1"}

        frontend._app.client.chat_postMessage = AsyncMock(side_effect=post)
        await frontend.send(1, Response(body="x"))
        assert [p["channel"] for p in posts] == ["C1", "D1"]

    async def test_a_not_ok_response_that_is_not_reroutable_returns_nothing(
        self, frontend
    ):
        self._wire(frontend)
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": False, "error": "invalid_blocks"}
        )
        assert await frontend.send(1, Response(body="x")) == []


class TestFallbackDm:
    async def test_a_session_with_no_known_sender_loses_the_reply(
        self, frontend, caplog
    ):
        """Nothing else identifies who to DM, and the log line is the only record
        the answer existed."""
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert await frontend._fallback_dm(1, Response(body="x"), "C1", "e") == []
        assert "response lost" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_a_dm_that_cannot_be_opened_returns_nothing(self, frontend):
        frontend._session_sender_ids[1] = "U_ALLOWED"
        frontend._app.client.conversations_open = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "user_not_found"})
        )
        assert await frontend._fallback_dm(1, Response(body="x"), "C1", "e") == []

    async def test_a_dm_post_failure_returns_nothing(self, frontend, caplog):
        frontend._session_sender_ids[1] = "U_ALLOWED"
        frontend._app.client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D1"}}
        )
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend._fallback_dm(1, Response(body="x"), "C1", "e") == []
        assert "DM to U_ALLOWED failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_not_ok_dm_returns_nothing(self, frontend, caplog):
        frontend._session_sender_ids[1] = "U_ALLOWED"
        frontend._app.client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D1"}}
        )
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": False, "error": "invalid_blocks"}
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend._fallback_dm(1, Response(body="x"), "C1", "e") == []
        assert "DM post failed" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_a_successful_dm_hands_back_the_attachments(self, frontend, tmp_path):
        """The return value is what the orchestrator archives, so claiming a handoff
        that did not happen would make the outbox re-send next turn."""
        attachment = tmp_path / "report.txt"
        attachment.write_text("body")
        frontend._session_sender_ids[1] = "U_ALLOWED"
        frontend._app.client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D1"}}
        )
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "1785382999.1"}
        )
        frontend._upload_attachments = AsyncMock()
        handed = await frontend._fallback_dm(
            1, Response(body="x", attachments=[attachment]), "C1", "e"
        )
        assert handed == [attachment]
        frontend._upload_attachments.assert_awaited_once()


class TestOpenDmChannel:
    async def test_the_channel_id_is_cached(self, frontend):
        frontend._app.client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D1"}}
        )
        assert await frontend._open_dm_channel("U1") == "D1"
        assert await frontend._open_dm_channel("U1") == "D1"
        frontend._app.client.conversations_open.assert_awaited_once()

    async def test_a_failure_is_none_not_an_exception(self, frontend, caplog):
        frontend._app.client.conversations_open = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend._open_dm_channel("U1") is None
        assert "cannot open DM with U1" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestUploadFailures:
    async def test_one_bad_file_does_not_drop_the_rest(
        self, frontend, tmp_path, caplog
    ):
        good = tmp_path / "good.txt"
        good.write_text("a")
        bad = tmp_path / "bad.txt"
        bad.write_text("b")
        uploaded: list[str] = []

        async def upload(**kwargs):
            # `file` carries the bytes; `filename` is the name.
            name = kwargs["filename"]
            if name == "bad.txt":
                raise RuntimeError("socket reset")
            uploaded.append(name)
            return {"ok": True, "files": []}

        frontend._app.client.files_upload_v2 = AsyncMock(side_effect=upload)
        frontend._notify_upload_failure = AsyncMock()
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            await frontend._upload_attachments("C1", None, [bad, good])
        assert uploaded == ["good.txt"]
        assert "failed to send bad.txt" in "\n".join(
            r.getMessage() for r in caplog.records
        )
        # One in-thread heads-up, so the user is not left guessing.
        frontend._notify_upload_failure.assert_awaited_once()

    async def test_the_failure_notice_that_cannot_be_posted_is_logged(
        self, frontend, caplog
    ):
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            await frontend._notify_upload_failure("C1", None, ["a.txt"])
        assert "failed to post failure notice" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestReplyLimitWarning:
    async def test_a_warning_that_cannot_be_posted_is_logged(self, frontend, caplog):
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            await frontend._warn_reply_limit("C1", None)
        assert "failed to post warning" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_posted_warning_records_its_own_ts(self, frontend):
        """Otherwise the bot re-ingests its own warning as an inbound message."""
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "1785382999.1"}
        )
        await frontend._warn_reply_limit("C1", None)
        assert "1785382999.1" in frontend._our_sent_timestamps


class TestAnchorPost:
    async def test_a_failed_anchor_is_none(self, frontend, caplog):
        frontend._app.client.chat_postMessage = AsyncMock(
            side_effect=RuntimeError("socket reset")
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
            assert await frontend._post_anchor("C1", "Running review") is None
        assert "anchor: failed to post" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_not_ok_anchor_is_none(self, frontend):
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": False, "error": "invalid_blocks"}
        )
        assert await frontend._post_anchor("C1", "Running review") is None

    async def test_a_posted_anchor_returns_its_ts_and_records_it(self, frontend):
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "1785382999.1"}
        )
        assert await frontend._post_anchor("C1", "Running review") == "1785382999.1"
        assert "1785382999.1" in frontend._our_sent_timestamps


class TestJobQueueAcknowledgement:
    """The reply must not promise something nothing can deliver: with the trigger on
    by default, an install that never started the worker would otherwise be told
    "I'll reply here when it's done" on a job nothing will ever run, which is a
    failure that looks exactly like success."""

    def _wire(self, frontend):
        frontend._job_queue = MagicMock()
        frontend._job_command = "job"
        frontend._orchestrator = MagicMock()
        frontend._post_notice = AsyncMock()
        frontend._app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "1785382999.1"}
        )
        frontend._app.client.reactions_add = AsyncMock()
        return {
            "type": "message",
            "text": "job go and do the thing",
            "user": "U_ALLOWED",
            "ts": "1785382860.1",
            "channel": "C1",
        }

    async def test_a_live_worker_gets_the_confident_promise(self, frontend):
        event = self._wire(frontend)
        with patch.object(slack_mod, "live_pid", lambda _role: 4242):
            await frontend._ingest_event(event)
        notice = frontend._post_notice.await_args[0][2]
        assert "I'll reply here when it's done" in notice

    async def test_no_worker_says_so_and_names_the_fix(self, frontend):
        event = self._wire(frontend)
        with patch.object(slack_mod, "live_pid", lambda _role: None):
            await frontend._ingest_event(event)
        notice = frontend._post_notice.await_args[0][2]
        assert "no worker is running" in notice
        assert "claude-tui start jobs" in notice, "must name the fix"


class TestThreadContext:
    async def test_an_api_failure_degrades_to_no_context(self, frontend, caplog):
        frontend._app.client.conversations_replies = AsyncMock(
            side_effect=SlackApiError("nope", {"error": "thread_not_found"})
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.slack"):
            assert await frontend._fetch_thread_context("C1", "1.1", "2.2") == ""
        assert "conversations.replies failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_an_empty_thread_yields_nothing(self, frontend):
        frontend._app.client.conversations_replies = AsyncMock(
            return_value={"messages": []}
        )
        assert await frontend._fetch_thread_context("C1", "1.1", "2.2") == ""

    async def test_the_current_message_is_not_included(self, frontend):
        """It is about to be sent as the prompt, so repeating it as context would
        have the agent read the same question twice."""
        frontend._app.client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {"ts": "1.1", "text": "earlier question", "user": "U_ALLOWED"},
                    {"ts": "2.2", "text": "the current one", "user": "U_ALLOWED"},
                ]
            }
        )
        frontend._resolve_message_author = AsyncMock(return_value="hoss")
        out = await frontend._fetch_thread_context("C1", "1.1", "2.2")
        assert "earlier question" in out
        assert "the current one" not in out

    async def test_a_message_with_no_usable_text_is_skipped(self, frontend):
        """A bare file share or a reaction-only message contributes nothing, and an
        empty <message> element is noise in the prompt."""
        frontend._app.client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {"ts": "1.1", "text": "", "user": "U_ALLOWED"},
                    {"ts": "1.2", "text": "real content", "user": "U_ALLOWED"},
                ]
            }
        )
        frontend._resolve_message_author = AsyncMock(return_value="hoss")
        out = await frontend._fetch_thread_context("C1", "1.1", "9.9")
        assert out.count("<message") == 1
        assert "real content" in out

    async def test_a_thread_of_only_empty_messages_yields_nothing(self, frontend):
        frontend._app.client.conversations_replies = AsyncMock(
            return_value={"messages": [{"ts": "1.1", "text": "", "user": "U_ALLOWED"}]}
        )
        frontend._resolve_message_author = AsyncMock(return_value="hoss")
        assert await frontend._fetch_thread_context("C1", "1.1", "9.9") == ""

    async def test_block_content_is_folded_in_alongside_the_text(self, frontend):
        """An app-bot post carries its body in section blocks, with `text` holding
        only a degraded fallback. rich_text blocks are deliberately not folded in:
        for an ordinary user message they just duplicate `text`.
        """
        frontend._app.client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {
                        "ts": "1.1",
                        "text": "see this",
                        "user": "U_ALLOWED",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "quoted body"},
                            }
                        ],
                    }
                ]
            }
        )
        frontend._resolve_message_author = AsyncMock(return_value="hoss")
        out = await frontend._fetch_thread_context("C1", "1.1", "9.9")
        assert "see this" in out
        assert "quoted body" in out


class TestSenderIdentityFallsBackToTheChat:
    """Identity routes prompts and memory. An unseen session must still get a
    stable value rather than None leaking into a path or a prompt."""

    def test_a_known_session_reports_its_slack_user_id(self):
        frontend = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        frontend._session_sender_ids[42] = "U_ALICE"
        assert frontend.sender_identity(42) == "U_ALICE"

    def test_an_unknown_session_reports_the_chat_id(self):
        frontend = SlackFrontend("xapp-tok", "xoxp-tok", "U_SELF")
        assert frontend.sender_identity(42) == "42"
