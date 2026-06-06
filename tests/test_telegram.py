"""Tests for the Telegram frontend."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from claude_on_the_fly.telegram import MAX_MESSAGE_LENGTH, TelegramFrontend


@pytest.fixture
def frontend(tmp_path, monkeypatch) -> TelegramFrontend:
    # Isolate the session-persistence file so /new doesn't write real state.
    monkeypatch.setattr(
        "claude_on_the_fly.telegram.SESSIONS_FILE",
        tmp_path / "telegram-sessions.json",
    )
    return TelegramFrontend(token="fake-token", allowed_user_id=123)


# --- Helpers to build mock Telegram objects ---


def make_user(
    user_id: int = 123, username: str | None = "hoss", first_name: str | None = "Hoss"
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.first_name = first_name
    return user


def make_update(
    user_id: int = 123,
    chat_id: int = 1,
    text: str = "hello",
    username: str | None = "hoss",
    first_name: str | None = "Hoss",
    has_message: bool = True,
) -> MagicMock:
    update = MagicMock()
    user = make_user(user_id=user_id, username=username, first_name=first_name)
    update.effective_user = user
    chat = MagicMock()
    chat.id = chat_id
    update.effective_chat = chat
    if has_message:
        msg = MagicMock()
        msg.text = text
        msg.reply_text = AsyncMock()
        update.message = msg
    else:
        update.message = None
    return update


def make_message(
    text: str | None = None,
    caption: str | None = None,
    document: MagicMock | None = None,
    photo: list | None = None,
    message_id: int = 42,
) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.caption = caption
    msg.document = document
    msg.photo = photo
    msg.message_id = message_id
    return msg


# ============================================================
# workspace_name
# ============================================================


class TestWorkspaceName:
    def test_known_chat(self, frontend: TelegramFrontend) -> None:
        frontend._chat_names[1] = "hoss"
        assert frontend.workspace_name(1) == "telegram/hoss"

    def test_unknown_chat_uses_id(self, frontend: TelegramFrontend) -> None:
        assert frontend.workspace_name(999) == "telegram/999"

    def test_no_session_token_no_suffix(self, frontend: TelegramFrontend) -> None:
        frontend._chat_names[1] = "hoss"
        assert frontend.workspace_name(1) == "telegram/hoss"

    def test_session_token_adds_suffix(self, frontend: TelegramFrontend) -> None:
        frontend._chat_names[1] = "hoss"
        frontend._session_tokens[1] = "20260606-123412"
        assert frontend.workspace_name(1) == "telegram/hoss-20260606-123412"

    def test_unknown_chat_with_token(self, frontend: TelegramFrontend) -> None:
        frontend._session_tokens[5] = "20260606-090000"
        assert frontend.workspace_name(5) == "telegram/5-20260606-090000"


# ============================================================
# sender_name
# ============================================================


class TestSenderName:
    def test_known_sender(self, frontend: TelegramFrontend) -> None:
        frontend._chat_names[1] = "hoss"
        assert frontend.sender_name(1) == "hoss"

    def test_unknown_sender(self, frontend: TelegramFrontend) -> None:
        assert frontend.sender_name(999) == "unknown"


# ============================================================
# channel_context
# ============================================================


class TestChannelContext:
    def test_always_dm(self, frontend: TelegramFrontend) -> None:
        assert frontend.channel_context(1) == "dm"
        assert frontend.channel_context(999) == "dm"


# ============================================================
# set_orchestrator
# ============================================================


class TestSetOrchestrator:
    def test_accepts_orchestrator(self, frontend: TelegramFrontend) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        frontend.set_orchestrator(orch)
        assert frontend._orchestrator is orch

    def test_rejects_non_orchestrator(self, frontend: TelegramFrontend) -> None:
        with pytest.raises(TypeError, match="Expected Orchestrator"):
            frontend.set_orchestrator("not an orchestrator")


# ============================================================
# _send_chunked
# ============================================================


class TestSendChunked:
    async def test_single_chunk(self, frontend: TelegramFrontend) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        await frontend._send_chunked(1, "short message")

        frontend._app.bot.send_message.assert_called_once_with(
            chat_id=1, text="short message", parse_mode="Markdown"
        )

    async def test_multiple_chunks_split_on_lines(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        # Build text that exceeds MAX_MESSAGE_LENGTH with multiple lines
        line = "x" * 2000
        text = f"{line}\n{line}\n{line}"

        await frontend._send_chunked(1, text)

        calls = frontend._app.bot.send_message.call_args_list
        assert len(calls) >= 2
        # Each chunk should be at most MAX_MESSAGE_LENGTH
        for call in calls:
            sent_text = call.kwargs.get("text", call.args[0] if call.args else "")
            assert len(sent_text) <= MAX_MESSAGE_LENGTH

    async def test_very_long_single_line_truncated(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        long_line = "a" * (MAX_MESSAGE_LENGTH + 500)
        await frontend._send_chunked(1, long_line)

        calls = frontend._app.bot.send_message.call_args_list
        assert len(calls) == 1
        sent_text = calls[0].kwargs.get("text", "")
        assert len(sent_text) == MAX_MESSAGE_LENGTH


# ============================================================
# _send_msg
# ============================================================


class TestSendMsg:
    async def test_sends_markdown(self, frontend: TelegramFrontend) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        await frontend._send_msg(1, "hello")

        frontend._app.bot.send_message.assert_called_once_with(
            chat_id=1, text="hello", parse_mode="Markdown"
        )

    async def test_fallback_plain_text_on_bad_request(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock(
            side_effect=[BadRequest("bad markdown"), None]
        )

        await frontend._send_msg(1, "**broken")

        assert frontend._app.bot.send_message.call_count == 2
        second_call = frontend._app.bot.send_message.call_args_list[1]
        assert second_call == ((), {"chat_id": 1, "text": "**broken"})

    async def test_raises_when_app_not_started(
        self, frontend: TelegramFrontend
    ) -> None:
        with pytest.raises(RuntimeError, match="App not started"):
            await frontend._send_msg(1, "hello")


# ============================================================
# _allowed
# ============================================================


class TestAllowed:
    def test_matching_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=123)
        assert frontend._allowed(update) is True

    def test_non_matching_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=999)
        assert frontend._allowed(update) is False

    def test_no_effective_user(self, frontend: TelegramFrontend) -> None:
        update = MagicMock()
        update.effective_user = None
        assert frontend._allowed(update) is False


# ============================================================
# _extract_file
# ============================================================


class TestExtractFile:
    def test_document_with_name(self) -> None:
        doc = MagicMock()
        doc.file_id = "doc123"
        doc.file_name = "report.pdf"
        msg = make_message(document=doc)

        file_id, file_name = TelegramFrontend._extract_file(msg)
        assert file_id == "doc123"
        assert file_name == "report.pdf"

    def test_document_without_name(self) -> None:
        doc = MagicMock()
        doc.file_id = "doc456"
        doc.file_name = None
        msg = make_message(document=doc)

        file_id, file_name = TelegramFrontend._extract_file(msg)
        assert file_id == "doc456"
        assert file_name == "document"

    def test_photo(self) -> None:
        photo_obj = MagicMock()
        photo_obj.file_id = "photo789"
        msg = make_message(photo=[MagicMock(), photo_obj], message_id=42)
        # photo is falsy if empty list, truthy if non-empty
        msg.document = None

        file_id, file_name = TelegramFrontend._extract_file(msg)
        assert file_id == "photo789"
        assert file_name == "photo_42.jpg"

    def test_text_only(self) -> None:
        msg = make_message(text="just text")
        msg.document = None
        msg.photo = []

        file_id, file_name = TelegramFrontend._extract_file(msg)
        assert file_id is None
        assert file_name is None


# ============================================================
# _enqueue_media_group
# ============================================================


class TestEnqueueMediaGroup:
    def test_first_file_creates_group_and_schedules_flush(
        self, frontend: TelegramFrontend
    ) -> None:
        with patch("claude_on_the_fly.telegram.asyncio.create_task") as mock_task:
            frontend._enqueue_media_group("grp1", 1, "fid1", "a.jpg", "caption")

        assert "grp1" in frontend._media_groups
        assert frontend._media_groups["grp1"]["chat_id"] == 1
        assert frontend._media_groups["grp1"]["files"] == [("fid1", "a.jpg")]
        assert frontend._media_groups["grp1"]["caption"] == "caption"
        mock_task.assert_called_once()

    def test_subsequent_files_append(self, frontend: TelegramFrontend) -> None:
        with patch("claude_on_the_fly.telegram.asyncio.create_task"):
            frontend._enqueue_media_group("grp1", 1, "fid1", "a.jpg", "cap")

        # Second file should not create_task again
        with patch("claude_on_the_fly.telegram.asyncio.create_task") as mock_task:
            frontend._enqueue_media_group("grp1", 1, "fid2", "b.jpg", "")

        mock_task.assert_not_called()
        assert len(frontend._media_groups["grp1"]["files"]) == 2

    def test_caption_captured_from_any_file(self, frontend: TelegramFrontend) -> None:
        with patch("claude_on_the_fly.telegram.asyncio.create_task"):
            frontend._enqueue_media_group("grp1", 1, "fid1", "a.jpg", "")
            frontend._enqueue_media_group("grp1", 1, "fid2", "b.jpg", "late caption")

        assert frontend._media_groups["grp1"]["caption"] == "late caption"


# ============================================================
# _track_name
# ============================================================


class TestTrackName:
    def test_sets_name_on_first_call(self, frontend: TelegramFrontend) -> None:
        user = make_user(username="hoss")
        frontend._track_name(1, user)
        assert frontend._chat_names[1] == "hoss"

    def test_does_not_overwrite(self, frontend: TelegramFrontend) -> None:
        frontend._chat_names[1] = "original"
        user = make_user(username="new_name")
        frontend._track_name(1, user)
        assert frontend._chat_names[1] == "original"

    def test_falls_back_to_first_name(self, frontend: TelegramFrontend) -> None:
        user = make_user(username=None, first_name="Hoss")
        frontend._track_name(1, user)
        assert frontend._chat_names[1] == "Hoss"

    def test_falls_back_to_chat_id(self, frontend: TelegramFrontend) -> None:
        user = make_user(username=None, first_name=None)
        frontend._track_name(42, user)
        assert frontend._chat_names[42] == "42"

    def test_none_user_ignored(self, frontend: TelegramFrontend) -> None:
        frontend._track_name(1, None)
        assert 1 not in frontend._chat_names


# ============================================================
# _cmd_new
# ============================================================


class TestCmdNew:
    async def test_mints_unique_timestamp_token(
        self, frontend: TelegramFrontend
    ) -> None:
        update = make_update(chat_id=1)
        await frontend._cmd_new(update, MagicMock())

        token = frontend._session_tokens[1]
        # YYYYMMDD-HHMMSS — unique and sortable, no disk scan or counter.
        assert re.fullmatch(r"\d{8}-\d{6}", token)
        # The workspace suffix is the token, so it never recycles an old dir.
        frontend._chat_names[1] = "hoss"
        assert frontend.workspace_name(1) == f"telegram/hoss-{token}"

    async def test_pushes_token_to_orchestrator_in_step(
        self, frontend: TelegramFrontend
    ) -> None:
        orch = MagicMock()
        orch.set_session_token = MagicMock()
        frontend._orchestrator = orch

        await frontend._cmd_new(make_update(chat_id=5), MagicMock())

        # The session UUID must use the same token as the workspace suffix.
        orch.set_session_token.assert_called_once_with(5, frontend._session_tokens[5])

    async def test_token_does_not_depend_on_disk(
        self, frontend: TelegramFrontend, monkeypatch
    ) -> None:
        # Pruning workspaces can't make /new collide: the token is time-based,
        # not max-of-disk+1. Freeze the minter and confirm the suffix is the
        # token regardless of what's on disk.
        monkeypatch.setattr(frontend, "_mint_session_token", lambda: "20260606-120000")
        frontend._chat_names[9] = "hoss"

        await frontend._cmd_new(make_update(chat_id=9), MagicMock())

        assert frontend._session_tokens[9] == "20260606-120000"
        assert frontend.workspace_name(9) == "telegram/hoss-20260606-120000"

    async def test_replies_with_session_token(self, frontend: TelegramFrontend) -> None:
        update = make_update(chat_id=1)
        await frontend._cmd_new(update, MagicMock())

        token = frontend._session_tokens[1]
        update.message.reply_text.assert_called_once_with(f"New session ({token}).")

    async def test_ignores_unauthorized_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=999, chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_new(update, ctx)

        update.message.reply_text.assert_not_called()


class TestSessionPersistence:
    """A /new session survives a daemon restart instead of snapping back to
    the base session."""

    async def test_new_persists_token_to_disk(
        self, frontend: TelegramFrontend, tmp_path
    ) -> None:
        await frontend._cmd_new(make_update(chat_id=7), MagicMock())

        saved = json.loads((tmp_path / "telegram-sessions.json").read_text())
        assert saved == {"7": frontend._session_tokens[7]}

    def test_load_restores_token_and_pushes_to_orchestrator(
        self, frontend: TelegramFrontend, tmp_path
    ) -> None:
        (tmp_path / "telegram-sessions.json").write_text('{"7": "20260606-120000"}')
        orch = MagicMock()
        orch.set_session_token = MagicMock()
        frontend._orchestrator = orch

        frontend._load_sessions()

        # Resumes the previous session, not base; UUID kept in step via orch.
        assert frontend._session_tokens[7] == "20260606-120000"
        orch.set_session_token.assert_called_once_with(7, "20260606-120000")
        frontend._chat_names[7] = "hoss"
        assert frontend.workspace_name(7) == "telegram/hoss-20260606-120000"

    def test_load_missing_file_is_noop(self, frontend: TelegramFrontend) -> None:
        # Fresh install: no file yet, no crash, no tokens (base session).
        frontend._load_sessions()
        assert frontend._session_tokens == {}


# ============================================================
# _cmd_status
# ============================================================


class TestCmdStatus:
    async def test_shows_working_when_busy(self, frontend: TelegramFrontend) -> None:
        orch = MagicMock()
        orch.is_busy.return_value = True
        orch.queue_size.return_value = 0
        frontend._orchestrator = orch

        update = make_update(chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_status(update, ctx)

        update.message.reply_text.assert_called_once_with("Working...")

    async def test_shows_idle_when_not_busy(self, frontend: TelegramFrontend) -> None:
        orch = MagicMock()
        orch.is_busy.return_value = False
        orch.queue_size.return_value = 0
        frontend._orchestrator = orch

        update = make_update(chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_status(update, ctx)

        update.message.reply_text.assert_called_once_with("Idle.")

    async def test_shows_queue_count(self, frontend: TelegramFrontend) -> None:
        orch = MagicMock()
        orch.is_busy.return_value = True
        orch.queue_size.return_value = 3
        frontend._orchestrator = orch

        update = make_update(chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_status(update, ctx)

        update.message.reply_text.assert_called_once_with("Working... (3 queued)")

    async def test_idle_without_orchestrator(self, frontend: TelegramFrontend) -> None:
        update = make_update(chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_status(update, ctx)

        update.message.reply_text.assert_called_once_with("Idle.")

    async def test_ignores_unauthorized_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=999, chat_id=1)
        ctx = MagicMock()

        await frontend._cmd_status(update, ctx)

        update.message.reply_text.assert_not_called()


# ============================================================
# start
# ============================================================


class TestStart:
    async def test_registers_handlers_and_starts_polling(self) -> None:
        frontend = TelegramFrontend(token="fake-token", allowed_user_id=123)
        on_message = AsyncMock()

        mock_updater = MagicMock()
        mock_updater.start_polling = AsyncMock()

        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.updater = mock_updater
        mock_app.add_handler = MagicMock()

        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with patch("claude_on_the_fly.telegram.Application") as MockApp:
            MockApp.builder.return_value = mock_builder
            await frontend.start(on_message)

        assert frontend._on_message is on_message
        assert frontend._app is mock_app
        assert mock_app.add_handler.call_count == 4
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_updater.start_polling.assert_awaited_once()

    async def test_raises_when_no_updater(self) -> None:
        frontend = TelegramFrontend(token="fake-token", allowed_user_id=123)

        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.updater = None
        mock_app.add_handler = MagicMock()

        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with patch("claude_on_the_fly.telegram.Application") as MockApp:
            MockApp.builder.return_value = mock_builder
            with pytest.raises(RuntimeError, match="Application has no updater"):
                await frontend.start(AsyncMock())


# ============================================================
# stop
# ============================================================


class TestStop:
    async def test_stops_updater_app_and_shuts_down(
        self, frontend: TelegramFrontend
    ) -> None:
        mock_updater = MagicMock()
        mock_updater.stop = AsyncMock()

        mock_app = MagicMock()
        mock_app.updater = mock_updater
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        frontend._app = mock_app

        await frontend.stop()

        mock_updater.stop.assert_awaited_once()
        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()

    async def test_stop_without_updater(self, frontend: TelegramFrontend) -> None:
        mock_app = MagicMock()
        mock_app.updater = None
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        frontend._app = mock_app

        await frontend.stop()

        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()

    async def test_stop_noop_when_no_app(self, frontend: TelegramFrontend) -> None:
        await frontend.stop()  # should not raise


# ============================================================
# send (with stats)
# ============================================================


class TestSend:
    async def test_send_appends_stats_when_present(
        self, frontend: TelegramFrontend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "summary")
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        response = MagicMock()
        response.body = "Here is the answer"
        response.has_stats = True
        response.has_tools = False
        response.format_stats.return_value = "tokens: 100, cost: $0.01"

        await frontend.send(1, response)

        call_args = frontend._app.bot.send_message.call_args
        sent_text = call_args.kwargs["text"]
        assert "Here is the answer" in sent_text
        assert "_tokens: 100, cost: $0.01_" in sent_text

    async def test_send_no_stats(self, frontend: TelegramFrontend) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        response = MagicMock()
        response.body = "plain reply"
        response.has_stats = False
        response.has_tools = False

        await frontend.send(1, response)

        call_args = frontend._app.bot.send_message.call_args
        sent_text = call_args.kwargs["text"]
        assert sent_text == "plain reply"

    async def test_send_with_tools_appends_tool_line(
        self, frontend: TelegramFrontend, monkeypatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "detailed")
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        response = MagicMock()
        response.body = "Here is the answer"
        response.has_stats = True
        response.has_tools = True
        response.format_stats.return_value = "$0.01 | sonnet"
        response.format_tools.return_value = "🔧 5 (Read×3 Bash×2)"

        await frontend.send(1, response)

        sent_text = frontend._app.bot.send_message.call_args.kwargs["text"]
        assert "_$0.01 | sonnet_" in sent_text
        assert "_🔧 5 (Read×3 Bash×2)_" in sent_text

    async def test_send_mode_off_drops_stats_and_tools(
        self, frontend: TelegramFrontend, monkeypatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "off")
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        response = MagicMock()
        response.body = "plain"
        response.has_stats = True
        response.has_tools = True
        response.format_stats.return_value = "stats"
        response.format_tools.return_value = "tools"

        await frontend.send(1, response)

        sent_text = frontend._app.bot.send_message.call_args.kwargs["text"]
        assert sent_text == "plain"

    async def test_send_mode_summary_keeps_stats_drops_tools(
        self, frontend: TelegramFrontend, monkeypatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "summary")
        frontend._app = MagicMock()
        frontend._app.bot.send_message = AsyncMock()

        response = MagicMock()
        response.body = "body"
        response.has_stats = True
        response.has_tools = True
        response.format_stats.return_value = "stats"
        response.format_tools.return_value = "tools"

        await frontend.send(1, response)

        sent_text = frontend._app.bot.send_message.call_args.kwargs["text"]
        assert "_stats_" in sent_text
        assert "tools" not in sent_text

    async def test_send_noop_when_no_app(self, frontend: TelegramFrontend) -> None:
        response = MagicMock()
        response.body = "hi"
        await frontend.send(1, response)  # should not raise


# ============================================================
# send_typing
# ============================================================


class TestSendTyping:
    async def test_sends_chat_action(self, frontend: TelegramFrontend) -> None:
        frontend._app = MagicMock()
        frontend._app.bot.send_chat_action = AsyncMock()

        await frontend.send_typing(42)

        frontend._app.bot.send_chat_action.assert_awaited_once_with(
            chat_id=42, action="typing"
        )

    async def test_noop_when_no_app(self, frontend: TelegramFrontend) -> None:
        await frontend.send_typing(42)  # should not raise


# ============================================================
# _on_update
# ============================================================


class TestOnUpdate:
    async def test_text_message_calls_on_message(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._on_message = AsyncMock()
        frontend._app = MagicMock()

        update = MagicMock()
        update.effective_user = make_user(user_id=123, username="hoss")
        update.effective_chat = MagicMock()
        update.effective_chat.id = 1

        msg = MagicMock()
        msg.text = "hello world"
        msg.caption = None
        msg.document = None
        msg.photo = []
        msg.media_group_id = None
        update.message = msg

        await frontend._on_update(update, MagicMock())

        frontend._on_message.assert_awaited_once()
        call_text = frontend._on_message.call_args[0][1]
        assert "hello world" in call_text
        assert "[from: hoss]" in call_text

    async def test_file_message_saves_and_calls_on_message(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._on_message = AsyncMock()
        frontend._app = MagicMock()
        frontend._app.bot.get_file = AsyncMock()
        mock_tg_file = MagicMock()
        mock_tg_file.download_to_drive = AsyncMock()
        frontend._app.bot.get_file.return_value = mock_tg_file

        update = MagicMock()
        update.effective_user = make_user(user_id=123)
        update.effective_chat = MagicMock()
        update.effective_chat.id = 1

        doc = MagicMock()
        doc.file_id = "file123"
        doc.file_name = "report.pdf"

        msg = MagicMock()
        msg.text = None
        msg.caption = "check this"
        msg.document = doc
        msg.photo = []
        msg.media_group_id = None
        msg.message_id = 10
        update.message = msg

        with patch("claude_on_the_fly.telegram.Path") as MockPath:
            mock_workspace = MagicMock()
            MockPath.home.return_value.__truediv__ = MagicMock(
                return_value=mock_workspace
            )
            mock_workspace.__truediv__ = MagicMock(return_value=mock_workspace)
            mock_workspace.mkdir = MagicMock()
            mock_workspace.name = "report.pdf"

            await frontend._on_update(update, MagicMock())

        frontend._on_message.assert_awaited_once()
        call_text = frontend._on_message.call_args[0][1]
        assert "[File saved: report.pdf]" in call_text
        assert "check this" in call_text

    async def test_media_group_enqueues(self, frontend: TelegramFrontend) -> None:
        frontend._on_message = AsyncMock()
        frontend._app = MagicMock()

        update = MagicMock()
        update.effective_user = make_user(user_id=123)
        update.effective_chat = MagicMock()
        update.effective_chat.id = 1

        doc = MagicMock()
        doc.file_id = "file123"
        doc.file_name = "a.jpg"

        msg = MagicMock()
        msg.text = None
        msg.caption = "group caption"
        msg.document = doc
        msg.photo = []
        msg.media_group_id = "grp42"
        msg.message_id = 10
        update.message = msg

        with patch.object(frontend, "_enqueue_media_group") as mock_enqueue:
            await frontend._on_update(update, MagicMock())

        mock_enqueue.assert_called_once_with(
            "grp42", 1, "file123", "a.jpg", "group caption"
        )
        frontend._on_message.assert_not_awaited()

    async def test_rejected_user_ignored(self, frontend: TelegramFrontend) -> None:
        frontend._on_message = AsyncMock()

        update = MagicMock()
        update.effective_user = make_user(user_id=999)
        update.effective_chat = MagicMock()
        update.effective_chat.id = 1
        update.message = MagicMock()

        await frontend._on_update(update, MagicMock())

        frontend._on_message.assert_not_awaited()

    async def test_no_message_ignored(self, frontend: TelegramFrontend) -> None:
        frontend._on_message = AsyncMock()

        update = MagicMock()
        update.effective_user = make_user(user_id=123)
        update.effective_chat = MagicMock()
        update.message = None

        await frontend._on_update(update, MagicMock())

        frontend._on_message.assert_not_awaited()


# ============================================================
# _on_unsupported
# ============================================================


class TestOnUnsupported:
    async def test_replies_for_allowed_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=123)

        await frontend._on_unsupported(update, MagicMock())

        update.message.reply_text.assert_awaited_once_with(
            "Audio, video, and stickers aren't supported yet. Send text, files, or images."
        )

    async def test_ignores_unauthorized_user(self, frontend: TelegramFrontend) -> None:
        update = make_update(user_id=999)

        await frontend._on_unsupported(update, MagicMock())

        update.message.reply_text.assert_not_called()


# ============================================================
# _save_file
# ============================================================


class TestSaveFile:
    async def test_downloads_file_to_workspace(
        self, frontend: TelegramFrontend, tmp_path: Path
    ) -> None:
        mock_tg_file = MagicMock()
        mock_tg_file.download_to_drive = AsyncMock()

        mock_app = MagicMock()
        mock_app.bot.get_file = AsyncMock(return_value=mock_tg_file)
        frontend._app = mock_app
        frontend._chat_names[1] = "hoss"

        with patch("claude_on_the_fly.telegram.Path") as MockPath:
            # Path.home() / x / y / z chains via __truediv__; just let MagicMock
            # auto-chain and verify the important calls at the end.
            MockPath.home.return_value = tmp_path
            # Path(file_name).name must return a string for the / operator
            safe_path = MagicMock()
            safe_path.name = "report.pdf"
            MockPath.return_value = safe_path

            result = await frontend._save_file(1, "file_abc", "report.pdf")

        mock_app.bot.get_file.assert_awaited_once_with("file_abc")
        mock_tg_file.download_to_drive.assert_awaited_once()
        assert result is not None

    async def test_raises_when_no_app(self, frontend: TelegramFrontend) -> None:
        with pytest.raises(RuntimeError, match="App not started"):
            await frontend._save_file(1, "file_abc", "report.pdf")


# ============================================================
# _flush_media_group
# ============================================================


class TestFlushMediaGroup:
    async def test_saves_all_files_and_calls_on_message(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._on_message = AsyncMock()
        frontend._chat_names[1] = "hoss"
        frontend._media_groups["grp1"] = {
            "chat_id": 1,
            "files": [("fid1", "a.jpg"), ("fid2", "b.png")],
            "caption": "look at these",
        }

        with patch.object(frontend, "_save_file", new_callable=AsyncMock) as mock_save:
            with patch(
                "claude_on_the_fly.telegram.asyncio.sleep", new_callable=AsyncMock
            ):
                await frontend._flush_media_group("grp1")

        assert mock_save.await_count == 2
        mock_save.assert_any_await(1, "fid1", "a.jpg")
        mock_save.assert_any_await(1, "fid2", "b.png")

        frontend._on_message.assert_awaited_once()
        call_text = frontend._on_message.call_args[0][1]
        assert "[File saved: a.jpg]" in call_text
        assert "[File saved: b.png]" in call_text
        assert "look at these" in call_text
        assert "[from: hoss]" in call_text

    async def test_default_text_when_no_caption(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._on_message = AsyncMock()
        frontend._media_groups["grp2"] = {
            "chat_id": 1,
            "files": [("fid1", "a.jpg")],
            "caption": "",
        }

        with patch.object(frontend, "_save_file", new_callable=AsyncMock):
            with patch(
                "claude_on_the_fly.telegram.asyncio.sleep", new_callable=AsyncMock
            ):
                await frontend._flush_media_group("grp2")

        call_text = frontend._on_message.call_args[0][1]
        assert "Please review the uploaded files." in call_text

    async def test_noop_when_group_missing(self, frontend: TelegramFrontend) -> None:
        frontend._on_message = AsyncMock()

        with patch("claude_on_the_fly.telegram.asyncio.sleep", new_callable=AsyncMock):
            await frontend._flush_media_group("nonexistent")

        frontend._on_message.assert_not_awaited()

    async def test_exception_does_not_propagate(
        self, frontend: TelegramFrontend
    ) -> None:
        frontend._on_message = AsyncMock()
        frontend._media_groups["grp3"] = {
            "chat_id": 1,
            "files": [("fid1", "a.jpg")],
            "caption": "",
        }

        with patch.object(
            frontend,
            "_save_file",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "claude_on_the_fly.telegram.asyncio.sleep", new_callable=AsyncMock
            ):
                await frontend._flush_media_group("grp3")  # should not raise

        frontend._on_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# notify_queued (default Frontend impl, not overridden by Telegram)
# ---------------------------------------------------------------------------


async def test_notify_queued_sends_text(frontend: TelegramFrontend) -> None:
    frontend._app = MagicMock()
    frontend._app.bot.send_message = AsyncMock()

    await frontend.notify_queued(1, 3)

    call_args = frontend._app.bot.send_message.call_args
    sent_text = call_args.kwargs["text"]
    assert "Queued (3 pending)" in sent_text
