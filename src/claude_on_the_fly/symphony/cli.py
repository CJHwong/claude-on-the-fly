"""claude-symphony entrypoint.

Usage:
    claude-symphony [config-path]                 # run the daemon (default)
    claude-symphony takeover <TICKET-KEY>         # print resume command for a ticket

Default config: ~/.claude-on-the-fly/symphony.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from claude_on_the_fly.agent import get_backend

from . import orchestrator, watch
from .agent_runner import session_uuid_for
from .workspace import ensure_workspace

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
DEFAULT_CONFIG = DATA_DIR / "symphony.yaml"

SUBCOMMANDS = ("run", "takeover", "watch")


def _setup_logging(platform: str = "symphony") -> None:
    """Console respects LOG_LEVEL; file always DEBUG with daily rotation, 7-day retention."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_fmt,
    )
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / f"{platform}.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)


async def _run(config_path: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows
    await orchestrator.run_loop(config_path, stop)


def _cmd_run(config_path: Path) -> int:
    if not config_path.exists():
        sys.stderr.write(f"config not found: {config_path}\n")
        sys.stderr.write(
            "See symphony.yaml.example and symphony-prompt.md.example at the repo root.\n"
        )
        return 2

    _setup_logging()
    logger.info("claude-symphony: config=%s", config_path)

    try:
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_takeover(ticket: str) -> int:
    """Print the interactive resume command for a ticket's session."""
    workspace = ensure_workspace(ticket)
    session_uuid = session_uuid_for(ticket)
    backend = get_backend()
    cmd = backend.takeover_command(workspace, session_uuid)

    if cmd is None:
        sys.stderr.write(
            f"no session yet for {ticket} (workspace={workspace}, uuid={session_uuid})\n"
        )
        sys.stderr.write(
            "The daemon must have run at least one turn on this ticket before takeover.\n"
        )
        return 1

    sys.stdout.write(
        f"Stop the daemon first to avoid session collisions:\n"
        f"  claude-tui stop symphony\n"
        f"\n"
        f"Then attach:\n"
        f"  cd {workspace} && {cmd}\n"
        f"\n"
        f"When done, resume the daemon:\n"
        f"  claude-tui resume\n"
    )
    return 0


def _cmd_watch(ticket: str) -> int:
    """Tail the live session JSONL and print formatted events as they appear."""
    from rich.console import Console

    workspace = ensure_workspace(ticket)
    session_uuid = session_uuid_for(ticket)
    backend = get_backend()
    log_path = backend.session_log_path(workspace, session_uuid)

    if log_path is None:
        sys.stderr.write(
            f"no session log for {ticket} "
            f"(workspace={workspace}, uuid={session_uuid})\n"
        )
        sys.stderr.write(
            "Either the daemon hasn't run a turn yet, or this backend "
            "doesn't expose a streamable session log.\n"
        )
        return 1

    console = Console()
    console.print(f"[dim]watching {log_path} — Ctrl-C to stop[/dim]")
    try:
        for event in watch.tail(log_path):
            line = watch.format_event(event)
            if line is None:
                continue
            console.print(line)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Subparser-based dispatch. Legacy `claude-symphony [config]` is normalized
    to `claude-symphony run [config]` in main() before parsing."""
    parser = argparse.ArgumentParser(
        prog="claude-symphony",
        description=(
            "Long-running daemon that polls a tracker and runs Claude Code in "
            "per-ticket sessions."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="Run the daemon (default)")
    run_parser.add_argument(
        "config",
        nargs="?",
        default=str(DEFAULT_CONFIG),
        help=f"Path to symphony.yaml (default: {DEFAULT_CONFIG})",
    )

    takeover_parser = sub.add_parser(
        "takeover",
        help="Print the interactive resume command for a ticket's session",
    )
    takeover_parser.add_argument("ticket", help="Ticket key (e.g. PROJ-123)")

    watch_parser = sub.add_parser(
        "watch",
        help="Tail a running ticket's session JSONL and print formatted events",
    )
    watch_parser.add_argument("ticket", help="Ticket key (e.g. PROJ-123)")

    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Insert `run` when no subcommand is given, preserving legacy `[config]` form."""
    if not argv:
        return ["run"]
    if argv[0] in SUBCOMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["run", *argv]


def main() -> int:
    load_dotenv()
    argv = _normalize_argv(sys.argv[1:])
    args = _build_parser().parse_args(argv)

    if args.cmd == "takeover":
        return _cmd_takeover(args.ticket)

    if args.cmd == "watch":
        return _cmd_watch(args.ticket)

    return _cmd_run(Path(args.config).expanduser())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
