"""Tests for claude_on_the_fly.orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest

import claude_on_the_fly.agent as agent_mod
from claude_on_the_fly import interim as interim_mod
from claude_on_the_fly import orchestrator as orchestrator_mod
from claude_on_the_fly import permissions as permissions_mod
from claude_on_the_fly import settings
from claude_on_the_fly import turns as turns_mod
from claude_on_the_fly.agent import ClaudeUnavailableError, Compaction, Response
from claude_on_the_fly.events import EventLog
from claude_on_the_fly.orchestrator import (
    SUGGESTIONS_TEMPLATE,
    Orchestrator,
    Turn,
    _extract_suggestions,
    _parse_suggestion_block,
)
from claude_on_the_fly.protocol import Frontend, interrupted_notice
from claude_on_the_fly.turns import PendingTurn

# ---------------------------------------------------------------------------
# Fake frontend
# ---------------------------------------------------------------------------


class StubFrontend(Frontend):
    def __init__(self) -> None:
        self.sent: list[tuple[int, Response]] = []
        self.typing_sent: list[int] = []
        self.queued_notifications: list[tuple[int, int]] = []
        self.start_notifications: list[int] = []
        self.complete_notifications: list[int] = []
        self.progress: list[tuple[int, str]] = []

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        pass

    async def send(self, chat_id: int, response: Response) -> list[Path] | None:
        self.sent.append((chat_id, response))
        return response.attachments

    async def send_typing(self, chat_id: int) -> None:
        self.typing_sent.append(chat_id)

    async def notify_queued(self, chat_id: int, position: int) -> None:
        self.queued_notifications.append((chat_id, position))

    async def notify_start(self, chat_id: int) -> None:
        self.start_notifications.append(chat_id)

    async def notify_complete(self, chat_id: int) -> None:
        self.complete_notifications.append(chat_id)

    async def send_progress(self, chat_id: int, text: str) -> None:
        self.progress.append((chat_id, text))

    async def stop(self) -> None:
        pass

    def workspace_name(self, chat_id: int) -> str:
        return f"test/{chat_id}"

    def sender_name(self, chat_id: int) -> str:
        return f"user-{chat_id}"

    def channel_context(self, chat_id: int) -> str:
        return "dm"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def journal_in_tmp(tmp_path, monkeypatch):
    """Keep every test's pending-turn journal to itself.

    The suite shares one redirected home for the whole session, so without this
    each Orchestrator writes its journal to the same file and a turn recorded by
    one test is resumed by the next.
    """
    monkeypatch.setattr(orchestrator_mod, "DATA_DIR", tmp_path)


@pytest.fixture
def frontend() -> StubFrontend:
    return StubFrontend()


@pytest.fixture
def event_log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


@pytest.fixture
def orch(frontend: StubFrontend, event_log: EventLog) -> Orchestrator:
    return Orchestrator(frontend, "test", event_log=event_log)


# ---------------------------------------------------------------------------
# session_uuid
# ---------------------------------------------------------------------------


class TestSessionUUID:
    def test_same_chat_id_returns_same_uuid(self, orch: Orchestrator) -> None:
        assert orch.session_uuid(1) == orch.session_uuid(1)

    def test_different_chat_id_returns_different_uuid(self, orch: Orchestrator) -> None:
        assert orch.session_uuid(1) != orch.session_uuid(2)

    def test_reset_changes_uuid(self, orch: Orchestrator) -> None:
        before = orch.session_uuid(1)
        orch.reset_session(1)
        after = orch.session_uuid(1)
        assert before != after

    def test_deterministic_uuid5(self, orch: Orchestrator) -> None:
        expected = str(uuid5(NAMESPACE_URL, "42"))
        assert orch.session_uuid(42) == expected

    def test_deterministic_uuid5_after_reset(self, orch: Orchestrator) -> None:
        orch.reset_session(7)
        expected = str(uuid5(NAMESPACE_URL, "7-1"))
        assert orch.session_uuid(7) == expected


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------


class TestResetSession:
    def test_increments_counter(self, orch: Orchestrator) -> None:
        orch.reset_session(1)
        assert orch._session_counters[1] == 1

    def test_multiple_resets_keep_incrementing(self, orch: Orchestrator) -> None:
        for _ in range(5):
            orch.reset_session(1)
        assert orch._session_counters[1] == 5


class TestSetSessionToken:
    def test_pins_token(self, orch: Orchestrator) -> None:
        orch.set_session_token(1, "20260606-120000")
        assert orch._session_counters[1] == "20260606-120000"
        # Stable: same chat + token -> same UUID, so resume works.
        assert orch.session_uuid(1) == orch.session_uuid(1)

    def test_token_changes_uuid_from_base(self, orch: Orchestrator) -> None:
        base = orch.session_uuid(1)
        orch.set_session_token(1, "20260606-120000")
        assert orch.session_uuid(1) != base

    def test_overwrites_scheduler_counter(self, orch: Orchestrator) -> None:
        orch.reset_session(1)  # scheduler-style int bump -> 1
        orch.set_session_token(1, "20260606-120000")
        assert orch._session_counters[1] == "20260606-120000"


# ---------------------------------------------------------------------------
# is_busy
# ---------------------------------------------------------------------------


class TestIsBusy:
    def test_false_when_no_task(self, orch: Orchestrator) -> None:
        assert orch.is_busy(1) is False

    async def test_true_when_task_running(self, orch: Orchestrator) -> None:
        loop = asyncio.get_running_loop()
        pending_future = loop.create_future()
        orch._running[1] = asyncio.ensure_future(pending_future)  # type: ignore[assignment]
        try:
            assert orch.is_busy(1) is True
        finally:
            pending_future.set_result(None)

    async def test_false_when_task_done(self, orch: Orchestrator) -> None:
        loop = asyncio.get_running_loop()
        done_future = loop.create_future()
        done_future.set_result(None)
        orch._running[1] = asyncio.ensure_future(done_future)  # type: ignore[assignment]
        assert orch.is_busy(1) is False


# ---------------------------------------------------------------------------
# queue_size
# ---------------------------------------------------------------------------


class TestQueueSize:
    def test_zero_when_no_queue(self, orch: Orchestrator) -> None:
        assert orch.queue_size(99) == 0

    def test_returns_actual_size(self, orch: Orchestrator) -> None:
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait("a")
        q.put_nowait("b")
        orch._queues[1] = q
        assert orch.queue_size(1) == 2


# ---------------------------------------------------------------------------
# on_message
# ---------------------------------------------------------------------------


class TestOnMessage:
    async def test_first_message_starts_drain(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        with patch.object(orch, "_drain", new_callable=AsyncMock) as mock_drain:
            await orch.on_message(1, "hello")
            # Drain task was created
            assert 1 in orch._running
            # Let it run
            await asyncio.sleep(0)
            mock_drain.assert_called_once_with(1)

    async def test_second_message_while_busy_queues(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        # Simulate a long-running drain that blocks forever
        blocker = asyncio.get_event_loop().create_future()

        async def slow_drain(chat_id: int) -> None:
            await blocker

        with patch.object(orch, "_drain", side_effect=slow_drain):
            await orch.on_message(1, "first")
            await asyncio.sleep(0)  # let drain task start

            await orch.on_message(1, "second")

        # notify_queued was called (chat_id, position)
        assert frontend.queued_notifications == [(1, 2)]
        assert frontend.sent == []  # no chat reply for queueing

        blocker.set_result(None)

    async def test_creates_queue_if_missing(self, orch: Orchestrator) -> None:
        assert 1 not in orch._queues
        with patch.object(orch, "_drain", new_callable=AsyncMock):
            await orch.on_message(1, "hi")
        assert 1 in orch._queues


# ---------------------------------------------------------------------------
# _drain
# ---------------------------------------------------------------------------


class TestDrain:
    async def test_processes_all_queued_messages(self, orch: Orchestrator) -> None:
        processed: list[str] = []

        async def fake_process(chat_id: int, turn: Turn) -> None:
            processed.append(turn.text)

        orch._queues[1] = asyncio.Queue()
        orch._queues[1].put_nowait(Turn("msg1"))
        orch._queues[1].put_nowait(Turn("msg2"))
        orch._queues[1].put_nowait(Turn("msg3"))

        with patch.object(orch, "_process", side_effect=fake_process):
            # Simulate being the running task
            task = asyncio.create_task(orch._drain(1))
            orch._running[1] = task
            await task

        assert processed == ["msg1", "msg2", "msg3"]

    async def test_cleans_up_running_when_done(self, orch: Orchestrator) -> None:
        orch._queues[1] = asyncio.Queue()

        with patch.object(orch, "_process", new_callable=AsyncMock):
            task = asyncio.create_task(orch._drain(1))
            orch._running[1] = task
            await task

        assert 1 not in orch._running

    async def test_handles_empty_queue(self, orch: Orchestrator) -> None:
        orch._queues[1] = asyncio.Queue()

        with patch.object(orch, "_process", new_callable=AsyncMock) as mock_proc:
            task = asyncio.create_task(orch._drain(1))
            orch._running[1] = task
            await task

        mock_proc.assert_not_called()


# ---------------------------------------------------------------------------
# _process
# ---------------------------------------------------------------------------


class TestProcess:
    async def test_creates_workspace_and_calls_agent(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        fake_response = Response(body="answer", cost=0.01, tokens_in=100, tokens_out=50)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=fake_response)
            await orch._process(1, Turn("question"))

        workspace = tmp_path / "workspaces" / "test/1"
        assert workspace.exists()

        mock_agent.run.assert_called_once_with(
            workspace,
            orch.session_uuid(1),
            "question",
            "test",
            user_name="user-1",
            channel_context="dm",
            timeout=None,
            nudge_prompt=None,
        )

        # Response was sent
        assert len(frontend.sent) == 1
        assert frontend.sent[0][1].body == "answer"
        # Suggestions disabled: empty means no buttons.
        assert frontend.sent[0][1].suggestions == []

    async def test_links_the_frontends_own_persona(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        """A frontend that keys personas per chat decides which file gets linked;
        the default None keeps the single global persona."""
        persona = tmp_path / "oncall.md"
        persona.write_text("# oncall")
        assert frontend.persona_source(1) is None  # the protocol default
        frontend.persona_source = lambda chat_id: persona  # type: ignore[method-assign]
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="answer"))
            await orch._process(1, Turn("question"))

        workspace = tmp_path / "workspaces" / "test/1"
        mock_agent.ensure_persona.assert_called_once_with(workspace, persona)

    async def test_appends_template_and_parses_suggestions_when_enabled(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("COTF_SUGGESTIONS_ENABLED", "true")
        fake_response = Response(
            body='Here is the answer <suggestions>["ask a?", "ask b?"]</suggestions>'
        )
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=fake_response)
            await orch._process(1, Turn("question"))

        prompt = mock_agent.run.call_args.args[2]
        assert prompt == f"question\n\n{SUGGESTIONS_TEMPLATE}"
        sent = frontend.sent[0][1]
        # The machine half of the reply never reaches the user.
        assert sent.body == "Here is the answer"
        assert sent.suggestions == ["ask a?", "ask b?"]

    async def test_malformed_block_yields_no_suggestions(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("COTF_SUGGESTIONS_ENABLED", "true")
        fake_response = Response(
            body="answer <suggestions>not json at all</suggestions>"
        )
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=fake_response)
            await orch._process(1, Turn("question"))

        sent = frontend.sent[0][1]
        assert sent.body == "answer"
        # Empty suggestions render no buttons rather than stale static ones.
        assert sent.suggestions == []

    async def test_block_only_reply_gets_a_placeholder(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("COTF_SUGGESTIONS_ENABLED", "true")
        fake_response = Response(body='<suggestions>["only?"]</suggestions>')
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=fake_response)
            await orch._process(1, Turn("question"))

        sent = frontend.sent[0][1]
        # Not "Suggestions:" — the labels are dropped below, so naming them
        # would promise buttons that are not there.
        assert sent.body == "No response"
        # A reply that is only buttons is a skipped reply: no body to pair
        # the buttons with, so they are dropped rather than shown blind.
        assert sent.suggestions == []

    async def test_the_nudge_prompt_carries_the_suggestions_template(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The backend's nudge retry must come back with working buttons, so
        the nudge prompt carries the same suggestions instruction as the
        turn's own prompt."""
        monkeypatch.setenv("COTF_SUGGESTIONS_ENABLED", "true")
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="answer"))
            await orch._process(1, Turn("question"))

        nudge = mock_agent.run.call_args.kwargs["nudge_prompt"]
        assert nudge is not None
        assert "<cotf-suggest>" in nudge
        assert "<suggestions>" in nudge

    async def test_command_token_is_bound_to_the_turn_workspace_and_revoked(
        self, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        command_broker = MagicMock()
        command_broker.agent_env.side_effect = lambda workspace: {
            orchestrator_mod.commands.ENDPOINT_ENV: "http://127.0.0.1:1234",
            orchestrator_mod.commands.TOKEN_ENV: f"token:{workspace}",
        }
        orch = Orchestrator(frontend, "test", command_broker=command_broker)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="answer"))
            await orch._process(1, Turn("question"))

        workspace = tmp_path / "workspaces" / "test/1"
        command_broker.agent_env.assert_called_once_with(workspace)
        command_broker.revoke_token.assert_called_once_with(f"token:{workspace}")

    async def test_sends_error_on_agent_failure(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(1, Turn("bad prompt"))

        assert len(frontend.sent) == 1
        assert "Error: boom" in frontend.sent[0][1].body

    async def test_notifies_complete_on_success(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, Turn("question"))

        assert frontend.complete_notifications == [1]

    async def test_notifies_complete_on_error(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(1, Turn("bad"))

        assert frontend.complete_notifications == [1]

    async def test_unavailable_error_uses_distinct_message(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(
                side_effect=ClaudeUnavailableError("monthly usage limit")
            )
            await orch._process(1, Turn("hi"))

        assert len(frontend.sent) == 1
        body = frontend.sent[0][1].body
        assert body.startswith("Claude unavailable:")
        assert "monthly usage limit" in body

    async def test_timeout_threaded_from_frontend(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        frontend.timeout_for = lambda chat_id: 99.0  # type: ignore[method-assign]

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, Turn("hi"))

        assert mock_agent.run.call_args.kwargs["timeout"] == 99.0

    async def test_typing_loop_is_cancelled_after_process(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, Turn("hello"))

        # Typing indicator should have been sent at least once before cancel
        # (the loop fires immediately then sleeps 4s, but agent.run is instant
        # with our mock so it may or may not have fired -- we just verify no
        # lingering tasks are running)
        await asyncio.sleep(0.05)
        # If typing_task leaked we'd see ongoing send_typing calls; this is
        # covered implicitly by no hanging tasks.

    async def test_attaches_and_archives_outbox_for_attachment_platform(
        self, frontend: StubFrontend, event_log: EventLog, tmp_path: Path
    ) -> None:
        from claude_on_the_fly import agent

        orch = Orchestrator(frontend, "slack", event_log=event_log)
        outbox = tmp_path / "workspaces" / "test/1" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "report.csv").write_text("data")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(agent, "run", AsyncMock(return_value=Response(body="ok"))),
        ):
            await orch._process(1, Turn("make a file"))

        sent = frontend.sent[0][1]
        assert [p.name for p in sent.attachments] == ["report.csv"]
        assert not (outbox / "report.csv").exists()  # archived, not left behind
        assert list((outbox / ".sent").rglob("report.csv"))

    async def test_does_not_archive_when_send_reports_nothing_delivered(
        self, event_log: EventLog, tmp_path: Path
    ) -> None:
        # If send() couldn't deliver (e.g. text post failed), the file must stay
        # in outbox for a retry, not get archived out from under the user.
        from claude_on_the_fly import agent

        class UndeliveredFrontend(StubFrontend):
            async def send(self, chat_id, response):
                self.sent.append((chat_id, response))
                return []

        frontend = UndeliveredFrontend()
        orch = Orchestrator(frontend, "slack", event_log=event_log)
        outbox = tmp_path / "workspaces" / "test/1" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "report.csv").write_text("data")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(agent, "run", AsyncMock(return_value=Response(body="ok"))),
        ):
            await orch._process(1, Turn("make a file"))

        assert (outbox / "report.csv").exists()  # preserved for retry
        assert not (outbox / ".sent").exists()  # nothing archived

    async def test_skips_outbox_for_non_attachment_platform(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        from claude_on_the_fly import agent

        outbox = tmp_path / "workspaces" / "test/1" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "report.csv").write_text("data")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(agent, "run", AsyncMock(return_value=Response(body="ok"))),
        ):
            await orch._process(1, Turn("hi"))

        assert frontend.sent[0][1].attachments == []
        assert (outbox / "report.csv").exists()  # untouched


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _typing_loop
# ---------------------------------------------------------------------------


class TestTypingLoop:
    async def test_calls_send_typing_repeatedly(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        task = asyncio.create_task(orch._typing_loop(1))
        # Let it fire at least once
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(frontend.typing_sent) >= 1
        assert frontend.typing_sent[0] == 1


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    async def test_sets_up_and_shuts_down(self, tmp_path: Path) -> None:
        stub = StubFrontend()
        started = False

        async def fake_start(on_message):
            nonlocal started
            started = True

        stub.start = fake_start  # type: ignore[assignment]
        stub.stop = AsyncMock()  # type: ignore[assignment]

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch(
                "claude_on_the_fly.orchestrator.asyncio.get_event_loop"
            ) as mock_loop_fn,
            patch("claude_on_the_fly.orchestrator.asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            # Make stop.wait() return immediately
            mock_event.wait = AsyncMock()
            mock_event_cls.return_value = mock_event

            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop

            from claude_on_the_fly.orchestrator import run

            await run(stub, platform="test")

        # Memory dirs created
        assert (tmp_path / "memory" / "users").exists()
        assert (tmp_path / "memory" / "knowledge").exists()
        # Frontend stop called during shutdown
        stub.stop.assert_awaited_once()  # type: ignore[union-attr]
        # Signal handlers were registered
        assert mock_loop.add_signal_handler.call_count == 2


async def _hang(fut: asyncio.Future) -> None:
    """A turn that never finishes on its own, so a stop has something to cancel."""
    await fut


class TestShutdown:
    async def test_cancels_all_running_tasks(self, orch: Orchestrator) -> None:
        blocker1 = asyncio.get_event_loop().create_future()
        blocker2 = asyncio.get_event_loop().create_future()

        async def hang(fut: asyncio.Future) -> None:
            await fut

        task1 = asyncio.create_task(hang(blocker1))
        task2 = asyncio.create_task(hang(blocker2))
        orch._running[1] = task1
        orch._running[2] = task2

        await orch.shutdown()

        assert task1.cancelled()
        assert task2.cancelled()

    async def test_a_chat_mid_answer_is_told_before_its_turn_is_cancelled(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        """The failure this exists for: the daemon dies, the turn is cancelled,
        and the person who asked gets silence forever."""
        blocker = asyncio.get_event_loop().create_future()
        orch._running[5] = asyncio.create_task(_hang(blocker))

        await orch.shutdown()

        assert [chat_id for chat_id, _ in frontend.sent] == [5]
        assert "Restarting" in frontend.sent[0][1].body

    async def test_queued_turns_are_counted_in_the_notice(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        blocker = asyncio.get_event_loop().create_future()
        orch._running[5] = asyncio.create_task(_hang(blocker))
        orch._queues[5] = asyncio.Queue()
        orch._queues[5].put_nowait(Turn("second"))
        orch._queues[5].put_nowait(Turn("third"))

        await orch.shutdown()

        # Three in total (one running, two queued), so the plural form.
        assert "back to these" in frontend.sent[0][1].body

    async def test_a_chat_with_only_queued_turns_is_told_too(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        """A drain task can finish between the queue filling and the stop."""
        orch._queues[8] = asyncio.Queue()
        orch._queues[8].put_nowait(Turn("waiting"))

        await orch.shutdown()

        assert "back to this" in frontend.sent[0][1].body

    async def test_an_idle_daemon_says_nothing(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        """A restart that costs nobody an answer must not wake anybody up."""
        orch._queues[8] = asyncio.Queue()

        await orch.shutdown()

        assert frontend.sent == []

    async def test_one_chat_failing_does_not_cost_the_others_their_notice(
        self, orch: Orchestrator, frontend: StubFrontend
    ) -> None:
        blocker = asyncio.get_event_loop().create_future()
        orch._running[1] = asyncio.create_task(_hang(blocker))
        orch._running[2] = asyncio.create_task(_hang(blocker))

        async def fail_for_chat_one(chat_id: int, **_kw) -> None:
            if chat_id == 1:
                raise RuntimeError("channel_not_found")
            frontend.sent.append((chat_id, Response(body="notice")))

        frontend.notify_interrupted = fail_for_chat_one  # type: ignore[method-assign]

        await orch.shutdown()

        assert [chat_id for chat_id, _ in frontend.sent] == [2]
        assert orch._running[1].cancelled()

    async def test_a_frontend_that_hangs_cannot_hold_the_shutdown_open(
        self, orch: Orchestrator, frontend: StubFrontend, monkeypatch, caplog
    ) -> None:
        """The supervisor SIGKILLs after its grace, so an unreachable API must
        cost the exit a bounded wait rather than the whole window."""
        blocker = asyncio.get_event_loop().create_future()
        task = asyncio.create_task(_hang(blocker))
        orch._running[1] = task
        monkeypatch.setattr(orchestrator_mod, "SHUTDOWN_NOTICE_BUDGET_S", 0.01)

        async def never_returns(*_a, **_kw) -> None:
            await asyncio.Event().wait()

        frontend.notify_interrupted = never_returns  # type: ignore[method-assign]

        with caplog.at_level("WARNING", logger="claude_on_the_fly.orchestrator"):
            await orch.shutdown()

        assert "unfinished" in caplog.text
        assert task.cancelled()

    async def test_the_heartbeat_publishes_the_queued_depth(
        self, orch: Orchestrator
    ) -> None:
        """Queued turns live in this process's memory, so the supervisor can only
        warn about them if the heartbeat carries the count."""
        orch._queues[1] = asyncio.Queue()
        orch._queues[1].put_nowait(Turn("a"))
        orch._queues[2] = asyncio.Queue()
        orch._queues[2].put_nowait(Turn("b"))

        assert orch.heartbeat_extra()["queued_turns"] == 2

    async def test_interrupted_chats_reports_running_and_queued_per_chat(
        self, orch: Orchestrator
    ) -> None:
        done: asyncio.Future = asyncio.get_event_loop().create_future()
        done.set_result(None)
        finished = asyncio.ensure_future(_hang(done))
        await finished
        orch._running[3] = finished
        orch._queues[4] = asyncio.Queue()
        orch._queues[4].put_nowait(Turn("a"))

        # A finished drain task is not interrupted work; an unstarted queue is.
        assert orch.interrupted_chats() == {4: (False, 1)}


# ---------------------------------------------------------------------------
# Event log emission (cross-frontend audit trail)
# ---------------------------------------------------------------------------


class TestEventEmission:
    async def test_dispatched_and_worker_done_on_success(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
    ) -> None:
        response = Response(body="ok", cost=0.02, tokens_in=10, tokens_out=20)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=response)
            await orch._process(7, Turn("hi"))

        events = event_log.tail(10)
        types = [e["type"] for e in events]
        assert types == ["dispatched", "worker_done"]
        for e in events:
            assert e["source"] == "test"
            assert e["identifier"] == "test/7"
        done = events[-1]
        assert done["cost"] == 0.02
        assert done["tokens_in"] == 10
        assert done["tokens_out"] == 20

    async def test_worker_failed_on_unavailable_error(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=ClaudeUnavailableError("rate limit"))
            await orch._process(7, Turn("hi"))

        events = event_log.tail(10)
        types = [e["type"] for e in events]
        assert types == ["dispatched", "worker_failed"]
        failed = events[-1]
        assert failed["source"] == "test"
        assert failed["reason"] == "unavailable"
        assert "rate limit" in failed["error"]

    async def test_worker_failed_on_generic_exception(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(7, Turn("hi"))

        events = event_log.tail(10)
        types = [e["type"] for e in events]
        assert types == ["dispatched", "worker_failed"]
        failed = events[-1]
        assert failed["source"] == "test"
        assert "reason" not in failed
        assert "boom" in failed["error"]

    async def test_in_flight_cleared_after_run(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
    ) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(7, Turn("hi"))
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(8, Turn("hi"))

        # Both chat_ids should be removed from in-flight after _process exits.
        assert orch._in_flight == {}

    async def test_heartbeat_extra_lists_in_flight_jobs(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
    ) -> None:
        gate = asyncio.Event()

        async def hang(*_a, **_kw) -> Response:
            await gate.wait()
            return Response(body="ok")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=hang)
            task = asyncio.create_task(orch._process(9, Turn("hi")))
            # Yield until the orchestrator has registered the in-flight slot.
            for _ in range(20):
                if orch._in_flight:
                    break
                await asyncio.sleep(0.01)

            extra = orch.heartbeat_extra()
            assert len(extra["running_jobs"]) == 1
            job = extra["running_jobs"][0]
            assert job["identifier"] == "test/9"
            assert job["chat_id"] == 9
            assert "session_uuid" in job

            gate.set()
            await task

        assert orch.heartbeat_extra() == {"running_jobs": [], "queued_turns": 0}


# ---------------------------------------------------------------------------
# _process: mid-turn progress wiring
# ---------------------------------------------------------------------------


def _watch_send_ordering(frontend: StubFrontend) -> list[int]:
    """How many progress messages had landed each time `send` was called.

    The relay is closed before the reply is posted, so a progress line can never
    arrive after the message that ends the turn. Two independent lists cannot show
    that on their own.
    """
    seen: list[int] = []
    original = frontend.send

    async def recording_send(chat_id: int, response: Response) -> list[Path] | None:
        seen.append(len(frontend.progress))
        return await original(chat_id, response)

    frontend.send = recording_send  # type: ignore[method-assign]
    return seen


class SilentFrontend(StubFrontend):
    """A frontend that never overrode `send_progress` — the Telegram shape.

    Re-inheriting the ABC's concrete no-op is the whole point: `StubFrontend`
    overrides it, and the gate under test is exactly "did anyone override this?".
    """

    send_progress = Frontend.send_progress


class TestProcessInterim:
    async def test_a_frontend_that_cannot_deliver_starts_no_relay(
        self,
        event_log: EventLog,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """`send_progress` is a concrete no-op on the ABC, so the setting alone is
        not enough: with the toggle on under Telegram, every turn would otherwise
        spawn a drain task that ticks for the life of the turn and delivers into
        nothing."""
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        frontend = SilentFrontend()
        orch = Orchestrator(frontend, "test", event_log=event_log)
        seen: list[object] = []

        async def fake_run(*_args, **_kwargs) -> Response:
            seen.append(orchestrator_mod.agent._PROGRESS_SINK.get())
            return Response(body="done")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch._process(1, Turn("hi"))

        assert orch._start_interim(1) is None
        assert seen == [None]
        assert frontend.progress == []

    async def test_narration_reaches_the_frontend_mid_turn(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_WARMUP_S", 0.0)
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_MIN_GAP_S", 0.0)
        order = _watch_send_ordering(frontend)

        async def fake_run(*_args, **_kwargs) -> Response:
            # Exactly once: with the gap at 0.0 every post is immediately due, so
            # a second emit here would pass without exercising coalescing at all.
            # The limiter's own behaviour is proved in tests/test_interim.py.
            sink = orchestrator_mod.agent._PROGRESS_SINK.get()
            assert sink is not None
            sink("盤點完成")
            return Response(body="done")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch._process(1, Turn("hi"))

        assert frontend.progress == [(1, "盤點完成")]
        assert order == [1]

    async def test_toggle_off_is_identical_to_today(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.delenv("COTF_INTERIM_PROGRESS", raising=False)
        seen: list[object] = []
        response = Response(body="ok", cost=0.02, tokens_in=10, tokens_out=20)

        async def fake_run(*_args, **_kwargs) -> Response:
            seen.append(orchestrator_mod.agent._PROGRESS_SINK.get())
            return response

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch._process(1, Turn("hi"))

        assert seen == [None]
        assert frontend.progress == []
        assert frontend.sent == [(1, response)]
        done = event_log.tail(10)[-1]
        assert done["type"] == "worker_done"
        assert done["cost"] == 0.02
        assert done["tokens_in"] == 10
        assert done["tokens_out"] == 20

    async def test_a_compaction_turn_starts_no_relay(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """A compaction produces no assistant message to narrate."""
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.compact = AsyncMock(return_value=Compaction(ok=True))
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("", compact=True))

        assert frontend.progress == []

    async def test_a_failing_turn_flushes_the_last_narration_before_the_error_reply(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """The GAP is deliberately NOT patched away, and TWO lines are narrated,
        which is what makes the flush the only thing that can release the second.

        The warm-up is patched to 0 because the flush respects it — a turn that
        fails before the warm-up posts nothing, which `tests/test_interim.py`
        pins separately. But warm-up 0 with a single line proves nothing: with no
        previous post the limiter has nothing to hold against, so that line goes
        out through the ordinary path and the assertion passes with
        `flush=False`. The first line here is what starts the gap; the second is
        held inside it, on the real 300s default, until `aclose(flush=True)`.
        """
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_WARMUP_S", 0.0)
        order = _watch_send_ordering(frontend)

        async def fake_run(*_args, **_kwargs) -> Response:
            emit = orchestrator_mod.agent._PROGRESS_SINK.get()
            emit("halfway")
            emit("and then it broke")
            raise RuntimeError("boom")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch._process(1, Turn("hi"))

        assert frontend.progress == [(1, "halfway"), (1, "and then it broke")]
        assert order == [2]
        assert "Error: boom" in frontend.sent[0][1].body
        assert event_log.tail(10)[-1]["type"] == "worker_failed"

    async def test_an_unavailable_backend_takes_the_same_flush_path(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        event_log: EventLog,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """One inner arm covers both outer handlers; there is no second site.

        Two lines, and the gap left real, for the reason spelled out on the test
        above: the second is held by the limiter and only the flush releases it.
        """
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_WARMUP_S", 0.0)
        order = _watch_send_ordering(frontend)

        async def fake_run(*_args, **_kwargs) -> Response:
            emit = orchestrator_mod.agent._PROGRESS_SINK.get()
            emit("halfway")
            emit("and then it broke")
            raise ClaudeUnavailableError("monthly usage limit")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch._process(1, Turn("hi"))

        assert frontend.progress == [(1, "halfway"), (1, "and then it broke")]
        assert order == [2]
        assert "Claude unavailable" in frontend.sent[0][1].body
        assert event_log.tail(10)[-1]["reason"] == "unavailable"

    async def test_an_aborted_turn_cancels_the_relay_without_awaiting(
        self,
        orch: Orchestrator,
        frontend: StubFrontend,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """Every assertion reads through the sink the turn itself captured. The
        ContextVar is None in the test's own context whatever the code does, which
        is what made an earlier version of this test unable to fail."""
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "1")
        holder: dict[str, object] = {}
        release = asyncio.Event()

        async def fake_run(*_args, **_kwargs) -> Response:
            holder["sink"] = orchestrator_mod.agent._PROGRESS_SINK.get()
            await release.wait()
            return Response(body="never")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch.object(orchestrator_mod.agent, "run", new=fake_run),
        ):
            await orch.on_message(1, "go")
            for _ in range(200):
                if "sink" in holder:
                    break
                await asyncio.sleep(0.01)

            assert await orch.abort(1) is True

        relay = holder["sink"].__self__  # type: ignore[attr-defined]
        assert isinstance(relay, interim_mod.InterimProgress)
        assert relay._closed is True
        await asyncio.sleep(0)
        assert relay._task.done()


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------


class TestAbort:
    async def test_returns_false_when_idle(self, orch: Orchestrator) -> None:
        assert await orch.abort(999) is False

    async def test_cancels_running_turn_and_clears_queue(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_run(*args, **kwargs):
            started.set()
            await release.wait()  # held open to simulate a long turn
            return Response(body="done")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = slow_run
            await orch.on_message(1, "go")
            await asyncio.wait_for(started.wait(), timeout=2)
            orch._queues[1].put_nowait(Turn("queued-behind"))
            assert orch.is_busy(1)

            stopped = await orch.abort(1)

        assert stopped is True
        assert orch.queue_size(1) == 0
        assert 1 not in orch._in_flight
        assert not orch.is_busy(1)

    async def test_cancellation_during_startup_cleans_lifecycle_state(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        """A stop may land while notify_start is awaiting Slack I/O."""
        started = asyncio.Event()

        async def slow_notify_start(chat_id: int) -> None:
            frontend.start_notifications.append(chat_id)
            started.set()
            await asyncio.Event().wait()

        frontend.notify_start = slow_notify_start  # type: ignore[method-assign]
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.ATTACHMENT_PLATFORMS = ()
            mock_agent.run = AsyncMock()
            await orch.on_message(1, "go")
            await asyncio.wait_for(started.wait(), timeout=2)

            assert await orch.abort(1) is True

        mock_agent.run.assert_not_called()
        assert frontend.complete_notifications == [1]
        assert 1 not in orch._in_flight
        assert not orch.is_busy(1)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestAutoCompactThreshold:
    def test_unset_is_off(self, monkeypatch) -> None:
        monkeypatch.delenv("COTF_AUTO_COMPACT_PCT", raising=False)
        assert orchestrator_mod._auto_compact_pct() == 0

    def test_reads_a_valid_percentage(self, monkeypatch) -> None:
        monkeypatch.setenv("COTF_AUTO_COMPACT_PCT", "60")
        assert orchestrator_mod._auto_compact_pct() == 60

    def test_junk_disables_rather_than_crashing_the_daemon(self, monkeypatch) -> None:
        monkeypatch.setenv("COTF_AUTO_COMPACT_PCT", "sixty")
        assert orchestrator_mod._auto_compact_pct() == 0

    def test_out_of_range_disables(self, monkeypatch) -> None:
        monkeypatch.setenv("COTF_AUTO_COMPACT_PCT", "0")
        assert orchestrator_mod._auto_compact_pct() == 0
        monkeypatch.setenv("COTF_AUTO_COMPACT_PCT", "150")
        assert orchestrator_mod._auto_compact_pct() == 0


class TestDueForCompaction:
    def test_off_by_default_however_full_the_context(self, orch: Orchestrator) -> None:
        orch._context[1] = (990_000, 1_000_000)
        assert orch._due_for_compaction(1) is False

    def test_fires_above_the_threshold(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (650_000, 1_000_000)
        assert orch._due_for_compaction(1) is True

    def test_holds_below_the_threshold(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (500_000, 1_000_000)
        assert orch._due_for_compaction(1) is False

    def test_never_fires_without_a_reading(self, orch: Orchestrator) -> None:
        """Native mode reports no context size, so the gate has nothing to read
        and auto-compaction is inert there however it is configured."""
        orch._auto_compact_pct = 1
        assert orch._due_for_compaction(1) is False

    def test_reading_is_consumed_so_it_fires_once(self, orch: Orchestrator) -> None:
        """Two messages arriving together must queue one compaction, not two —
        the second would pay a full-context pass to be told there is nothing
        left to do."""
        orch._auto_compact_pct = 60
        orch._context[1] = (650_000, 1_000_000)
        assert orch._due_for_compaction(1) is True
        assert orch._due_for_compaction(1) is False

    def test_a_zero_window_cannot_divide(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (100, 0)
        assert orch._due_for_compaction(1) is False

    def test_each_chat_is_judged_on_its_own_context(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (650_000, 1_000_000)
        orch._context[2] = (10_000, 1_000_000)
        assert orch._due_for_compaction(1) is True
        assert orch._due_for_compaction(2) is False

    def test_threshold_reloads_from_config(
        self, orch: Orchestrator, operator_settings
    ) -> None:
        operator_settings.write_text("agent:\n  auto_compact_pct: 80\n")
        orch._context[1] = (70, 100)
        assert orch._due_for_compaction(1) is False
        operator_settings.write_text("agent:\n  auto_compact_pct: 60\n")
        assert orch._due_for_compaction(1) is True


class TestOnMessageAutoCompacts:
    async def test_compaction_is_queued_ahead_of_the_message(
        self, orch: Orchestrator
    ) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (650_000, 1_000_000)
        seen: list[Turn] = []

        async def capture(chat_id: int, turn: Turn) -> None:
            seen.append(turn)

        with patch.object(orch, "_process", side_effect=capture):
            await orch.on_message(1, "and another thing")
            await orch._running[1]

        assert [(t.compact, t.text) for t in seen] == [
            (True, ""),
            (False, "and another thing"),
        ]

    async def test_no_compaction_when_under_threshold(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[1] = (10_000, 1_000_000)
        seen: list[Turn] = []

        async def capture(chat_id: int, turn: Turn) -> None:
            seen.append(turn)

        with patch.object(orch, "_process", side_effect=capture):
            await orch.on_message(1, "hi")
            await orch._running[1]

        assert [t.compact for t in seen] == [False]


class TestRunCompaction:
    async def test_reports_the_numbers_to_the_user(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        outcome = Compaction(ok=True, pre_tokens=48939, post_tokens=5162, duration=10.8)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.compact = AsyncMock(return_value=outcome)
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("", compact=True))

        mock_agent.run.assert_not_called()
        body = frontend.sent[-1][1].body
        assert "48,939" in body and "5,162" in body

    async def test_says_so_when_the_backend_cannot_compact(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        """codex has no /compact. Silence would read as a compaction that worked."""
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.compact = AsyncMock(return_value=None)
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("", compact=True))

        assert "can't compact" in frontend.sent[-1][1].body

    async def test_a_manual_compaction_clears_the_stale_reading(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        """Otherwise the next message sees the pre-compaction size and fires an
        automatic compaction straight after the manual one."""
        orch._auto_compact_pct = 60
        orch._context[1] = (650_000, 1_000_000)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.compact = AsyncMock(return_value=Compaction(ok=True))
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("", compact=True))

        assert orch._due_for_compaction(1) is False

    async def test_a_compaction_still_gets_the_live_status_lifecycle(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        """Minutes of silence on a large thread look exactly like a dead daemon,
        so the reaction and the ticking status matter more here than on a reply."""
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.compact = AsyncMock(return_value=Compaction(ok=True))
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("", compact=True))

        assert frontend.start_notifications == [1]
        assert frontend.complete_notifications == [1]


class TestContextTracking:
    async def test_records_the_reading_a_pty_turn_reports(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        response = Response(
            body="hi", context_tokens=650_000, context_window_size=1_000_000
        )
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=response)
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("hi"))

        assert orch._context[1] == (650_000, 1_000_000)

    async def test_a_native_turn_does_not_erase_a_pty_reading(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        """Zeroing it on a mode switch would make a large context look small."""
        orch._context[1] = (650_000, 1_000_000)
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="hi"))
            mock_agent.ATTACHMENT_PLATFORMS = ()
            await orch._process(1, Turn("hi"))

        assert orch._context[1] == (650_000, 1_000_000)


class TestContextIsForgottenOnSessionChange:
    """The reading is keyed by chat but describes a session, and two callers
    repoint a chat at a fresh one."""

    def test_scheduler_reset_drops_the_previous_fires_reading(
        self, orch: Orchestrator
    ) -> None:
        """The scheduler resets before every fire. A reading left over from the
        last one would queue a compaction against a session with nothing in it."""
        orch._auto_compact_pct = 60
        orch._context[7] = (650_000, 1_000_000)

        orch.reset_session(7)

        assert orch._context.get(7) is None
        assert orch._due_for_compaction(7) is False

    def test_pinning_a_session_token_drops_it_too(self, orch: Orchestrator) -> None:
        """telegram's /new mints a token for a deliberately fresh session."""
        orch._auto_compact_pct = 60
        orch._context[7] = (650_000, 1_000_000)

        orch.set_session_token(7, "20260728-120000")

        assert orch._due_for_compaction(7) is False

    def test_other_chats_keep_their_readings(self, orch: Orchestrator) -> None:
        orch._auto_compact_pct = 60
        orch._context[7] = (650_000, 1_000_000)
        orch._context[8] = (650_000, 1_000_000)

        orch.reset_session(7)

        assert orch._context.get(8) == (650_000, 1_000_000)


# ---------------------------------------------------------------------------
# Per-session egress
# ---------------------------------------------------------------------------


class TestSessionEgress:
    """Egress is per-session, not per-daemon, and the reasons are in the class
    docstring: a shared proxy would let one chat's approval authorize every other
    chat and cron, and a `/new` would inherit grants it never earned."""

    async def test_each_chat_gets_its_own_proxy(self, frontend: StubFrontend) -> None:
        manager = orchestrator_mod.SessionEgress(frontend)
        try:
            first = await manager.env_for(1, "session-a")
            second = await manager.env_for(2, "session-b")
            assert first["HTTPS_PROXY"] != second["HTTPS_PROXY"]
        finally:
            await manager.close_all()

    async def test_the_same_session_reuses_its_proxy(
        self, frontend: StubFrontend
    ) -> None:
        manager = orchestrator_mod.SessionEgress(frontend)
        try:
            first = await manager.env_for(1, "session-a")
            again = await manager.env_for(1, "session-a")
            assert first == again
        finally:
            await manager.close_all()

    async def test_a_new_session_drops_the_previous_grants(
        self, frontend: StubFrontend, caplog
    ) -> None:
        """`/new` earns a fresh proxy with a fresh grant store. Inheriting the old
        store would carry approvals across a boundary the user drew on purpose."""
        manager = orchestrator_mod.SessionEgress(frontend)
        try:
            with caplog.at_level("INFO", logger="claude_on_the_fly.orchestrator"):
                before = await manager.env_for(1, "session-a")
                after = await manager.env_for(1, "session-b")
            assert before["HTTPS_PROXY"] != after["HTTPS_PROXY"]
            assert "grants dropped" in "\n".join(r.getMessage() for r in caplog.records)
        finally:
            await manager.close_all()

    async def test_close_all_revokes_every_session(
        self, frontend: StubFrontend
    ) -> None:
        manager = orchestrator_mod.SessionEgress(frontend)
        await manager.env_for(1, "session-a")
        await manager.env_for(2, "session-b")
        await manager.close_all()
        assert manager._proxies == {}

    async def test_close_all_is_safe_with_nothing_started(
        self, frontend: StubFrontend
    ) -> None:
        await orchestrator_mod.SessionEgress(frontend).close_all()

    async def test_the_never_ask_tier_is_live_on_a_session_proxy(
        self, frontend: StubFrontend
    ) -> None:
        """Wired with bare hostnames this matched nothing, because a host subject
        is "<host>:<port>". A dead never-ask tier means a metadata endpoint gets
        offered to the operator as an ordinary choice."""
        manager = orchestrator_mod.SessionEgress(frontend)
        try:
            await manager.env_for(1, "session-a")
            _session, proxy = manager._proxies[1]
            assert proxy._approvals._policy.refuses("metadata.google.internal:443")
        finally:
            await manager.close_all()


# ---------------------------------------------------------------------------
# Turn lifecycle when egress itself fails
# ---------------------------------------------------------------------------


class TestEgressStartupFailure:
    async def test_a_proxy_that_cannot_start_still_completes_the_turn_lifecycle(
        self, frontend: StubFrontend
    ) -> None:
        """Set outside the lifecycle try, this failure escaped _process entirely:
        the in-flight slot leaked so the heartbeat reported a phantom running job
        forever, notify_complete never ran so the frontend kept its typing
        indicator, and the drain task died with turns still queued."""
        egress_manager = MagicMock()
        egress_manager.env_for = AsyncMock(side_effect=OSError("address in use"))
        orch = Orchestrator(frontend, "test", egress_manager=egress_manager)

        await orch._process(1, Turn(text="hello"))

        assert orch._in_flight == {}, "in-flight slot leaked"
        assert frontend.complete_notifications == [1], "frontend left thinking"
        # Reported to the user rather than swallowed: a turn that produced nothing
        # with no message is indistinguishable from the daemon being asleep.
        assert "address in use" in frontend.sent[-1][1].body

    async def test_the_drain_loop_survives_it(self, frontend: StubFrontend) -> None:
        """The queue must keep draining: one failed turn is not a reason to strand
        every message behind it."""
        egress_manager = MagicMock()
        egress_manager.env_for = AsyncMock(side_effect=OSError("address in use"))
        orch = Orchestrator(frontend, "test", egress_manager=egress_manager)
        await orch.on_message(1, "first")
        await orch.on_message(1, "second")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(frontend.complete_notifications) >= 2:
                break
        assert len(frontend.complete_notifications) >= 2, (
            f"drain stopped after {frontend.complete_notifications}"
        )
        await orch.shutdown()


# ---------------------------------------------------------------------------
# Sandbox startup
# ---------------------------------------------------------------------------


class TestStartSandbox:
    async def test_all_none_when_sandboxing_is_off(
        self, frontend: StubFrontend, monkeypatch
    ) -> None:
        """Zero change for anyone who has not opted in: the spawn sites see
        exactly what they saw before any of this existed."""
        monkeypatch.delenv("COTF_SANDBOX", raising=False)
        assert await orchestrator_mod._start_sandbox(frontend) == (None, None, None)

    async def test_a_command_broker_that_cannot_start_revokes_the_credentials(
        self, frontend: StubFrontend, monkeypatch, operator_settings
    ) -> None:
        """Without the teardown the credential broker stayed listening with every
        route's key loaded in memory and ANTHROPIC_BASE_URL still published, for a
        daemon that was on its way to exiting."""
        monkeypatch.setenv("COTF_SANDBOX", "jail")
        credential_broker = MagicMock()
        credential_broker.stop = AsyncMock()
        monkeypatch.setattr(
            orchestrator_mod.broker,
            "start_default_broker",
            AsyncMock(return_value=credential_broker),
        )
        command_broker = MagicMock()
        command_broker.start = AsyncMock(side_effect=OSError("port in use"))
        command_broker.stop = AsyncMock()
        monkeypatch.setattr(
            orchestrator_mod.commands, "CommandBroker", lambda *_a: command_broker
        )

        with pytest.raises(OSError, match="port in use"):
            await orchestrator_mod._start_sandbox(frontend)

        credential_broker.stop.assert_awaited_once()
        command_broker.stop.assert_awaited_once()

    async def test_a_credential_broker_that_cannot_start_needs_no_teardown(
        self, frontend: StubFrontend, monkeypatch, caplog, operator_settings
    ) -> None:
        monkeypatch.setenv("COTF_SANDBOX", "jail")
        monkeypatch.setattr(
            orchestrator_mod.broker,
            "start_default_broker",
            AsyncMock(side_effect=KeyError("keychain item not found")),
        )
        with (
            caplog.at_level("ERROR", logger="claude_on_the_fly.orchestrator"),
            pytest.raises(KeyError),
        ):
            await orchestrator_mod._start_sandbox(frontend)
        assert "revoking what already started" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_successful_start_publishes_the_shim_endpoint(
        self, frontend: StubFrontend, monkeypatch, operator_settings
    ) -> None:
        monkeypatch.setenv("COTF_SANDBOX", "jail")
        credential_broker = MagicMock()
        credential_broker.stop = AsyncMock()
        monkeypatch.setattr(
            orchestrator_mod.broker,
            "start_default_broker",
            AsyncMock(return_value=credential_broker),
        )
        command_broker = MagicMock()
        command_broker.start = AsyncMock()
        command_broker.stop = AsyncMock()
        command_broker.shimmed = ["gh"]
        command_broker.agent_env = lambda: {
            "COTF_COMMAND_ENDPOINT": "http://127.0.0.1:1"
        }
        monkeypatch.setattr(
            orchestrator_mod.commands, "CommandBroker", lambda *_a: command_broker
        )
        monkeypatch.setattr(
            orchestrator_mod.sandbox, "verify_denials", AsyncMock(return_value={})
        )

        (
            got_broker,
            egress_manager,
            got_commands,
        ) = await orchestrator_mod._start_sandbox(frontend)
        try:
            assert got_broker is credential_broker
            assert got_commands is command_broker
            assert isinstance(egress_manager, orchestrator_mod.SessionEgress)
            assert os.environ["COTF_COMMAND_ENDPOINT"] == "http://127.0.0.1:1"
        finally:
            os.environ.pop("COTF_COMMAND_ENDPOINT", None)

    async def test_start_sandbox_no_longer_seeds_the_policy_file(
        self, frontend: StubFrontend, monkeypatch, operator_settings
    ) -> None:
        """Seeding moved up to `run`, which is the only place that reaches every
        deployment shape. Asserted here so it cannot quietly move back and leave two
        callers racing to create the same file."""
        monkeypatch.setenv("COTF_SANDBOX", "jail")
        monkeypatch.setattr(
            orchestrator_mod.broker, "start_default_broker", AsyncMock()
        )
        command_broker = MagicMock()
        command_broker.start = AsyncMock()
        command_broker.shimmed = []
        command_broker.agent_env = dict
        monkeypatch.setattr(
            orchestrator_mod.commands, "CommandBroker", lambda *_a: command_broker
        )
        monkeypatch.setattr(
            orchestrator_mod.sandbox, "verify_denials", AsyncMock(return_value={})
        )
        await orchestrator_mod._start_sandbox(frontend)
        assert not operator_settings.exists()


# ---------------------------------------------------------------------------
# Startup summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("", "<unset>"),
        ("ab", "***"),
        ("abcd", "***"),
        ("xoxb-secret-value", "xo***ue"),
    ],
)
def test_redact_token(token, expected):
    assert orchestrator_mod._redact_token(token) == expected


def test_settings_summary_names_the_backend_mode_variable(
    frontend: StubFrontend, monkeypatch, caplog
):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODE", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    with caplog.at_level("INFO", logger="claude_on_the_fly.orchestrator"):
        orchestrator_mod._log_settings_summary("telegram", frontend)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent_backend   = codex" in logged
    assert "codex_mode" in logged
    assert "qwen3:8b" in logged


class TestSessionEnvIsScopedToTheTurn:
    async def test_the_session_env_is_reset_when_the_turn_ends(
        self, frontend: StubFrontend, monkeypatch
    ) -> None:
        """The proxy env is set on a ContextVar so it reaches a spawn several
        frames down without touching os.environ. Left set, the next turn in this
        task would inherit the previous session's proxy."""
        egress_manager = MagicMock()
        egress_manager.env_for = AsyncMock(return_value={"HTTPS_PROXY": "http://x:1"})
        orch = Orchestrator(frontend, "test", egress_manager=egress_manager)
        with patch.object(
            orchestrator_mod.agent, "run", AsyncMock(return_value=Response(body="ok"))
        ):
            await orch._process(1, Turn(text="hello"))
        # The ContextVar is back to its default, so the next turn cannot inherit
        # this session's proxy.
        assert orchestrator_mod.sandbox._SESSION_ENV.get() is None


class TestAbortDrainsTheQueue:
    async def test_a_queue_that_empties_under_the_drain_loop_is_not_an_error(
        self, frontend: StubFrontend
    ) -> None:
        """`empty()` and `get_nowait()` are two separate observations, so the drain
        has to tolerate the queue being emptied between them rather than raising
        into the abort path."""
        orch = Orchestrator(frontend, "test")

        class LyingQueue:
            def empty(self) -> bool:
                return False

            def get_nowait(self):
                raise asyncio.QueueEmpty

        orch._queues[1] = LyingQueue()  # type: ignore[assignment]
        assert await orch.abort(1) is False


class TestRunTeardown:
    async def test_shutdown_revokes_every_sandbox_capability(
        self, frontend: StubFrontend, monkeypatch, tmp_path
    ) -> None:
        """Each of these is a live route out of the sandbox. A daemon that exits
        without stopping them leaves a listener holding credentials behind."""
        credential_broker = MagicMock()
        credential_broker.stop = AsyncMock()
        command_broker = MagicMock()
        command_broker.stop = AsyncMock()
        session_egress = MagicMock()
        session_egress.close_all = AsyncMock()
        monkeypatch.setattr(
            orchestrator_mod,
            "_start_sandbox",
            AsyncMock(return_value=(credential_broker, session_egress, command_broker)),
        )

        stub = MagicMock()
        stub.describe = lambda: {"bot_token": "ab***yz"}
        stub.set_orchestrator = MagicMock()
        stub.stop = AsyncMock()

        async def start_then_stop(_on_message):
            # Stands in for the signal handler: run() blocks on this event.
            await asyncio.sleep(3600)

        stub.start = start_then_stop
        original_wait = asyncio.Event.wait

        async def stop_immediately(self):
            self.set()
            return await original_wait(self)

        monkeypatch.setattr(asyncio.Event, "wait", stop_immediately)
        await orchestrator_mod.run(stub, "test")

        session_egress.close_all.assert_awaited_once()
        command_broker.stop.assert_awaited_once()
        credential_broker.stop.assert_awaited_once()

    async def test_the_interruption_notices_go_out_before_the_frontend_stops(
        self, monkeypatch
    ) -> None:
        """The notice posts through the frontend's own client. Cancelling its
        listener first can tear that client down, so the notice has to be sent
        while the frontend is still fully up."""
        monkeypatch.setattr(
            orchestrator_mod,
            "_start_sandbox",
            AsyncMock(return_value=(None, None, None)),
        )
        order: list[str] = []

        stub = MagicMock()
        stub.describe = lambda: {}
        stub.set_orchestrator = MagicMock()

        async def _stop() -> None:
            order.append("frontend.stop")

        async def _notify(_chat_id, **_kw) -> None:
            order.append("notice")

        async def never_returns(_on_message):
            await asyncio.sleep(3600)

        stub.stop = _stop
        stub.notify_interrupted = _notify
        stub.start = never_returns

        captured: list[Orchestrator] = []
        real_orchestrator = orchestrator_mod.Orchestrator

        def _capture(*args, **kwargs):
            orch = real_orchestrator(*args, **kwargs)
            orch._queues[1] = asyncio.Queue()
            orch._queues[1].put_nowait(Turn("waiting"))
            captured.append(orch)
            return orch

        monkeypatch.setattr(orchestrator_mod, "Orchestrator", _capture)
        original_wait = asyncio.Event.wait

        async def stop_immediately(self):
            self.set()
            return await original_wait(self)

        monkeypatch.setattr(asyncio.Event, "wait", stop_immediately)
        await orchestrator_mod.run(stub, "test")

        assert order == ["notice", "frontend.stop"]
        # shutdown() asks again; the person is not told twice.
        await captured[0].shutdown()
        assert order.count("notice") == 1

    async def test_shutdown_also_revokes_the_approval_services(
        self, frontend: StubFrontend, monkeypatch, operator_settings
    ) -> None:
        """Each approval service is a listening socket holding a session's grants.
        Leaving one up past shutdown would keep answering for a daemon that is
        gone."""
        operator_settings.write_text("permissions:\n  mode: ask\n")
        monkeypatch.setattr(
            orchestrator_mod,
            "_start_sandbox",
            AsyncMock(return_value=(None, None, None)),
        )
        closed: list[str] = []

        class SpyPermissions(orchestrator_mod.SessionPermissions):
            async def close_all(self) -> None:
                closed.append("closed")
                await super().close_all()

        monkeypatch.setattr(orchestrator_mod, "SessionPermissions", SpyPermissions)

        stub = MagicMock()
        stub.describe = lambda: {}
        stub.set_orchestrator = MagicMock()
        stub.stop = AsyncMock()

        async def never_returns(_on_message):
            await asyncio.sleep(3600)

        stub.start = never_returns
        original_wait = asyncio.Event.wait

        async def stop_immediately(self):
            self.set()
            return await original_wait(self)

        monkeypatch.setattr(asyncio.Event, "wait", stop_immediately)
        await orchestrator_mod.run(stub, "test")
        assert closed == ["closed"]


class TestPolicyFileAtStartup:
    """`run` seeds and validates the settings file, whatever the sandbox mode."""

    @staticmethod
    async def _run_once(monkeypatch) -> None:
        monkeypatch.setattr(
            orchestrator_mod,
            "_start_sandbox",
            AsyncMock(return_value=(None, None, None)),
        )
        stub = MagicMock()
        stub.describe = lambda: {}
        stub.set_orchestrator = MagicMock()
        stub.stop = AsyncMock()

        async def never_returns(_on_message):
            await asyncio.sleep(3600)

        stub.start = never_returns
        original_wait = asyncio.Event.wait

        async def stop_immediately(self):
            self.set()
            return await original_wait(self)

        monkeypatch.setattr(asyncio.Event, "wait", stop_immediately)
        await orchestrator_mod.run(stub, "test")

    async def test_the_file_is_seeded_with_the_sandbox_off(
        self, monkeypatch, operator_settings
    ) -> None:
        """This used to be gated on `sandbox.enabled()`, which meant the shape that
        most needs the diagnostics -- sandbox off, approvals on -- was the one shape
        that never got them, and never saw the commented template either."""
        monkeypatch.delenv("COTF_SANDBOX", raising=False)
        assert not operator_settings.exists()
        await self._run_once(monkeypatch)
        assert operator_settings.is_file()


class TestConfigRestartNotice:
    """Most of the file is re-read; the rest is reported rather than ignored."""

    @staticmethod
    def _orchestrator(frontend: StubFrontend) -> orchestrator_mod.Orchestrator:
        return orchestrator_mod.Orchestrator(frontend, "test")

    async def test_a_restart_required_edit_is_named_in_the_conversation(
        self, frontend: StubFrontend, operator_settings
    ) -> None:
        """Reported where the operator already is, because that is where they will be
        right after saving the file."""
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        settings.check_operator_settings()
        operator_settings.write_text("permissions:\n  mode: ask\n")

        await self._orchestrator(frontend)._report_config_restarts(7)

        assert len(frontend.sent) == 1
        body = frontend.sent[0][1].body
        assert "permissions.mode" in body
        assert "needs a restart" in body
        assert settings.FILENAME in body

    async def test_a_live_edit_is_not_reported(
        self, frontend: StubFrontend, operator_settings
    ) -> None:
        """An allowlist edit takes effect on the next read, so telling anyone to
        restart for it would be a lie that trains them to ignore the notice."""
        operator_settings.write_text("egress:\n  allow: [a.example]\n")
        settings.check_operator_settings()
        operator_settings.write_text("egress:\n  allow: [a.example, b.example]\n")

        await self._orchestrator(frontend)._report_config_restarts(7)

        assert frontend.sent == []

    async def test_the_same_change_is_reported_once(
        self, frontend: StubFrontend, operator_settings
    ) -> None:
        """check_reload compares against the startup baseline, so it keeps returning
        the same answer until a restart. Sending it every turn is noise."""
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        settings.check_operator_settings()
        operator_settings.write_text("permissions:\n  mode: ask\n")

        orch = self._orchestrator(frontend)
        for _ in range(3):
            await orch._report_config_restarts(7)

        assert len(frontend.sent) == 1

    async def test_a_second_change_is_reported_again(
        self, frontend: StubFrontend, operator_settings
    ) -> None:
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        settings.check_operator_settings()
        orch = self._orchestrator(frontend)

        operator_settings.write_text("permissions:\n  mode: ask\n")
        await orch._report_config_restarts(7)
        operator_settings.write_text(
            "permissions:\n  mode: ask\ncommands:\n  tools: []\n"
        )
        await orch._report_config_restarts(7)

        assert len(frontend.sent) == 2
        assert "commands" in frontend.sent[1][1].body

    async def test_reverting_the_edit_clears_the_notice(
        self, frontend: StubFrontend, operator_settings
    ) -> None:
        """So the next real change is reported, rather than swallowed by a flag that
        was never lowered."""
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        settings.check_operator_settings()
        orch = self._orchestrator(frontend)

        operator_settings.write_text("permissions:\n  mode: ask\n")
        await orch._report_config_restarts(7)
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        await orch._report_config_restarts(7)
        operator_settings.write_text("permissions:\n  mode: ask\n")
        await orch._report_config_restarts(7)

        assert len(frontend.sent) == 2

    async def test_a_frontend_failure_does_not_kill_the_turn(
        self, frontend: StubFrontend, operator_settings, caplog
    ) -> None:
        """A missed notice is a log line. An exception here would take down the drain
        task with turns still queued."""
        operator_settings.write_text('permissions:\n  mode: "off"\n')
        settings.check_operator_settings()
        operator_settings.write_text("permissions:\n  mode: ask\n")

        orch = self._orchestrator(frontend)
        frontend.send = AsyncMock(side_effect=RuntimeError("slack is down"))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.orchestrator"):
            await orch._report_config_restarts(7)
        assert "could not report" in caplog.text


def test_settings_summary_includes_the_frontends_own_fields(monkeypatch, caplog):
    """Secrets are redacted by the frontend before they get here, so this only has
    to print what it is handed."""
    stub = MagicMock()
    stub.describe = lambda: {"bot_token": "xo***ue", "allowed_user_id": "42"}
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    with caplog.at_level("INFO", logger="claude_on_the_fly.orchestrator"):
        orchestrator_mod._log_settings_summary("telegram", stub)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "bot_token" in logged and "xo***ue" in logged
    assert "allowed_user_id" in logged


class TestFrontendApprovalDefault:
    async def test_a_frontend_without_an_approval_ui_denies(
        self, frontend: StubFrontend
    ) -> None:
        """The default must deny, so a frontend that has not implemented an approval
        UI behaves exactly like one with no approval channel rather than silently
        granting every host the agent asks for."""
        from claude_on_the_fly.approvals import ApprovalRequest

        request = ApprovalRequest(kind="host", subject="evil.example:443", detail="d")
        assert await frontend.ask_approval(request, chat_id=1) is False

    async def test_the_default_queued_notice_is_a_plain_message(self) -> None:
        """Frontends with cheaper signals (an emoji reaction) override this, but the
        base has to say something: a queued message that looks ignored gets resent."""

        class MinimalFrontend(StubFrontend):
            pass

        # StubFrontend overrides notify_queued for assertions, so call the base
        # implementation directly to exercise the protocol default.
        frontend = MinimalFrontend()
        await Frontend.notify_queued(frontend, 1, 3)
        assert frontend.sent[0][0] == 1
        assert "Queued (3 pending)" in frontend.sent[0][1].body

    async def test_the_default_interruption_notice_is_a_plain_message(self) -> None:
        """The fallback for a frontend with no state to show. Slack overrides it
        with a reaction instead."""
        frontend = StubFrontend()
        await Frontend.notify_interrupted(frontend, 4, running=True, queued=1)

        chat_id, response = frontend.sent[0]
        assert chat_id == 4
        assert "Restarting" in response.body

    def test_the_fallback_notice_reads_like_a_person_in_passing(self) -> None:
        """The fallback, for a turn with no reaction to put a state on. Everything
        comes back, so it asks for nothing and reports nothing the reader can see."""
        one = interrupted_notice(running=True, queued=0)
        several = interrupted_notice(running=False, queued=3)

        assert one == "Restarting. I'll get back to this in a moment."
        assert several == "Restarting. I'll get back to these in a moment."

    def test_one_pending_turn_is_not_pluralised(self) -> None:
        assert "back to this" in interrupted_notice(running=False, queued=1)

    async def test_the_default_resume_says_nothing(self) -> None:
        """Invisible by default: the turn's own reaction and reply are the signal,
        so a message here would be the daemon narrating itself."""
        frontend = StubFrontend()

        await Frontend.notify_resumed(frontend, 3, 2)

        assert frontend.sent == []

    async def test_the_default_nudge_carries_the_prompt_back(self) -> None:
        """Reached only for a turn the daemon stopped retrying, so the person has
        to be able to send it again themselves."""
        frontend = StubFrontend()
        await Frontend.notify_nudge(frontend, 3, "audit the release posts")

        _chat_id, response = frontend.sent[0]
        assert "audit the release posts" in response.body
        assert "Send it again" in response.body
        assert "keep failing" in response.body

    async def test_the_nudge_shows_what_the_person_typed(self) -> None:
        """Not the prompt scaffolding wrapped around it."""
        marked = f"{agent_mod.sender_marker('U1', 'hoss')} Wait ten seconds."
        frontend = StubFrontend()

        await Frontend.notify_nudge(frontend, 3, marked)

        body = frontend.sent[0][1].body
        assert "Wait ten seconds." in body
        assert "from-id" not in body

    def test_the_default_route_hooks_are_inert(self) -> None:
        """A frontend whose chat id is already an address needs neither."""
        frontend = StubFrontend()

        assert Frontend.route_for(frontend, 1) == {}
        assert Frontend.restore_route(frontend, 1, {"channel": "C1"}) is None


# --- per-session approval services ---


async def test_session_permissions_is_inert_when_approvals_are_off(operator_settings):
    """Off is the default, so this is the path almost every deployment takes: no
    service, no env, nothing added to the spawn."""
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    assert await manager.env_for(1, "sess-a", Path("/tmp/ws")) == {}


async def test_session_permissions_hands_the_agent_its_endpoint(operator_settings):
    from claude_on_the_fly import cotf_approve

    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        env = await manager.env_for(7, "sess-a", Path("/tmp/ws"))
        url = env[cotf_approve.ENDPOINT_ENV]
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith(permissions_mod.DECIDE_PATH)
        # Same session, same service: a new port per turn would drop grants mid-run.
        assert await manager.env_for(7, "sess-a", Path("/tmp/ws")) == env
    finally:
        await manager.close_all()


async def test_existing_session_reloads_approval_timing(operator_settings):
    from claude_on_the_fly import cotf_approve

    operator_settings.write_text(
        "permissions:\n  mode: ask\n  ttl_seconds: 60\n  timeout_seconds: 30\n"
    )
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        first = await manager.env_for(7, "sess-a", Path("/tmp/ws"))
        service = manager._services[7][1]
        operator_settings.write_text(
            "permissions:\n  mode: ask\n  ttl_seconds: 120\n  timeout_seconds: 45\n"
        )
        second = await manager.env_for(7, "sess-a", Path("/tmp/ws"))
        assert service.ttl_seconds == 120
        assert service.broker.timeout_seconds == 45
        assert float(second[cotf_approve.REQUEST_TIMEOUT_ENV]) > 45
        assert first[cotf_approve.ENDPOINT_ENV] == second[cotf_approve.ENDPOINT_ENV]
    finally:
        await manager.close_all()


async def test_the_service_can_reach_the_conversation_it_belongs_to(operator_settings):
    """A gate that cannot function has to say so where the operator is looking. The
    ERROR alone left a stuck turn looking like a slow one."""
    operator_settings.write_text("permissions:\n  mode: ask\n")
    frontend = StubFrontend()
    manager = orchestrator_mod.SessionPermissions(frontend)
    try:
        await manager.env_for(11, "sess-a", Path("/tmp/ws"))
        service = manager._services[11][1]
        assert service.notify is not None
        await service.notify(permissions_mod.UNREADABLE_DIALOG_NOTICE)
    finally:
        await manager.close_all()
    # The session's own chat, not a fallback: the notice belongs with the work.
    assert [(chat_id, r.body) for chat_id, r in frontend.sent] == [
        (11, permissions_mod.UNREADABLE_DIALOG_NOTICE)
    ]


async def test_a_new_session_drops_the_previous_grants(operator_settings, caplog):
    """/new has to mean what it says. Reusing the service would carry every
    approval from the conversation the operator just abandoned."""
    from claude_on_the_fly import cotf_approve

    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        first = await manager.env_for(9, "sess-a", Path("/tmp/ws"))
        with caplog.at_level("INFO", logger="claude_on_the_fly.orchestrator"):
            second = await manager.env_for(9, "sess-b", Path("/tmp/ws"))
        assert first[cotf_approve.ENDPOINT_ENV] != second[cotf_approve.ENDPOINT_ENV]
        assert "grants dropped" in caplog.text
    finally:
        await manager.close_all()


async def test_each_chat_gets_its_own_grant_store(operator_settings):
    """A grant approved in one conversation must not authorise another. The port is
    the only label a request carries, so one service per chat is what confines it."""
    from claude_on_the_fly import cotf_approve

    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        one = await manager.env_for(1, "s", Path("/tmp/ws"))
        two = await manager.env_for(2, "s", Path("/tmp/ws"))
        assert one[cotf_approve.ENDPOINT_ENV] != two[cotf_approve.ENDPOINT_ENV]
    finally:
        await manager.close_all()


async def test_check_turn_is_silent_for_a_chat_with_no_service(operator_settings):
    """A chat that never started a service cannot have been gated, and there is
    nothing to compare against."""
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    manager.check_turn(404, Response(body="x", tool_counts={"Bash": 2}), "codex")


async def test_check_turn_reports_a_turn_that_never_reached_the_gate(
    operator_settings, caplog
):
    """The whole point of the guard: codex treats an untrusted hook as no opinion
    and runs the command, so an ungated turn is otherwise indistinguishable from a
    supervised one."""
    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        await manager.env_for(3, "s", Path("/tmp/ws"))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
            manager.check_turn(3, Response(body="x", tool_counts={"Bash": 2}), "codex")
        assert "ran UNSUPERVISED" in caplog.text
    finally:
        await manager.close_all()


async def test_the_spawn_env_carries_both_egress_and_approval_routing(
    operator_settings, monkeypatch, tmp_path
):
    """Both managers publish into the same per-session ContextVar. An earlier
    revision assigned rather than merged, so whichever ran second silently dropped
    the other's routing."""
    from claude_on_the_fly import cotf_approve, sandbox

    operator_settings.write_text("permissions:\n  mode: ask\n")
    frontend = StubFrontend()
    egress_manager = MagicMock()
    egress_manager.env_for = AsyncMock(
        return_value={"HTTPS_PROXY": "http://127.0.0.1:1"}
    )
    permissions_manager = orchestrator_mod.SessionPermissions(frontend)
    orch = Orchestrator(
        frontend,
        "test",
        egress_manager=egress_manager,
        permissions_manager=permissions_manager,
    )
    seen: dict[str, str] = {}

    async def fake_run(*_args, **_kwargs):
        current = sandbox._SESSION_ENV.get() or {}
        seen.update(current)
        return Response(body="done")

    monkeypatch.setattr(orchestrator_mod.agent, "run", fake_run)
    monkeypatch.setattr(orch, "_workspace_for", lambda _chat: tmp_path, raising=False)
    try:
        await orch._process(1, Turn(text="hi"))
    finally:
        await permissions_manager.close_all()
    assert seen.get("HTTPS_PROXY") == "http://127.0.0.1:1"
    assert cotf_approve.ENDPOINT_ENV in seen


async def test_start_sandbox_writes_the_approval_shim_when_approvals_are_on(
    monkeypatch, operator_settings
):
    """The shim and its MCP config are rewritten every startup so the interpreter
    path cannot go stale across a venv move or an upgrade, and so a wheel build
    never has to carry an exec bit."""
    from claude_on_the_fly import permissions as perms

    operator_settings.write_text("permissions:\n  mode: ask\n")
    monkeypatch.setenv("COTF_SANDBOX", "env")
    credential_broker = MagicMock()
    credential_broker.stop = AsyncMock()
    monkeypatch.setattr(
        orchestrator_mod.broker,
        "start_default_broker",
        AsyncMock(return_value=credential_broker),
    )
    command_broker = MagicMock()
    command_broker.start = AsyncMock()
    command_broker.stop = AsyncMock()
    command_broker.shimmed = []
    command_broker.agent_env = lambda: {}
    monkeypatch.setattr(
        orchestrator_mod.commands, "CommandBroker", lambda *_a: command_broker
    )
    monkeypatch.setattr(
        orchestrator_mod.sandbox, "verify_denials", AsyncMock(return_value={})
    )
    written: list[str] = []
    monkeypatch.setattr(perms, "write_shim", lambda: written.append("shim"))
    monkeypatch.setattr(perms, "write_mcp_config", lambda: written.append("config"))

    await orchestrator_mod._start_sandbox(StubFrontend())
    assert written == ["shim", "config"]


async def test_start_sandbox_writes_approval_artifacts_with_sandbox_off(
    monkeypatch, operator_settings
):
    from claude_on_the_fly import permissions as perms

    operator_settings.write_text("permissions:\n  mode: ask\n")
    monkeypatch.setenv("COTF_SANDBOX", "off")
    written: list[str] = []
    monkeypatch.setattr(perms, "write_shim", lambda: written.append("shim"))
    monkeypatch.setattr(perms, "write_mcp_config", lambda: written.append("mcp"))
    monkeypatch.setattr(perms, "write_pty_settings", lambda: written.append("pty"))

    assert await orchestrator_mod._start_sandbox(StubFrontend()) == (None, None, None)
    assert written == ["shim", "mcp", "pty"]


async def test_start_sandbox_writes_no_shim_when_approvals_are_off(
    monkeypatch, operator_settings
):
    from claude_on_the_fly import permissions as perms

    monkeypatch.setenv("COTF_SANDBOX", "env")
    credential_broker = MagicMock()
    credential_broker.stop = AsyncMock()
    monkeypatch.setattr(
        orchestrator_mod.broker,
        "start_default_broker",
        AsyncMock(return_value=credential_broker),
    )
    command_broker = MagicMock()
    command_broker.start = AsyncMock()
    command_broker.stop = AsyncMock()
    command_broker.shimmed = []
    command_broker.agent_env = lambda: {}
    monkeypatch.setattr(
        orchestrator_mod.commands, "CommandBroker", lambda *_a: command_broker
    )
    monkeypatch.setattr(
        orchestrator_mod.sandbox, "verify_denials", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        perms, "write_shim", lambda: pytest.fail("wrote a shim with approvals off")
    )
    await orchestrator_mod._start_sandbox(StubFrontend())


async def test_the_guard_catches_a_gate_that_breaks_after_working_once(
    operator_settings, caplog
):
    """The bug this replaced: comparing the service's lifetime total meant one early
    request bought a permanent pass. A gate that worked on turn one and then
    detached would never be reported again -- and that is the case most worth
    catching, since a gate that never worked at all gets noticed on the first turn.
    """

    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        await manager.env_for(5, "s", Path("/tmp/ws"))
        service = manager._services[5][1]

        # Turn one: the gate is attached and answers a question.
        service.requests_seen = 1
        caplog.clear()
        with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
            manager.check_turn(5, Response(body="a", tool_counts={"Bash": 1}), "codex")
        assert "ran UNSUPERVISED" not in caplog.text, (
            "a working turn must not be reported"
        )

        # Turn two: tools ran, the gate was never asked. Under the old lifetime
        # comparison this passed, because the total was still 1.
        caplog.clear()
        with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
            manager.check_turn(5, Response(body="b", tool_counts={"Bash": 4}), "codex")
        assert "ran UNSUPERVISED" in caplog.text
    finally:
        await manager.close_all()


async def test_a_new_session_resets_the_guards_baseline(operator_settings, caplog):
    """The replacement service counts from zero, so a stale baseline would make the
    first turn's delta negative and silently pass."""
    operator_settings.write_text("permissions:\n  mode: ask\n")
    manager = orchestrator_mod.SessionPermissions(StubFrontend())
    try:
        await manager.env_for(6, "first", Path("/tmp/ws"))
        manager._services[6][1].requests_seen = 7
        manager.check_turn(6, Response(body="a", tool_counts={"Bash": 1}), "codex")

        await manager.env_for(6, "second", Path("/tmp/ws"))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
            manager.check_turn(6, Response(body="b", tool_counts={"Bash": 2}), "codex")
        assert "ran UNSUPERVISED" in caplog.text
    finally:
        await manager.close_all()


class TestSuggestionsParsing:
    def test_no_block_leaves_body_untouched(self):
        body, labels = _extract_suggestions("plain reply")
        assert body == "plain reply"
        assert labels == []

    def test_block_is_stripped_and_parsed(self):
        body, labels = _extract_suggestions(
            'a\n\n<suggestions>["x?", "y?"]</suggestions>'
        )
        assert body == "a"
        assert labels == ["x?", "y?"]

    def test_block_only_reply_gets_placeholder_and_drops_labels(self, caplog):
        with caplog.at_level("WARNING", logger="claude_on_the_fly.orchestrator"):
            body, labels = _extract_suggestions('<suggestions>["x?"]</suggestions>')
        assert body == "No response"
        assert labels == []
        assert "reply body empty" in "\n".join(r.getMessage() for r in caplog.records)

    def test_code_fenced_block_is_parsed(self):
        assert _parse_suggestion_block('```json\n["a?", "b?"]\n```') == ["a?", "b?"]

    def test_markdown_list_fallback(self):
        assert _parse_suggestion_block("- first?\n* second?") == ["first?", "second?"]

    def test_non_string_items_are_filtered(self):
        assert _parse_suggestion_block('[1, "ok?", {"x": 1}]') == ["ok?"]

    def test_non_list_payloads_yield_nothing(self):
        assert _parse_suggestion_block('{"x": 1}') == []
        assert _parse_suggestion_block('"just a string"') == []

    def test_garbage_yields_nothing(self):
        assert _parse_suggestion_block("whatever") == []

    def test_caps_at_five(self):
        labels = [f"q{i}?" for i in range(8)]
        assert _parse_suggestion_block(json.dumps(labels)) == labels[:5]

    def test_overlong_labels_are_truncated(self):
        # 75, not 80: Slack's block-kit button text hard cap is 75 characters,
        # and a longer label would reject the whole message.
        long = "x" * 200
        assert _parse_suggestion_block(f'["{long}?"]') == ["x" * 75]

    def test_single_quoted_json_is_parsed(self):
        # LLMs emit single-quoted lists despite the template; the parser
        # accepts them as Python literals rather than dropping the questions.
        assert _parse_suggestion_block("['ask a?', 'ask b?']") == [
            "ask a?",
            "ask b?",
        ]

    def test_all_blocks_are_stripped_and_the_last_wins(self):
        body, labels = _extract_suggestions(
            'a <suggestions>["x?"]</suggestions> b <suggestions>["y?"]</suggestions>'
        )
        assert body == "a  b"  # the two gaps join; only the ends are trimmed
        assert labels == ["y?"]

    def test_fenced_block_leaves_no_dangling_fence(self):
        body, labels = _extract_suggestions(
            'answer\n```json\n<suggestions>["x?"]</suggestions>\n```'
        )
        assert body == "answer"
        assert labels == ["x?"]


# ---------------------------------------------------------------------------
# Pending-turn journal: recording, and resuming after a stop
# ---------------------------------------------------------------------------


class RoutingFrontend(StubFrontend):
    """A frontend whose chat id is not enough to reach the conversation, like
    Slack's. Records what it was asked to restore."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[int, dict] = {}
        self.restored: list[tuple[int, dict]] = []
        self.resumed: list[tuple[int, int]] = []
        self.nudged: list[tuple[int, str]] = []

    def route_for(self, chat_id: int) -> dict:
        return self.routes.get(chat_id, {})

    def restore_route(self, chat_id: int, route: dict) -> None:
        self.restored.append((chat_id, route))

    async def notify_resumed(self, chat_id: int, count: int) -> None:
        self.resumed.append((chat_id, count))

    async def notify_nudge(self, chat_id: int, text: str) -> None:
        self.nudged.append((chat_id, text))


@pytest.fixture
def routing_frontend() -> RoutingFrontend:
    return RoutingFrontend()


@pytest.fixture
def journaled(routing_frontend, event_log):
    """An orchestrator over a routing frontend. `journal_in_tmp` already points
    DATA_DIR, and so the journal, at this test's tmp_path."""
    return Orchestrator(routing_frontend, "test", event_log=event_log)


async def _settle(orch: Orchestrator) -> None:
    """Wait out the drain tasks an enqueue started.

    A test that leaves one pending pays for it in pytest's unraisable-exception
    sweep, and asserting on the journal before the drain has finished is a race
    rather than a check.
    """
    for _ in range(200):
        tasks = [task for task in orch._running.values() if not task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


class TestJournalingOnTheWayIn:
    async def test_an_accepted_message_is_recorded_before_it_can_run(
        self, journaled, routing_frontend
    ) -> None:
        """Written when the turn is accepted, not at shutdown. A record made at
        shutdown covers a clean SIGTERM and nothing else."""
        routing_frontend.routes[4] = {"channel": "C1", "thread_ts": "1.0"}
        with patch.object(journaled, "_drain", AsyncMock()):
            await journaled.on_message(4, "please do it")

        replay, nudge = journaled._journal.take()
        assert nudge == []
        assert [(t.chat_id, t.text) for t in replay] == [(4, "please do it")]
        assert replay[0].route == {"channel": "C1", "thread_ts": "1.0"}

    async def test_the_session_token_rides_along(
        self, journaled, routing_frontend
    ) -> None:
        """Without it a replay resumes a different conversation than the person
        was in, and a different workspace."""
        journaled.set_session_token(4, "tok-9")
        with patch.object(journaled, "_drain", AsyncMock()):
            await journaled.on_message(4, "hi")

        replay, _ = journaled._journal.take()
        assert replay[0].session == "tok-9"

    async def test_a_compaction_is_not_journaled(self, journaled) -> None:
        """Daemon maintenance with no text: replaying it would answer nobody and
        offering it back would show an empty prompt."""
        with patch.object(journaled, "_drain", AsyncMock()):
            await journaled.on_compact(4)

        assert journaled._journal.take() == ([], [])

    async def test_a_finished_turn_leaves_no_record(self, journaled, tmp_path) -> None:
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="done"))
            await journaled.on_message(4, "hi")
            await _settle(journaled)

        assert journaled._journal.take() == ([], [])

    async def test_a_failed_turn_leaves_no_record(self, journaled, tmp_path) -> None:
        """The person saw an error, so nothing is owed. Replaying it would repeat
        whatever the failure already did."""
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await journaled.on_message(4, "hi")
            await _settle(journaled)

        assert journaled._journal.take() == ([], [])

    async def test_a_cancelled_turn_comes_back_marked_as_already_started(
        self, journaled, tmp_path
    ) -> None:
        """Exactly the case recovery exists for. It resumes, and the phase is what
        tells the resumed turn that some of the work may already have happened."""
        gate = asyncio.Event()

        async def hang(*_a, **_kw) -> Response:
            await gate.wait()
            return Response(body="never")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=hang)
            await journaled.on_message(4, "long one")
            for _ in range(50):
                if journaled._in_flight:
                    break
                await asyncio.sleep(0.01)
            await journaled.shutdown()

        replay, nudge = journaled._journal.take()
        assert nudge == []
        assert [(t.text, t.phase) for t in replay] == [
            ("long one", turns_mod.DISPATCHED)
        ]

    async def test_stopping_a_turn_on_purpose_drops_its_record(
        self, journaled, tmp_path
    ) -> None:
        """`$stop` is not pending work. Without this it would come back to haunt
        the person after a restart."""
        gate = asyncio.Event()

        async def hang(*_a, **_kw) -> Response:
            await gate.wait()
            return Response(body="never")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=hang)
            await journaled.on_message(4, "long one")
            for _ in range(50):
                if journaled._in_flight:
                    break
                await asyncio.sleep(0.01)
            assert await journaled.abort(4) is True

        assert journaled._journal.take() == ([], [])

    async def test_aborting_drops_the_records_of_queued_turns_too(
        self, journaled, tmp_path
    ) -> None:
        gate = asyncio.Event()

        async def hang(*_a, **_kw) -> Response:
            await gate.wait()
            return Response(body="never")

        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=hang)
            await journaled.on_message(4, "running")
            for _ in range(50):
                if journaled._in_flight:
                    break
                await asyncio.sleep(0.01)
            await journaled.on_message(4, "queued behind it")
            await journaled.abort(4)

        assert journaled._journal.take() == ([], [])


class TestResumingPendingTurns:
    async def test_nothing_pending_does_nothing(self, journaled) -> None:
        assert await journaled.resume_pending() == (0, 0)

    async def test_a_queued_turn_is_replayed_and_announced(
        self, journaled, routing_frontend, tmp_path
    ) -> None:
        journaled._journal.record(
            PendingTurn(
                chat_id=4,
                text="unanswered",
                route={"channel": "C1"},
                turn_id="t-1",
                recorded_at=time.time(),
            )
        )
        with (
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="answered"))
            replayed, nudged = await journaled.resume_pending()
            await _settle(journaled)

        assert (replayed, nudged) == (1, 0)
        # The route is restored before the turn runs, or the reply has nowhere to
        # go and no reaction can land on the message that asked.
        assert routing_frontend.restored == [(4, {"channel": "C1"})]
        assert [body.body for _chat, body in routing_frontend.sent] == ["answered"]

    async def test_the_journaled_session_is_restored_before_the_replay(
        self, journaled, tmp_path
    ) -> None:
        journaled._journal.record(
            PendingTurn(
                chat_id=4,
                text="x",
                session="tok-9",
                turn_id="t-1",
                recorded_at=time.time(),
            )
        )
        with patch.object(journaled, "_enqueue", AsyncMock()):
            await journaled.resume_pending()

        assert journaled._session_counters[4] == "tok-9"

    async def test_a_turn_that_had_started_resumes_with_a_warning_to_the_agent(
        self, journaled, routing_frontend
    ) -> None:
        """It comes back, because somebody asked for the work. The note is what
        keeps it from silently repeating a push or a message it already sent."""
        journaled._journal.record(
            PendingTurn(
                chat_id=4,
                text="half-done work",
                phase=turns_mod.DISPATCHED,
                turn_id="t-1",
                recorded_at=time.time(),
            )
        )
        with patch.object(journaled, "_enqueue", AsyncMock()) as enqueue:
            replayed, nudged = await journaled.resume_pending()

        assert (replayed, nudged) == (1, 0)
        assert routing_frontend.nudged == []
        prompt = enqueue.await_args.args[1].text
        assert prompt.endswith("half-done work")
        assert "cotf-resume" in prompt
        assert "before repeating anything that writes" in prompt

    async def test_a_never_started_turn_resumes_verbatim(self, journaled) -> None:
        """No note: nothing ran, so there is nothing to be careful about."""
        journaled._journal.record(
            PendingTurn(
                chat_id=4, text="just asked", turn_id="t-1", recorded_at=time.time()
            )
        )
        with patch.object(journaled, "_enqueue", AsyncMock()) as enqueue:
            await journaled.resume_pending()

        assert enqueue.await_args.args[1].text == "just asked"

    async def test_a_resume_says_nothing_in_the_chat(
        self, journaled, routing_frontend
    ) -> None:
        """Invisible on purpose: the turn's own reaction and its reply are what
        tell the person it came back."""
        journaled._journal.record(
            PendingTurn(chat_id=4, text="x", turn_id="t-1", recorded_at=time.time())
        )
        with patch.object(journaled, "_enqueue", AsyncMock()):
            await journaled.resume_pending()

        assert routing_frontend.sent == []

    async def test_a_turn_at_the_replay_limit_is_offered_back(
        self, journaled, routing_frontend
    ) -> None:
        journaled._journal.record(
            PendingTurn(
                chat_id=4,
                text="the one that keeps breaking",
                turn_id="t-1",
                recorded_at=time.time(),
                replays=turns_mod.MAX_REPLAYS,
            )
        )
        with patch.object(journaled, "_enqueue", AsyncMock()) as enqueue:
            replayed, nudged = await journaled.resume_pending()

        assert (replayed, nudged) == (0, 1)
        enqueue.assert_not_called()
        assert routing_frontend.nudged == [(4, "the one that keeps breaking")]

    async def test_several_chats_are_resumed_independently(
        self, journaled, routing_frontend
    ) -> None:
        for chat_id, turn_id in ((1, "t-1"), (2, "t-2"), (1, "t-3")):
            journaled._journal.record(
                PendingTurn(
                    chat_id=chat_id,
                    text=f"msg-{turn_id}",
                    route={"message_ts": turn_id},
                    turn_id=turn_id,
                    recorded_at=time.time(),
                )
            )
        with patch.object(journaled, "_enqueue", AsyncMock()) as enqueue:
            replayed, _ = await journaled.resume_pending()

        assert replayed == 3
        assert sorted(routing_frontend.resumed) == [(1, 2), (2, 1)]
        assert enqueue.await_count == 3
        # Every entry's route, not just the first per chat: a route carries the
        # message that asked, and each turn has its own.
        assert routing_frontend.restored == [
            (1, {"message_ts": "t-1"}),
            (1, {"message_ts": "t-3"}),
            (2, {"message_ts": "t-2"}),
        ]

    async def test_the_replay_counter_is_carried_forward_so_a_poison_turn_parks(
        self, journaled, routing_frontend
    ) -> None:
        """Re-recording a replayed turn must not reset its count, or a turn that
        kills the daemon is replayed at every start for ever."""
        journaled._journal.record(
            PendingTurn(
                chat_id=4, text="x", turn_id="t-1", recorded_at=time.time(), replays=1
            )
        )
        with patch.object(journaled, "_drain", AsyncMock()):
            await journaled.resume_pending()

        # Re-recorded at the cap rather than back at zero, so the next start
        # parks it instead of replaying it a third time.
        replay, nudge = journaled._journal.take()
        assert replay == []
        assert [t.replays for t in nudge] == [turns_mod.MAX_REPLAYS]

    async def test_a_frontend_that_cannot_announce_still_replays(
        self, journaled, routing_frontend, caplog
    ) -> None:
        """The work matters more than the announcement of it."""
        journaled._journal.record(
            PendingTurn(chat_id=4, text="x", turn_id="t-1", recorded_at=time.time())
        )

        async def boom(*_a, **_kw) -> None:
            raise RuntimeError("channel_not_found")

        routing_frontend.notify_resumed = boom  # type: ignore[method-assign]
        with (
            patch.object(journaled, "_enqueue", AsyncMock()) as enqueue,
            caplog.at_level("ERROR", logger="claude_on_the_fly.orchestrator"),
        ):
            await journaled.resume_pending()

        enqueue.assert_awaited_once()
        assert "could not tell" in caplog.text

    async def test_a_frontend_that_cannot_nudge_is_logged(
        self, journaled, routing_frontend, caplog
    ) -> None:
        """A parked turn, since that is the only thing nudged now."""
        journaled._journal.record(
            PendingTurn(
                chat_id=4,
                text="x",
                turn_id="t-1",
                recorded_at=time.time(),
                replays=turns_mod.MAX_REPLAYS,
            )
        )

        async def boom(*_a, **_kw) -> None:
            raise RuntimeError("channel_not_found")

        routing_frontend.notify_nudge = boom  # type: ignore[method-assign]
        with (
            # Patched even though nothing should be replayed here: an unpatched
            # replay spawns a real agent CLI, which is a hang rather than a
            # failure, and that is a bad way to learn the routing changed.
            patch.object(journaled, "_enqueue", AsyncMock()),
            caplog.at_level("ERROR", logger="claude_on_the_fly.orchestrator"),
        ):
            replayed, nudged = await journaled.resume_pending()

        assert (replayed, nudged) == (0, 1)
        assert "could not offer" in caplog.text
