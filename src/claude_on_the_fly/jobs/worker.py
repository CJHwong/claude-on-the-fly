"""The worker use-case: `claim → run → complete → notify`.

`run_once` is the single-job transaction; `run_loop` is the daemon that drains
the queue and idles between polls. Both depend only on the `JobQueue`,
`AgentRunner`, and `Notifier` ports — swapping any adapter needs no change here,
and this module imports nothing but its ports plus `asyncio`/`logging`.

Shutdown cancels the in-flight job: the supervisor SIGKILLs after a
5s grace, so a multi-minute run must be cancelled — not finished — for
`agent._exec`'s `finally` to reap the process tree in time. A cancelled job was
never completed, so it stays in `cur/` and re-runs on the next start
(at-least-once; jobs must be safe to re-run).
"""

from __future__ import annotations

import asyncio
import logging

from claude_on_the_fly.jobs.core import (
    AgentRunner,
    JobQueue,
    Notifier,
    OutcomeRecorder,
    Result,
)

logger = logging.getLogger(__name__)


async def _deliver(
    queue: JobQueue,
    notifier: Notifier,
    job_id: str,
    origin: dict,
    result: Result,
) -> bool:
    """Post a result and mark it delivered. False if it could not be posted.

    Marking only after the notifier returns is what makes an interrupted
    delivery recoverable: the result is already durable, so an undelivered one
    can be re-posted at the next start without re-running the job. A cancel
    lands here as CancelledError, which `except Exception` deliberately does not
    catch — it leaves the result unmarked and propagates, which is both what
    shutdown needs and what redelivery needs.
    """
    try:
        await notifier.notify(origin, result)
    except Exception:
        logger.exception(
            "jobs: could not deliver %s; keeping it for redelivery at next start",
            job_id,
        )
        return False
    queue.mark_delivered(job_id)
    return True


async def redeliver_pending(queue: JobQueue, notifier: Notifier) -> int:
    """Re-post results that were completed but never delivered. Returns the count.

    The reply, not the work: a job whose agent run finished has already cost
    what it costs, and re-running it would repeat every side effect it had. So
    the result is kept and only the delivery is retried.
    """
    pending = queue.undelivered()
    if not pending:
        return 0
    delivered = 0
    for delivery in pending:
        if await _deliver(
            queue, notifier, delivery.job_id, delivery.origin, delivery.result
        ):
            delivered += 1
    return delivered


async def run_once(
    queue: JobQueue,
    runner: AgentRunner,
    notifier: Notifier,
    recorder: OutcomeRecorder | None = None,
) -> bool:
    """Claim, run, complete, and notify one job. Returns True if a job was
    handled, False if the queue was empty."""
    job = queue.claim()
    if job is None:
        return False
    logger.info("jobs: running %s", job.id)
    result = await runner.run(job)
    queue.complete(job, result)
    if recorder is not None:
        # Guarded even though the port forbids raising: this sits between a
        # durable result and its delivery, so a bookkeeping bug must not cost the
        # reply. Logged rather than swallowed, or a producer would back off on
        # nothing and nobody could tell why.
        try:
            recorder.record(job, result)
        except Exception:
            logger.exception("jobs: could not record the outcome of %s", job.id)
    await _deliver(queue, notifier, job.id, job.origin, result)
    logger.info("jobs: completed %s (ok=%s)", job.id, result.ok)
    return True


async def run_loop(
    queue: JobQueue,
    runner: AgentRunner,
    notifier: Notifier,
    stop_event: asyncio.Event,
    poll_interval_s: float,
    concurrency: int = 1,
    recorder: OutcomeRecorder | None = None,
) -> None:
    """Drain the queue until `stop_event` is set, up to `concurrency` jobs at once.

    Busy periods drain with no sleep; a claim that finds nothing waits up to
    `poll_interval_s` on `stop_event` (interruptible) before topping up again.
    That wait happens even while other jobs are still running, and it has to:
    without it, one slot finding an empty queue would respawn instantly and spin
    on `claim()` for as long as the other slots stayed busy. One failing job never
    kills the loop. On stop, every in-flight job is cancelled so their process
    trees are reaped within the supervisor's grace.

    `concurrency=1` is the default and reproduces the single-job behavior exactly:
    one claim, one run, then either an immediate next claim or a poll wait.
    """
    recovered = queue.recover_stale(None)
    if recovered:
        logger.info("jobs: recovered %d stale job(s) at startup", recovered)

    # Before claiming anything: a reply somebody is still waiting for costs one
    # message to send, and the alternative to sending it is never sending it.
    redelivered = await redeliver_pending(queue, notifier)
    if redelivered:
        logger.info("jobs: redelivered %d undelivered result(s)", redelivered)

    # One task for the whole loop: nothing ever clears `stop_event`, so its wait
    # resolves at most once and a per-iteration task would be built and torn
    # down every poll — roughly 43k allocations a day to watch a flag that can
    # only be set once.
    stop_task = asyncio.create_task(stop_event.wait())
    running: set[asyncio.Task] = set()
    try:
        while not stop_event.is_set():
            while len(running) < concurrency:
                running.add(
                    asyncio.create_task(run_once(queue, runner, notifier, recorder))
                )

            await asyncio.wait(
                {*running, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_event.is_set():
                # Shutdown mid-job: the `finally` cancels whatever is in flight.
                # Those jobs stay in cur/ and re-run at-least-once next start.
                logger.info(
                    "jobs: stop requested — cancelling %d in-flight job(s)",
                    len(running),
                )
                break

            finished = {task for task in running if task.done()}
            running -= finished
            if not _harvest(finished):
                # `wait` rather than `wait_for`: a timeout here must leave
                # stop_task alone, and wait_for would cancel it.
                await asyncio.wait({stop_task}, timeout=poll_interval_s)
    finally:
        for task in running:
            task.cancel()
        stop_task.cancel()
        await asyncio.gather(*running, stop_task, return_exceptions=True)


def _harvest(finished: set[asyncio.Task]) -> bool:
    """Whether any of `finished` actually ran a job, logging those that raised.

    A task that failed counts as no work: the queue may well be empty, and
    treating a crash as "busy" would spin the loop on whatever is breaking.
    """
    did_work = False
    for task in finished:
        try:
            did_work = task.result() or did_work
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("jobs: run_once failed (continuing)")
    return did_work
