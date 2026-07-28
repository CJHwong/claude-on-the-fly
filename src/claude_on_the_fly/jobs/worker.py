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

from claude_on_the_fly.jobs.core import AgentRunner, JobQueue, Notifier

logger = logging.getLogger(__name__)


async def run_once(queue: JobQueue, runner: AgentRunner, notifier: Notifier) -> bool:
    """Claim, run, complete, and notify one job. Returns True if a job was
    handled, False if the queue was empty."""
    job = queue.claim()
    if job is None:
        return False
    logger.info("jobs: running %s", job.id)
    result = await runner.run(job.prompt)
    queue.complete(job, result)
    await notifier.notify(job.origin, result)
    logger.info("jobs: completed %s (ok=%s)", job.id, result.ok)
    return True


async def run_loop(
    queue: JobQueue,
    runner: AgentRunner,
    notifier: Notifier,
    stop_event: asyncio.Event,
    poll_interval_s: float,
) -> None:
    """Drain the queue until `stop_event` is set.

    Busy periods drain with no sleep; an empty queue waits up to
    `poll_interval_s` on `stop_event` (interruptible). One failing job never
    kills the loop. On stop, an in-flight job is cancelled so its process tree
    is reaped within the supervisor's grace.
    """
    recovered = queue.recover_stale(None)
    if recovered:
        logger.info("jobs: recovered %d stale job(s) at startup", recovered)

    # One task for the whole loop: nothing ever clears `stop_event`, so its wait
    # resolves at most once and a per-iteration task would be built and torn
    # down every poll — roughly 43k allocations a day to watch a flag that can
    # only be set once.
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        while not stop_event.is_set():
            job_task = asyncio.create_task(run_once(queue, runner, notifier))
            await asyncio.wait(
                {job_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if not job_task.done():
                # Shutdown mid-job: cancel in-flight. The job stays in
                # cur/ and re-runs at-least-once next start.
                logger.info("jobs: stop requested — cancelling in-flight job")
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
                break

            try:
                did_work = job_task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("jobs: run_once failed (continuing)")
                did_work = False

            if not did_work:
                # `wait` rather than `wait_for`: a timeout here must leave
                # stop_task alone, and wait_for would cancel it.
                await asyncio.wait({stop_task}, timeout=poll_interval_s)
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
