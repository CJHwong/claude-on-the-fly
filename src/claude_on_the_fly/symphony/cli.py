"""claude-symphony entrypoint.

Usage:
    claude-symphony [config-path]                 # run the daemon (default)
    claude-symphony takeover <TICKET-KEY>         # print resume command
    claude-symphony watch <TICKET-KEY>            # tail live session JSONL
    claude-symphony <takeover|watch> --source <jira|github> <KEY>

Default config: ~/.claude-on-the-fly/symphony.yaml

Source resolution for takeover / watch (in order):
  1. `--source <name>` if provided
  2. Auto-detect by key shape (`ABC-123` → jira, `owner/repo#123` → github)
  3. Scan per-source workspace dirs; if exactly one matches, use it
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from claude_on_the_fly.agent import get_backend

from . import orchestrator, watch
from .agent_runner import session_uuid_for
from .workspace import WORKSPACES_ROOT, ensure_workspace, sanitize_key

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
DEFAULT_CONFIG = DATA_DIR / "symphony.yaml"

SUBCOMMANDS = ("run", "takeover", "watch")

# Auto-detect heuristics. Conservative — falls through to workspace scan if
# neither matches.
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
_GITHUB_PR_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


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


def _auto_detect_source(ticket: str) -> str | None:
    """Match common key shapes — Jira (`ABC-123`) and GitHub PR
    (`owner/repo#123`). Returns None when neither matches; callers fall back
    to a workspace scan."""
    if _GITHUB_PR_RE.match(ticket):
        return "github"
    if _JIRA_KEY_RE.match(ticket):
        return "jira"
    return None


def _scan_workspace_sources(ticket: str, root: Path = WORKSPACES_ROOT) -> list[str]:
    """Return source subdirs where this ticket's sanitized workspace exists.

    Used as a last-resort disambiguator when auto-detect fails. Multiple
    matches means the caller must pass `--source` explicitly.
    """
    if not root.exists():
        return []
    sanitized = sanitize_key(ticket)
    matches: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if (entry / sanitized).is_dir():
            matches.append(entry.name)
    return matches


def resolve_source(ticket: str, explicit: str | None = None) -> str:
    """Resolve which source a ticket belongs to. Order:
      1. `explicit` (from `--source` flag) — used as-is
      2. Auto-detect by key shape
      3. Workspace scan — unique match only

    Raises `ValueError` when ambiguous or undetermined so callers can write a
    pointed error message at the CLI boundary.
    """
    if explicit:
        return explicit
    detected = _auto_detect_source(ticket)
    if detected:
        return detected
    matches = _scan_workspace_sources(ticket)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"ticket {ticket!r} found in multiple sources ({sorted(matches)}); "
            f"pass --source <name> explicitly"
        )
    raise ValueError(
        f"cannot determine source for ticket {ticket!r}; pass --source <jira|github>"
    )


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
            "See symphony.yaml.example, symphony-prompt-jira.md.example, and "
            "symphony-prompt-github.md.example at the repo root.\n"
        )
        return 2

    _setup_logging()
    logger.info("claude-symphony: config=%s", config_path)

    try:
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_takeover(ticket: str, source: str | None = None) -> int:
    """Print the interactive resume command for a ticket's session."""
    try:
        resolved_source = resolve_source(ticket, explicit=source)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    workspace = ensure_workspace(ticket, source=resolved_source)
    session_uuid = session_uuid_for(ticket, source=resolved_source)
    backend = get_backend()
    cmd = backend.takeover_command(workspace, session_uuid)

    if cmd is None:
        sys.stderr.write(
            f"no session yet for {ticket} (source={resolved_source}, "
            f"workspace={workspace}, uuid={session_uuid})\n"
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


def _cmd_watch(ticket: str, source: str | None = None) -> int:
    """Tail the live session JSONL and print formatted events as they appear."""
    from rich.console import Console

    try:
        resolved_source = resolve_source(ticket, explicit=source)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    workspace = ensure_workspace(ticket, source=resolved_source)
    session_uuid = session_uuid_for(ticket, source=resolved_source)
    backend = get_backend()
    log_path = backend.session_log_path(workspace, session_uuid)

    if log_path is None:
        sys.stderr.write(
            f"no session log for {ticket} (source={resolved_source}, "
            f"workspace={workspace}, uuid={session_uuid})\n"
        )
        sys.stderr.write(
            "Either the daemon hasn't run a turn yet, or this backend "
            "doesn't expose a streamable session log.\n"
        )
        return 1

    console = Console()
    console.print(
        f"[dim]watching {log_path} (source={resolved_source}) — Ctrl-C to stop[/dim]"
    )
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
            "Long-running daemon that polls trackers and runs Claude Code in "
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
    takeover_parser.add_argument(
        "ticket", help="Ticket key (e.g. PROJ-123, owner/repo#456)"
    )
    takeover_parser.add_argument(
        "--source",
        default=None,
        help="Tracker source (jira, github). Auto-detected from key shape if omitted.",
    )

    watch_parser = sub.add_parser(
        "watch",
        help="Tail a running ticket's session JSONL and print formatted events",
    )
    watch_parser.add_argument(
        "ticket", help="Ticket key (e.g. PROJ-123, owner/repo#456)"
    )
    watch_parser.add_argument(
        "--source",
        default=None,
        help="Tracker source (jira, github). Auto-detected from key shape if omitted.",
    )

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
        return _cmd_takeover(args.ticket, source=args.source)

    if args.cmd == "watch":
        return _cmd_watch(args.ticket, source=args.source)

    return _cmd_run(Path(args.config).expanduser())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
