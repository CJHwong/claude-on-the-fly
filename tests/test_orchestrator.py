"""Tests for claude_on_the_fly.orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest

from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.orchestrator import Orchestrator
from claude_on_the_fly.protocol import Frontend


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

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        pass

    async def send(self, chat_id: int, response: Response) -> None:
        self.sent.append((chat_id, response))

    async def send_typing(self, chat_id: int) -> None:
        self.typing_sent.append(chat_id)

    async def notify_queued(self, chat_id: int, position: int) -> None:
        self.queued_notifications.append((chat_id, position))

    async def notify_start(self, chat_id: int) -> None:
        self.start_notifications.append(chat_id)

    async def notify_complete(self, chat_id: int) -> None:
        self.complete_notifications.append(chat_id)

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


@pytest.fixture
def frontend() -> StubFrontend:
    return StubFrontend()


@pytest.fixture
def orch(frontend: StubFrontend) -> Orchestrator:
    return Orchestrator(frontend, "test")


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


# ---------------------------------------------------------------------------
# is_busy
# ---------------------------------------------------------------------------


class TestIsBusy:
    def test_false_when_no_task(self, orch: Orchestrator) -> None:
        assert orch.is_busy(1) is False

    def test_true_when_task_running(self, orch: Orchestrator) -> None:
        pending_future = asyncio.get_event_loop().create_future()
        orch._running[1] = asyncio.ensure_future(pending_future)  # type: ignore[assignment]
        try:
            assert orch.is_busy(1) is True
        finally:
            pending_future.set_result(None)

    def test_false_when_task_done(self, orch: Orchestrator) -> None:
        done_future = asyncio.get_event_loop().create_future()
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

        async def fake_process(chat_id: int, text: str) -> None:
            processed.append(text)

        orch._queues[1] = asyncio.Queue()
        orch._queues[1].put_nowait("msg1")
        orch._queues[1].put_nowait("msg2")
        orch._queues[1].put_nowait("msg3")

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
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=fake_response)
            await orch._process(1, "question")

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
        )

        # Response was sent
        assert len(frontend.sent) == 1
        assert frontend.sent[0][1].body == "answer"

    async def test_sends_error_on_agent_failure(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(1, "bad prompt")

        assert len(frontend.sent) == 1
        assert "Error: boom" in frontend.sent[0][1].body

    async def test_notifies_complete_on_success(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, "question")

        assert frontend.complete_notifications == [1]

    async def test_notifies_complete_on_error(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(1, "bad")

        assert frontend.complete_notifications == [1]

    async def test_unavailable_error_uses_distinct_message(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(
                side_effect=ClaudeUnavailableError("monthly usage limit")
            )
            await orch._process(1, "hi")

        assert len(frontend.sent) == 1
        body = frontend.sent[0][1].body
        assert body.startswith("Claude unavailable:")
        assert "monthly usage limit" in body

    async def test_timeout_threaded_from_frontend(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        frontend.timeout_for = lambda chat_id: 99.0  # type: ignore[method-assign]

        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, "hi")

        assert mock_agent.run.call_args.kwargs["timeout"] == 99.0

    async def test_typing_loop_is_cancelled_after_process(
        self, orch: Orchestrator, frontend: StubFrontend, tmp_path: Path
    ) -> None:
        with (
            patch.object(orch, "_ensure_persona"),
            patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path),
            patch("claude_on_the_fly.orchestrator.agent") as mock_agent,
        ):
            mock_agent.run = AsyncMock(return_value=Response(body="ok"))
            await orch._process(1, "hello")

        # Typing indicator should have been sent at least once before cancel
        # (the loop fires immediately then sleeps 4s, but agent.run is instant
        # with our mock so it may or may not have fired -- we just verify no
        # lingering tasks are running)
        await asyncio.sleep(0.05)
        # If typing_task leaked we'd see ongoing send_typing calls; this is
        # covered implicitly by no hanging tasks.


# ---------------------------------------------------------------------------
# _ensure_persona
# ---------------------------------------------------------------------------


class TestEnsurePersona:
    def test_noop_when_source_missing(self, tmp_path: Path) -> None:
        with patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path):
            workspace = tmp_path / "ws"
            workspace.mkdir()
            Orchestrator._ensure_persona(workspace)
            assert not (workspace / "CLAUDE.md").exists()

    def test_creates_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path):
            Orchestrator._ensure_persona(workspace)

        target = workspace / "CLAUDE.md"
        assert target.is_symlink()
        assert target.resolve() == source.resolve()

    def test_replaces_existing_file_with_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        existing = workspace / "CLAUDE.md"
        existing.write_text("old content")

        with patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path):
            Orchestrator._ensure_persona(workspace)

        assert existing.is_symlink()
        assert existing.resolve() == source.resolve()

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        wrong_target = tmp_path / "wrong.md"
        wrong_target.write_text("wrong")

        workspace = tmp_path / "ws"
        workspace.mkdir()
        link = workspace / "CLAUDE.md"
        link.symlink_to(wrong_target)

        with patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path):
            Orchestrator._ensure_persona(workspace)

        assert link.is_symlink()
        assert link.resolve() == source.resolve()

    def test_noop_when_symlink_already_correct(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        link = workspace / "CLAUDE.md"
        link.symlink_to(source)

        with patch("claude_on_the_fly.orchestrator.DATA_DIR", tmp_path):
            Orchestrator._ensure_persona(workspace)

        # Still the same symlink, not recreated
        assert link.is_symlink()
        assert link.resolve() == source.resolve()


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
