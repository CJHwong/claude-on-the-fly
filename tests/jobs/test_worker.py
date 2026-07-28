"""worker: run_once transaction, run_loop drain/idle, cancel-in-flight on stop,
and the ports-only import gate."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import claude_on_the_fly.jobs.worker as worker
from claude_on_the_fly.jobs.core import Job, QueueRow, Result
from claude_on_the_fly.jobs.file_queue import FileInboxQueue
from claude_on_the_fly.jobs.worker import run_loop, run_once


def _job(job_id: str = "100-a", prompt: str = "p") -> Job:
    return Job(id=job_id, prompt=prompt, origin={"channel": "C1", "thread_ts": "1.0"})


# --- test doubles ----------------------------------------------------------


class _FakeQueue:
    def __init__(self, jobs: list[Job] | None = None) -> None:
        self._jobs = list(jobs or [])
        self.completed: list[tuple[Job, Result]] = []

    def enqueue(self, job: Job) -> None:
        self._jobs.append(job)

    def claim(self) -> Job | None:
        return self._jobs.pop(0) if self._jobs else None

    def complete(self, job: Job, result: Result) -> None:
        self.completed.append((job, result))

    def list_unfinished(self, limit: int) -> list[QueueRow]:
        return []

    def recover_stale(self, ttl_s: float | None) -> int:
        return 0


class _CountingRunner:
    def __init__(self, result: Result | None = None) -> None:
        self.prompts: list[str] = []
        self._result = result

    async def run(self, prompt: str) -> Result:
        self.prompts.append(prompt)
        return self._result or Result(ok=True, text=f"ran:{prompt}")


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, Result]] = []

    async def notify(self, origin: dict, result: Result) -> None:
        self.calls.append((origin, result))


class _BlockingRunner:
    """Runs forever until released; records whether it was cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, prompt: str) -> Result:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Result(ok=True, text="done")


# --- run_once --------------------------------------------------------------


async def test_run_once_happy_path() -> None:
    job = _job()
    q = _FakeQueue([job])
    runner = _CountingRunner()
    notifier = _RecordingNotifier()

    did = await run_once(q, runner, notifier)

    assert did is True
    assert runner.prompts == ["p"]
    assert q.completed == [(job, Result(ok=True, text="ran:p"))]
    assert notifier.calls == [(job.origin, Result(ok=True, text="ran:p"))]


async def test_run_once_empty_queue_does_nothing() -> None:
    q = _FakeQueue([])
    runner = _CountingRunner()
    notifier = _RecordingNotifier()

    did = await run_once(q, runner, notifier)

    assert did is False
    assert runner.prompts == []
    assert q.completed == []
    assert notifier.calls == []


async def test_run_once_failure_still_completes_and_notifies() -> None:
    """A failure Result is delivered and the job reaches complete — not lost."""
    job = _job()
    q = _FakeQueue([job])
    runner = _CountingRunner(result=Result(ok=False, text="boom"))
    notifier = _RecordingNotifier()

    did = await run_once(q, runner, notifier)

    assert did is True
    assert q.completed == [(job, Result(ok=False, text="boom"))]
    assert notifier.calls == [(job.origin, Result(ok=False, text="boom"))]


# --- run_loop --------------------------------------------------------------


async def test_run_loop_drains_then_idles_until_stop(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    for i in range(3):
        q.enqueue(_job(job_id=f"{i:03d}-a", prompt=f"p{i}"))
    runner = _CountingRunner()
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.02)
    )

    async def _drained() -> None:
        while len(notifier.calls) < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_drained(), timeout=2.0)
    # Loop should now be idling on an empty queue; stop unblocks it.
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert len(notifier.calls) == 3
    assert sorted(runner.prompts) == ["p0", "p1", "p2"]
    assert q.claim() is None  # all archived to done/


async def test_run_loop_cancels_in_flight_job_on_stop(tmp_path: Path) -> None:
    """Stop cancels the in-flight job; it was never completed, so it stays in
    cur/ and becomes claimable again after recover_stale (at-least-once)."""
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job())
    runner = _BlockingRunner()
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.05)
    )
    await asyncio.wait_for(runner.started.wait(), timeout=1.0)  # job in-flight

    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert runner.cancelled is True
    assert notifier.calls == []  # cancelled before complete/notify
    assert q.claim() is None  # still in cur/, not new/
    assert q.recover_stale(None) == 1
    again = q.claim()
    assert again is not None and again.id == "100-a"


async def test_run_loop_recovers_stale_at_startup(tmp_path: Path) -> None:
    """A job left in cur/ (prior crash) is requeued and run on startup."""
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job())
    q.claim()  # simulate a crash: job stranded in cur/
    runner = _CountingRunner()
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.02)
    )

    async def _done() -> None:
        while not notifier.calls:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_done(), timeout=2.0)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert runner.prompts == ["p"]


# --- import gate -----------------------------------------------------------


def test_worker_depends_only_on_ports() -> None:
    """Acceptance #2: the worker imports nothing but jobs.core (ports) plus
    asyncio/logging — a machine-checkable clean-arch invariant."""
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)

    cof = {m for m in mods if m.split(".")[0] == "claude_on_the_fly"}
    assert cof <= {"claude_on_the_fly.jobs.core"}, f"worker reaches past ports: {cof}"
    roots = {m.split(".")[0] for m in mods}
    assert roots <= {"__future__", "asyncio", "logging", "claude_on_the_fly"}


async def test_stop_watcher_is_created_once_per_loop(monkeypatch) -> None:
    """stop_event is never cleared, so its wait resolves at most once — a task
    per poll iteration is pure churn at the default 2s cadence."""
    queue = _FakeQueue()
    stop = asyncio.Event()
    waits = 0
    real_wait = asyncio.Event.wait

    async def _counting_wait(self):
        nonlocal waits
        if self is stop:
            waits += 1
        return await real_wait(self)

    monkeypatch.setattr(asyncio.Event, "wait", _counting_wait)

    async def _stop_after_a_few_polls() -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(
        run_loop(
            queue,
            _CountingRunner(),
            _RecordingNotifier(),
            stop,
            poll_interval_s=0.001,
        ),
        _stop_after_a_few_polls(),
    )

    assert waits == 1
