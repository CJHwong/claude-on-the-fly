"""claude-jobs entrypoint — the background-job worker's composition root.

Usage:
    claude-jobs [run]                 # run the worker daemon (default)
    claude-jobs doctor                # preflight checks for the worker
    claude-jobs enqueue <prompt>      # drop a test job (Slack-free producer)
        [--channel <id>] [--thread-ts <ts>]

This is the only place that wires the concrete adapters together: the queue
(`make_queue`), the agent runner, and the Slack notifier (with its own client,
token resolved from config). It owns the heartbeat and the SIGINT/SIGTERM →
stop-event plumbing; `run_loop` stays pure use-case.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from typing import cast
from uuid import uuid4

from dotenv import load_dotenv

from claude_on_the_fly import agent, checks, settings
from claude_on_the_fly.heartbeat import HeartbeatWriter, live_pid
from claude_on_the_fly.jobs.agent_runner import (
    OrchestratorAgentRunner,
    sweep_run_workspaces,
)
from claude_on_the_fly.jobs.core import (
    AgentRunner,
    Job,
    JobQueue,
    Notifier,
    OutcomeRecorder,
)
from claude_on_the_fly.jobs.key_state import (
    KeyStateOutcomeRecorder,
    KeyStateStore,
)
from claude_on_the_fly.jobs.notifiers import LogNotifier, RoutingNotifier
from claude_on_the_fly.jobs.orphans import LEDGER_NAME, ProcessLedger
from claude_on_the_fly.jobs.registry import make_queue
from claude_on_the_fly.jobs.slack_notifier import SlackThreadNotifier
from claude_on_the_fly.jobs.worker import run_loop
from claude_on_the_fly.preflight import check_backend, setup_daemon_logging

logger = logging.getLogger(__name__)

SUBCOMMANDS = ("run", "doctor", "enqueue")
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_CONCURRENCY = 1
# Matches the log retention window, so an operator holds one number for "how far
# back can I look", not two.
DEFAULT_WORKSPACE_KEEP_DAYS = 30


def _setup_logging() -> None:
    """Console plus `logs/jobs.log`, which the dashboard's jobs tab tails."""
    setup_daemon_logging("jobs")


def _env_float(name: str, default: float) -> float:
    """Read a float setting, falling back to `default` when it is unset, blank, or
    unparseable. Reads config.yaml as well as the environment."""
    raw = settings.get(name).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _poll_interval_s() -> float:
    return _env_float("JOBS_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S)


def _concurrency() -> int:
    """How many jobs this worker runs at once, from `JOBS_CONCURRENCY`.

    Default 1, which is the behavior every install had before this existed. This
    is a property of the machine (how many agent CLIs it can host), deliberately
    separate from a producer's own cap on how much of its work may be outstanding.
    A junk or non-positive value falls back to 1 rather than refusing to start:
    the worker running slowly beats the worker not running.
    """
    value = int(_env_float("JOBS_CONCURRENCY", DEFAULT_CONCURRENCY))
    if value < 1:
        logger.warning("JOBS_CONCURRENCY=%s is below 1, using 1", value)
        return 1
    return value


def _workspace_keep_days() -> int:
    """How long a finished one-shot job workspace is kept, from
    `JOBS_WORKSPACE_KEEP_DAYS`. 0 or less disables the sweep and keeps them
    forever, which is a choice an operator with the disk for it may want.
    """
    return int(_env_float("JOBS_WORKSPACE_KEEP_DAYS", DEFAULT_WORKSPACE_KEEP_DAYS))


def _timeout_s() -> float | None:
    """Per-job wall-clock limit for `agent.run`, in seconds, from `JOBS_TIMEOUT`.

    A non-positive value (`JOBS_TIMEOUT=0` or negative) means "no limit" and maps
    to `None` — `agent.run` then skips `asyncio.wait_for` and waits indefinitely.
    Unset/blank/unparseable falls back to `agent.DEFAULT_TIMEOUT`.
    """
    timeout = _env_float("JOBS_TIMEOUT", agent.DEFAULT_TIMEOUT)
    return timeout if timeout > 0 else None


def _notifier_loop_warning(token_var: str | None, token: str) -> str | None:
    """Return a warning when the worker would post under the frontend's own user
    identity, else None.

    A user (`xoxp-`) token inherited from the shared `SLACK_TOKEN` makes the
    worker post job results as that user; the Slack frontend runs its own
    process with a per-process dedup set, so it re-ingests those results as new
    input — one spurious agent turn per job. A bot (`xoxb-`) token, or an
    explicit `JOBS_SLACK_TOKEN` the deployer chose, is left alone.
    """
    if token_var != "JOBS_SLACK_TOKEN" and token.startswith("xoxp-"):
        return (
            "jobs notifier inherits the frontend's user token; job results post "
            "as that user and the Slack frontend can re-ingest them as new input "
            "(a spurious agent turn per job). Set JOBS_SLACK_TOKEN to a bot "
            "(xoxb-) token to avoid this."
        )
    return None


def build_components(
    token: str,
) -> tuple[JobQueue, AgentRunner, Notifier, OutcomeRecorder]:
    """Wire the worker's three adapters from config — the single construction
    point the daemon uses.

    Extracted from `_run` so a composition test can drive the SAME wiring the
    daemon does (real queue, real runner, real Slack-backed notifier), which is
    what would catch a mis-constructed notifier or a token that never reaches
    the client. Returns the ports, not the concretes, so this stays the one
    place that names the runner and the notifier; the queue concrete is named
    in `registry.py`, behind `make_queue()`.
    """
    from slack_sdk.web.async_client import AsyncWebClient

    from claude_on_the_fly.cron import append_log

    queue = make_queue()
    runner = OrchestratorAgentRunner(data_dir=agent.DATA_DIR, timeout=_timeout_s())
    # One worker drains both producers, so delivery has to fan back out by where
    # the job came from: a Slack thread, or the cron entry's own log file.
    notifier = RoutingNotifier(
        {
            "slack": SlackThreadNotifier(AsyncWebClient(token=token)),
            "cron": LogNotifier(append_log),
        }
    )
    # Closes the producer's feedback loop: cron records attempts when it
    # enqueues, this records how they turned out, and its backoff reads both.
    recorder = KeyStateOutcomeRecorder(KeyStateStore(agent.DATA_DIR / "jobs"))
    return queue, runner, notifier, recorder


def _running_jobs(runner: OrchestratorAgentRunner) -> dict:
    """The in-flight jobs for the heartbeat, shaped like the orchestrator's
    chat `running_jobs` so the dashboard normalizes both the same way."""
    now = time.monotonic()
    return {
        "running_jobs": [
            {
                "job_id": job_id,
                "key": info["key"],
                "workspace": info["workspace"],
                "uptime_s": int(now - info["started_at_monotonic"]),
                "session_uuid": info["session_uuid"],
            }
            for job_id, info in runner.in_flight.items()
        ]
    }


async def _run(token: str) -> None:
    """Wire the heartbeat + signal handlers, and drive the worker loop until
    stopped; then tear the heartbeat down."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler is not implemented on Windows.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    queue, runner, notifier, recorder = build_components(token)

    # Reap what a previous worker orphaned, before anything claims work:
    # run_loop's first act is recover_stale, and re-running a job whose earlier
    # copy is still executing is exactly what this prevents.
    ledger = ProcessLedger(agent.DATA_DIR / "jobs" / LEDGER_NAME)
    killed = ledger.sweep()
    if killed:
        logger.warning(
            "claude-jobs: reaped %d orphaned agent process group(s) from a "
            "previous run",
            killed,
        )
    agent.add_process_listener(ledger.on_process)

    # Retention for finished one-shot workspaces. Startup is the whole cadence:
    # the sweep is bounded by what one worker's lifetime accumulated, and a worker
    # that never restarts is not accumulating either. Before the loop claims
    # anything, so a long rmtree cannot compete with a running job for the disk.
    retired = sweep_run_workspaces(agent.DATA_DIR, days=_workspace_keep_days())
    if retired:
        logger.info(
            "claude-jobs: retired %d finished job workspace(s) past retention",
            len(retired),
        )

    # The composition root knows the runner is the concrete
    # OrchestratorAgentRunner (build_components constructs it); the port type
    # only promises `run`, so the in_flight access needs the cast.
    heartbeat = HeartbeatWriter(
        "jobs",
        extra_provider=lambda: _running_jobs(cast(OrchestratorAgentRunner, runner)),
    )
    heartbeat_task = asyncio.create_task(heartbeat.run())
    concurrency = _concurrency()
    logger.info(
        "claude-jobs: started (poll every %.1fs, up to %d job(s) at once)",
        _poll_interval_s(),
        concurrency,
    )

    try:
        await run_loop(
            queue,
            runner,
            notifier,
            stop,
            _poll_interval_s(),
            concurrency=concurrency,
            recorder=recorder,
        )
    finally:
        agent.remove_process_listener(ledger.on_process)
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            heartbeat.path.unlink()
        logger.info("claude-jobs: shut down")


def _cmd_run() -> int:
    _setup_logging()
    running_pid = live_pid("jobs")
    if running_pid is not None:
        # The worker is a singleton, and nothing else enforces it: only
        # supervisor.spawn checks, so `claude-jobs` typed in a shell next to a
        # supervised one would sail past. Its first act is
        # `recover_stale(None)`, which moves the in-flight job out of cur/ and
        # back into new/ — so it claims the job the live worker is still
        # executing and runs the whole prompt a second time, concurrently. The
        # requester gets two replies, any side effect happens twice, and the
        # original worker's complete() then finds its own file gone.
        sys.stderr.write(
            f"claude-jobs is already running (pid {running_pid}); refusing to "
            "start a second worker on the same queue. Stop it first, or use "
            "`claude-jobs enqueue` to add work to the running one.\n"
        )
        return 2
    check_backend()  # raises SystemExit on a misconfigured/absent agent CLI
    token_var, token = checks.resolve_jobs_token(os.environ)
    if not token:
        sys.stderr.write(
            "no Slack token for the job notifier; "
            f"set JOBS_SLACK_TOKEN or SLACK_TOKEN in {agent.DATA_DIR / '.env'}\n"
        )
        return 2
    loop_warning = _notifier_loop_warning(token_var, token)
    if loop_warning:
        logger.warning("%s", loop_warning)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(token))
    return 0


def _cmd_doctor() -> int:
    env = settings.environment()
    results = checks.check_frontend("jobs", env) + checks.check_backend(env)
    failed = 0
    warned = 0
    for r in results:
        name = checks.display_name(r.name)
        sys.stdout.write(f"  {name:30s} {r.status:8s} {r.detail}\n")
        if r.status != "ok":
            if r.fix_hint:
                sys.stdout.write(f"    hint: {r.fix_hint}\n")
            # Advisory results are printed with their hint but do not fail the
            # run: an enqueue-only worker is a legitimate install, not an error.
            if checks.is_blocking(r):
                failed += 1
            else:
                warned += 1
    if failed:
        sys.stdout.write(f"\n{failed} check(s) failed\n")
        return 1
    suffix = f" ({warned} warning(s))" if warned else ""
    sys.stdout.write(f"\nall checks passed{suffix}\n")
    return 0


def _cmd_enqueue(prompt: str, channel: str | None, thread_ts: str | None) -> int:
    """Drop one job into the shared queue without going through Slack — a smoke
    producer for testing the worker end to end."""
    queue = make_queue()
    job_id = f"{time.time_ns()}-{uuid4().hex[:8]}"
    origin = {"channel": channel or "", "thread_ts": thread_ts, "sender_id": "cli"}
    queue.enqueue(Job(id=job_id, prompt=prompt, origin=origin))
    sys.stdout.write(f"queued job {job_id}\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-jobs",
        description="Background-job worker: claim, run, and reply into the origin thread.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Run the worker daemon (default)")
    sub.add_parser("doctor", help="Run the worker's preflight checks")
    enq = sub.add_parser("enqueue", help="Enqueue a test job (Slack-free producer)")
    enq.add_argument("prompt", help="The task prompt to run")
    enq.add_argument("--channel", default=None, help="Slack channel id for the reply")
    enq.add_argument("--thread-ts", default=None, help="Slack thread ts for the reply")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Insert `run` when no subcommand is given (bare `claude-jobs`)."""
    if not argv:
        return ["run"]
    if argv[0] in SUBCOMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["run", *argv]


def main() -> int:
    load_dotenv()
    argv = _normalize_argv(sys.argv[1:])
    args = _build_parser().parse_args(argv)

    if args.cmd == "doctor":
        return _cmd_doctor()
    if args.cmd == "enqueue":
        return _cmd_enqueue(args.prompt, args.channel, args.thread_ts)
    return _cmd_run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
