"""Clean core for background jobs — data types and ports, no I/O SDK.

A `Job` is a unit of work carrying an opaque `origin` (the core never reads it;
adapters at the edge do). A `Result` is the outcome of running one. The three
ports — `JobQueue`, `AgentRunner`, `Notifier` — are the only surfaces the worker
loop depends on, so any adapter (a file queue, a broker, a real or fake runner)
can be swapped in without touching the use-case.

Imports: standard library only. No chat / DB / network / LLM / filesystem
client, no `agent`, no Slack. Vendor vocabulary (channel ids, thread
timestamps) lives inside `origin` and is never named here. Protocol style mirrors
`symphony/tracker/base.py` and `AgentBackend` in `agent.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Job:
    """One unit of background work.

    `origin` is opaque routing context the producer attaches and the notifier
    reads back on completion; the core, the queue, and the worker pass it
    through untouched. It must stay JSON-serializable so a file-backed queue can
    round-trip it (the default producer builds a flat ``{str: str}``).
    """

    id: str
    prompt: str
    origin: dict[str, Any]


@dataclass(frozen=True)
class Result:
    """Outcome of running a job. `ok` is False for a handled failure; `text` is
    the agent's reply on success or the error summary on failure."""

    ok: bool
    text: str


@dataclass(frozen=True)
class QueueRow:
    """One not-yet-finished job, as a reader sees it: still queued, or claimed
    and running.

    `prompt` and `enqueued_at` are None when they could not be derived, so a
    half-written or hand-mangled record degrades one field instead of failing
    the whole read. `prompt` may be truncated — this is a listing, not the job.
    `origin` is the same opaque dict the producer attached; the core does not
    read it, but a caller that speaks the producer's vocabulary can (the Slack
    frontend filters a listing down to the channel that asked).
    """

    id: str
    prompt: str | None
    origin: dict[str, Any]
    enqueued_at: datetime | None
    in_flight: bool


@dataclass(frozen=True)
class Delivery:
    """A finished job's result that has not reached its origin yet."""

    job_id: str
    origin: dict[str, Any]
    result: Result


@runtime_checkable
class JobQueue(Protocol):
    """Read-write persistence port.

    Synchronous on purpose: the default adapter is fast local-filesystem work
    (atomic renames), unit-testable without an event loop, and called from async
    code the same way `EventLog.append` is.
    """

    def enqueue(self, job: Job) -> None:
        """Durably record a new job so a later `claim` can return it."""
        ...

    def claim(self) -> Job | None:
        """Atomically take the next runnable job, or None when none is
        claimable. A claimed job is owned by this caller until `complete`."""
        ...

    def complete(self, job: Job, result: Result) -> None:
        """Mark a claimed job finished and persist its result."""
        ...

    def mark_delivered(self, job_id: str) -> None:
        """Record that this job's result reached its origin.

        Separate from `complete` because the two can fail independently: the
        work is finished and durable the moment `complete` returns, but the
        reply has not been delivered until a notifier says so.
        """
        ...

    def undelivered(self) -> list[Delivery]:
        """Completed results that were never marked delivered, oldest first.

        These are replies somebody is still waiting for: the worker was
        cancelled between finishing and posting, or the post itself failed.
        Re-notifying costs one message; re-running the job would cost another
        agent execution and repeat its side effects, which is why the result is
        kept rather than the job being requeued.

        An adapter may bound this by age — a reply old enough is not worth
        delivering, and a permanently undeliverable one must not be retried
        forever.
        """
        ...

    def list_unfinished(self, limit: int) -> list[QueueRow]:
        """Up to `limit` jobs that have not completed — in-flight first, then
        queued, oldest first within each.

        A read, not a claim: it must not create, move, or modify anything, so a
        producer can answer "what is queued?" without disturbing the worker.
        Callers use it to show a listing, so an adapter is free to return
        truncated prompts.
        """
        ...

    def recover_stale(self, ttl_s: float | None) -> int:
        """Requeue jobs claimed but never completed (a worker crashed mid-run)
        so they run again, and return how many were requeued.

        `ttl_s=None` requeues every in-flight job (the single-worker default);
        a positive TTL requeues only those idle longer than it (multi-worker).
        A broker-backed adapter with its own visibility timeout implements this
        as a no-op returning 0. It lives on the port so the worker depends only
        on ports and every adapter guarantees recovery of its own
        claimed-but-crashed jobs.
        """
        ...


@runtime_checkable
class AgentRunner(Protocol):
    """Execution port — runs a job's prompt and returns its Result. Async; the
    default adapter drives a subprocess agent."""

    async def run(self, prompt: str) -> Result: ...


@runtime_checkable
class Notifier(Protocol):
    """Delivery port — posts a finished job's Result back to its `origin`.
    Async; the default adapter posts into a chat thread.

    Raises on failure rather than swallowing: returning normally is what marks
    a result delivered, so an adapter that hides a failed post turns a
    recoverable miss into a reply nobody ever receives. Whether a failure is
    worth retrying is the worker's decision, not the adapter's.
    """

    async def notify(self, origin: dict[str, Any], result: Result) -> None: ...
