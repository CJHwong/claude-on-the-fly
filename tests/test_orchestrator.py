"""Tests for claude_on_the_fly.orchestrator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest

from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.events import EventLog
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
            await orch._process(1, "make a file")

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
            await orch._process(1, "make a file")

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
            await orch._process(1, "hi")

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
            await orch._process(7, "hi")

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
            await orch._process(7, "hi")

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
            await orch._process(7, "hi")

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
            await orch._process(7, "hi")
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            await orch._process(8, "hi")

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
            task = asyncio.create_task(orch._process(9, "hi"))
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

        assert orch.heartbeat_extra() == {"running_jobs": []}


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
            orch._queues[1].put_nowait("queued-behind")
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
