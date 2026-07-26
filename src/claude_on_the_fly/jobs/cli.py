"""claude-jobs entrypoint — the background-job worker's composition root.

Usage:
    claude-jobs [run]                 # run the worker daemon (default)
    claude-jobs doctor                # preflight checks for the worker
    claude-jobs enqueue <prompt>      # drop a test job (Slack-free producer)
        [--channel <id>] [--thread-ts <ts>]

This is the only place that wires the concrete adapters together: the queue
(`make_queue`), the agent runner, and the Slack notifier (with its own client,
token resolved from config). It owns the heartbeat and the SIGINT/SIGTERM →
stop-event plumbing; `run_loop` stays pure use-case. Modeled on
`symphony/cli.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from uuid import uuid4

from dotenv import load_dotenv

from claude_on_the_fly import agent, checks
from claude_on_the_fly.heartbeat import HeartbeatWriter
from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner
from claude_on_the_fly.jobs.core import AgentRunner, Job, JobQueue, Notifier
from claude_on_the_fly.jobs.registry import make_queue
from claude_on_the_fly.jobs.slack_notifier import SlackThreadNotifier
from claude_on_the_fly.jobs.worker import run_loop
from claude_on_the_fly.preflight import check_backend

logger = logging.getLogger(__name__)

SUBCOMMANDS = ("run", "doctor", "enqueue")
DEFAULT_POLL_INTERVAL_S = 2.0


def _setup_logging() -> None:
    """Adds a daily-rotating file handler (7-day retention) beside the console.
    `basicConfig` sets the ROOT logger from LOG_LEVEL, so both sinks share that
    level and the file handler's own DEBUG floor only bites with
    LOG_LEVEL=DEBUG. Deliberately NOT `preflight._setup_logging`, which is
    console-only: without a file handler `logs/jobs.log` is never written and
    the dashboard's jobs tab has nothing to tail."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_fmt,
    )
    log_dir = agent.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "jobs.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)


def _env_float(name: str, default: float) -> float:
    """Read a float from env var `name`, falling back to `default` when it is
    unset, blank, or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _poll_interval_s() -> float:
    return _env_float("JOBS_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S)


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


def build_components(token: str) -> tuple[JobQueue, AgentRunner, Notifier]:
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

    queue = make_queue()
    runner = OrchestratorAgentRunner(data_dir=agent.DATA_DIR, timeout=_timeout_s())
    notifier = SlackThreadNotifier(AsyncWebClient(token=token))
    return queue, runner, notifier


async def _run(token: str) -> None:
    """Wire the heartbeat + signal handlers, and drive the worker loop until
    stopped; then tear the heartbeat down."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    queue, runner, notifier = build_components(token)

    heartbeat = HeartbeatWriter("jobs")
    heartbeat_task = asyncio.create_task(heartbeat.run())
    logger.info("claude-jobs: started (poll every %.1fs)", _poll_interval_s())

    try:
        await run_loop(queue, runner, notifier, stop, _poll_interval_s())
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            heartbeat.path.unlink()
        except FileNotFoundError:
            pass
        logger.info("claude-jobs: shut down")


def _cmd_run() -> int:
    _setup_logging()
    check_backend()  # raises SystemExit on a misconfigured/absent agent CLI
    token_var, token = checks.resolve_jobs_token(os.environ)
    if not token:
        sys.stderr.write(
            "no Slack token for the job notifier; "
            "set JOBS_SLACK_TOKEN or SLACK_TOKEN in ~/.claude-on-the-fly/.env\n"
        )
        return 2
    loop_warning = _notifier_loop_warning(token_var, token)
    if loop_warning:
        logger.warning("%s", loop_warning)
    try:
        asyncio.run(_run(token))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_doctor() -> int:
    results = checks.check_frontend("jobs", os.environ) + checks.check_backend(
        os.environ
    )
    failed = 0
    for r in results:
        sys.stdout.write(f"  {r.name:30s} {r.status:8s} {r.detail}\n")
        if r.status != "ok":
            if r.fix_hint:
                sys.stdout.write(f"    hint: {r.fix_hint}\n")
            failed += 1
    if failed:
        sys.stdout.write(f"\n{failed} check(s) failed\n")
        return 1
    sys.stdout.write("\nall checks passed\n")
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
