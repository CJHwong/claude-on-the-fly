"""worker: run_once transaction, run_loop drain/idle, cancel-in-flight on stop,
and the ports-only import gate."""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path

import pytest

import claude_on_the_fly.jobs.worker as worker
from claude_on_the_fly.jobs.core import Delivery, Job, QueueRow, Result
from claude_on_the_fly.jobs.file_queue import FileInboxQueue
from claude_on_the_fly.jobs.worker import redeliver_pending, run_loop, run_once


def _job(job_id: str = "100-a", prompt: str = "p") -> Job:
    return Job(id=job_id, prompt=prompt, origin={"channel": "C1", "thread_ts": "1.0"})


# --- test doubles ----------------------------------------------------------


class _FakeQueue:
    def __init__(self, jobs: list[Job] | None = None) -> None:
        self._jobs = list(jobs or [])
        self.completed: list[tuple[Job, Result]] = []
        self.delivered: list[str] = []
        self.pending: list[Delivery] = []

    def enqueue(self, job: Job) -> None:
        self._jobs.append(job)

    def claim(self) -> Job | None:
        return self._jobs.pop(0) if self._jobs else None

    def complete(self, job: Job, result: Result) -> None:
        self.completed.append((job, result))

    def mark_delivered(self, job_id: str) -> None:
        self.delivered.append(job_id)

    def undelivered(self) -> list[Delivery]:
        return list(self.pending)

    def list_unfinished(self, limit: int) -> list[QueueRow]:
        return []

    def recover_stale(self, ttl_s: float | None) -> int:
        return 0

    def count_unfinished(self, entry: str, item: str | None = None) -> int:
        return 0


class _CountingRunner:
    def __init__(self, result: Result | None = None) -> None:
        self.prompts: list[str] = []
        self._result = result

    async def run(self, job: Job) -> Result:
        self.prompts.append(job.prompt)
        return self._result or Result(ok=True, text=f"ran:{job.prompt}")


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

    async def run(self, job: Job) -> Result:
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
    # One notice, not a result: whoever asked is told the restart cut it short.
    assert len(notifier.calls) == 1
    _origin, result = notifier.calls[0]
    assert result.ok is False
    assert "runs again by itself" in result.text
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


# --- delivery ---------------------------------------------------------------


class _FailingNotifier:
    def __init__(self, fail_times: int = 1) -> None:
        self.remaining = fail_times
        self.calls: list[tuple[dict, Result]] = []

    async def notify(self, origin: dict, result: Result) -> None:
        self.calls.append((origin, result))
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("channel unreachable")


async def test_successful_delivery_is_marked() -> None:
    job = _job()
    queue = _FakeQueue([job])

    await run_once(queue, _CountingRunner(), _RecordingNotifier())

    assert queue.delivered == [job.id]


async def test_failed_delivery_is_not_marked_and_does_not_fail_the_job() -> None:
    """The work is done and durable; only the reply is outstanding. Failing the
    job here would re-run the agent and repeat every side effect it had."""
    job = _job()
    queue = _FakeQueue([job])

    handled = await run_once(queue, _CountingRunner(), _FailingNotifier())

    assert handled is True
    assert queue.completed  # the result is archived
    assert queue.delivered == []  # but not marked delivered


async def test_cancel_during_delivery_leaves_the_result_unmarked() -> None:
    """SIGTERM landing on the notify await used to lose the reply for good: the
    job was already in done/, and recover_stale only ever looks at cur/."""

    class _HangingNotifier:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def notify(self, origin: dict, result: Result) -> None:
            self.started.set()
            await asyncio.sleep(3600)

    job = _job()
    queue = _FakeQueue([job])
    notifier = _HangingNotifier()

    task = asyncio.create_task(run_once(queue, _CountingRunner(), notifier))
    await notifier.started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert queue.completed  # work finished
    assert queue.delivered == []  # reply still owed


async def test_redelivery_reposts_without_rerunning_the_job() -> None:
    queue = _FakeQueue()
    queue.pending = [
        Delivery(job_id="100-a", origin={"channel": "C1"}, result=Result(True, "done")),
        Delivery(job_id="200-b", origin={"channel": "C2"}, result=Result(True, "also")),
    ]
    runner = _CountingRunner()
    notifier = _RecordingNotifier()

    count = await redeliver_pending(queue, notifier)

    assert count == 2
    assert queue.delivered == ["100-a", "200-b"]
    assert runner.prompts == []  # the agent never ran again


async def test_redelivery_keeps_a_still_failing_result_for_next_time() -> None:
    queue = _FakeQueue()
    queue.pending = [
        Delivery(job_id="100-a", origin={"channel": "C1"}, result=Result(True, "done"))
    ]

    count = await redeliver_pending(queue, _FailingNotifier())

    assert count == 0
    assert queue.delivered == []


async def test_run_loop_redelivers_before_claiming_work() -> None:
    """A reply somebody is already waiting for goes out before new work starts."""
    queue = _FakeQueue()
    queue.pending = [
        Delivery(job_id="100-a", origin={"channel": "C1"}, result=Result(True, "done"))
    ]
    stop = asyncio.Event()
    notifier = _RecordingNotifier()

    async def _stop_soon() -> None:
        while not notifier.calls:
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.gather(
        run_loop(queue, _CountingRunner(), notifier, stop, poll_interval_s=0.01),
        _stop_soon(),
    )

    assert [origin for origin, _ in notifier.calls] == [{"channel": "C1"}]
    assert queue.delivered == ["100-a"]


# --- concurrency -----------------------------------------------------------


class _ConcurrencyProbe:
    """Holds every run open until `expect` of them are in flight at once, so a
    serial loop deadlocks instead of quietly passing."""

    def __init__(self, expect: int) -> None:
        self.in_flight = 0
        self.peak = 0
        self._expect = expect
        self._all_in = asyncio.Event()

    async def run(self, job: Job) -> Result:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self._expect:
            self._all_in.set()
        await self._all_in.wait()
        self.in_flight -= 1
        return Result(ok=True, text="ok")


class _CancelCountingRunner:
    """Blocks forever; counts starts and cancellations."""

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0

    async def run(self, job: Job) -> Result:
        self.started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")


class _CountingClaimQueue(_FakeQueue):
    """Counts claims, so a loop that spins on an empty queue is measurable."""

    def __init__(self, jobs: list[Job] | None = None) -> None:
        super().__init__(jobs)
        self.claims = 0

    def claim(self) -> Job | None:
        self.claims += 1
        return super().claim()


async def test_concurrency_runs_jobs_at_the_same_time() -> None:
    jobs = [_job("100-a", "1"), _job("100-b", "2"), _job("100-c", "3")]
    q = _FakeQueue(jobs)
    runner = _ConcurrencyProbe(expect=3)
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.01, concurrency=3)
    )

    async def _done() -> None:
        while len(notifier.calls) < 3:
            await asyncio.sleep(0.01)

    # A serial loop never lets all three start, so this times out rather than
    # reporting a wrong peak.
    await asyncio.wait_for(_done(), timeout=2.0)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert runner.peak == 3


async def test_default_concurrency_is_one() -> None:
    """The pre-existing behavior has to be the default: one claim, one run."""
    q = _FakeQueue([_job("100-a", "1"), _job("100-b", "2")])
    runner = _ConcurrencyProbe(expect=2)
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.01)
    )
    await asyncio.sleep(0.1)

    assert runner.peak == 1, "two jobs must not overlap without opting in"
    assert runner.in_flight == 1, "and the first is still held open"

    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)


async def test_stop_cancels_every_in_flight_job() -> None:
    """Shutdown must reach all N, not just one: an un-cancelled agent outlives
    the supervisor's grace and is orphaned under bypassPermissions."""
    q = _FakeQueue([_job(f"100-{n}", str(n)) for n in range(3)])
    runner = _CancelCountingRunner()
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.01, concurrency=3)
    )

    async def _all_started() -> None:
        while runner.started < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_all_started(), timeout=2.0)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert runner.cancelled == 3


async def test_an_empty_claim_waits_even_while_another_job_runs() -> None:
    """The hot-spin guard. With one long job and three idle slots, a loop that
    only waited when *nothing* was running would respawn the idle three
    instantly and hammer claim() for as long as the long job lasted."""
    q = _CountingClaimQueue([_job("100-a", "long")])
    runner = _CancelCountingRunner()
    notifier = _RecordingNotifier()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, runner, notifier, stop, poll_interval_s=0.05, concurrency=4)
    )
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    # Paced at ~3 idle claims per 50ms interval this lands near 15; an unguarded
    # spin over the same 200ms is thousands. The bound is deliberately loose so
    # only the pathology can trip it.
    assert q.claims < 60, f"claim() called {q.claims} times — the loop is spinning"


# --- outcome recording ------------------------------------------------------


class _RecordingOutcomes:
    def __init__(self, blow_up: bool = False) -> None:
        self.recorded: list[tuple[str | None, bool]] = []
        self._blow_up = blow_up

    def record(self, job: Job, result: Result) -> None:
        if self._blow_up:
            raise RuntimeError("bookkeeping exploded")
        self.recorded.append((job.key, result.ok))


async def test_run_once_records_the_outcome() -> None:
    """The gap that made the producer's backoff dead code: nothing reported a
    finished job back, so failures never accumulated and a broken key was retried
    at the poll interval forever."""
    job = Job(id="100-a", prompt="p", origin={"kind": "cron"}, key="jira/ACE-1")
    q = _FakeQueue([job])
    recorder = _RecordingOutcomes()

    await run_once(q, _CountingRunner(), _RecordingNotifier(), recorder)

    assert recorder.recorded == [("jira/ACE-1", True)]


async def test_a_failed_job_is_recorded_as_a_failure() -> None:
    job = Job(id="100-a", prompt="p", origin={"kind": "cron"}, key="jira/ACE-1")
    q = _FakeQueue([job])
    recorder = _RecordingOutcomes()
    runner = _CountingRunner(result=Result(ok=False, text="boom"))

    await run_once(q, runner, _RecordingNotifier(), recorder)

    assert recorder.recorded == [("jira/ACE-1", False)]


async def test_a_broken_recorder_does_not_cost_the_reply() -> None:
    """It runs between a durable result and its delivery, so a bookkeeping bug
    must not turn a recoverable problem into a reply nobody receives."""
    job = Job(id="100-a", prompt="p", origin={"kind": "cron"}, key="jira/ACE-1")
    q = _FakeQueue([job])
    notifier = _RecordingNotifier()

    did = await run_once(
        q, _CountingRunner(), notifier, _RecordingOutcomes(blow_up=True)
    )

    assert did is True
    assert len(notifier.calls) == 1
    assert q.delivered == ["100-a"]


async def test_run_loop_threads_the_recorder_through() -> None:
    """run_once taking it is not enough — run_loop is what the daemon calls."""
    job = Job(id="100-a", prompt="p", origin={"kind": "cron"}, key="jira/ACE-1")
    q = _FakeQueue([job])
    notifier = _RecordingNotifier()
    recorder = _RecordingOutcomes()
    stop = asyncio.Event()

    loop_task = asyncio.create_task(
        run_loop(q, _CountingRunner(), notifier, stop, 0.02, recorder=recorder)
    )

    async def _done() -> None:
        while not recorder.recorded:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_done(), timeout=2.0)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert recorder.recorded == [("jira/ACE-1", True)]


async def test_one_failing_job_does_not_stop_the_others_in_the_batch(caplog) -> None:
    """The batch is gathered, so an unhandled error inside one `run_once` must not
    take the whole drain down with it: the other slots' work is already done and the
    loop has to keep claiming."""

    async def ok_task() -> bool:
        return True

    async def failing_task() -> bool:
        raise RuntimeError("queue vanished mid-claim")

    finished = [
        asyncio.ensure_future(ok_task()),
        asyncio.ensure_future(failing_task()),
    ]
    await asyncio.gather(*finished, return_exceptions=True)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.jobs.worker"):
        assert worker._harvest(set(finished)) is True
    assert "run_once failed (continuing)" in "\n".join(
        r.getMessage() for r in caplog.records
    )


async def test_a_cancelled_batch_member_propagates() -> None:
    """Cancellation is how the loop stops. Swallowing it here would make a stop
    request look like an ordinary failure and the loop would keep going."""

    async def never() -> bool:
        await asyncio.Event().wait()
        return True

    task = asyncio.ensure_future(never())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    with pytest.raises(asyncio.CancelledError):
        worker._harvest({task})


# --- interruption notice ---------------------------------------------------


async def test_a_cancelled_job_tells_its_origin_without_marking_a_delivery() -> None:
    """The job itself re-runs, so this is a notice and not a result: marking it
    delivered would suppress the redelivery of a reply that never existed."""
    job = _job()
    q = _FakeQueue([job])
    runner = _BlockingRunner()
    notifier = _RecordingNotifier()

    task = asyncio.create_task(run_once(q, runner, notifier))
    await asyncio.wait_for(runner.started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(notifier.calls) == 1
    origin, result = notifier.calls[0]
    assert origin == job.origin
    assert result.ok is False
    assert q.completed == []
    assert q.delivered == []


async def test_a_notice_that_cannot_be_posted_does_not_hide_the_cancellation(
    caplog,
) -> None:
    """Shutdown must proceed: the notice is a courtesy, the cancel is the job."""
    q = _FakeQueue([_job()])
    runner = _BlockingRunner()

    class _BrokenNotifier:
        async def notify(self, origin: dict, result: Result) -> None:
            raise RuntimeError("channel_not_found")

    task = asyncio.create_task(run_once(q, runner, _BrokenNotifier()))
    await asyncio.wait_for(runner.started.wait(), timeout=1.0)
    task.cancel()
    with (
        caplog.at_level("ERROR", logger="claude_on_the_fly.jobs.worker"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task

    assert "interrupted" in caplog.text


async def test_a_notice_that_hangs_cannot_delay_the_exit_past_its_budget(
    monkeypatch,
) -> None:
    """The supervisor SIGKILLs after its grace; an unreachable API must cost a
    bounded wait, not the whole window."""
    monkeypatch.setattr(worker, "NOTICE_BUDGET_S", 0.01)
    q = _FakeQueue([_job()])
    runner = _BlockingRunner()

    class _HangingNotifier:
        async def notify(self, origin: dict, result: Result) -> None:
            await asyncio.Event().wait()

    task = asyncio.create_task(run_once(q, runner, _HangingNotifier()))
    await asyncio.wait_for(runner.started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
