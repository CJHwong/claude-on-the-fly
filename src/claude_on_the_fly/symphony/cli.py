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
import re
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from claude_on_the_fly.agent import current_backend_key, get_backend
from claude_on_the_fly.preflight import setup_daemon_logging

from . import orchestrator, watch
from .agent_runner import session_uuid_for
from .workspace import WORKSPACES_ROOT, ensure_workspace, sanitize_key

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
DEFAULT_CONFIG = DATA_DIR / "symphony.yaml"

SUBCOMMANDS = ("run", "takeover", "watch", "config", "doctor")

# Auto-detect heuristics. Conservative — falls through to workspace scan if
# neither matches.
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
_GITHUB_PR_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


def _setup_logging(platform: str = "symphony") -> None:
    """Console respects LOG_LEVEL; file always DEBUG with daily rotation, 7-day retention."""
    setup_daemon_logging(platform)


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


def _cmd_run(config_path: Path, *, once: bool = False) -> int:
    if not config_path.exists():
        sys.stderr.write(f"config not found: {config_path}\n")
        sys.stderr.write(
            "See symphony.yaml.example, symphony-prompt-jira.md.example, and "
            "symphony-prompt-github.md.example at the repo root.\n"
        )
        return 2

    _setup_logging()
    logger.info("claude-symphony: config=%s once=%s", config_path, once)

    if once:
        return _cmd_run_once(config_path)

    try:
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_run_once(config_path: Path) -> int:
    """Single poll tick + exit. Useful for debugging config / tracker auth."""

    async def _one_tick() -> int:
        from .config import load_config
        from .orchestrator import (
            _build_cursor_stores,
            _refresh_prompt_stores,
            make_trackers,
            tick as _tick,
        )
        from claude_on_the_fly.events import EventLog
        from .retry import RetryQueue
        from .state import OrchestratorState
        from .. import agent as _agent_mod

        cfg = load_config(config_path)
        cfg.validate()
        trackers = make_trackers(cfg)
        state = OrchestratorState()
        retry_queue = RetryQueue(event_log=EventLog())
        prompts: dict = {}
        _refresh_prompt_stores(prompts, cfg)
        cursors = _build_cursor_stores(cfg, _agent_mod.DATA_DIR / "symphony" / "state")
        await _tick(
            state,
            cfg,
            prompts,
            trackers,
            retry_queue,
            EventLog(),
            set(),
            cursor_stores=cursors,
        )
        sys.stdout.write(f"one tick complete. running={state.running_count()}\n")
        return 0

    try:
        return asyncio.run(_one_tick())
    except KeyboardInterrupt:
        return 130


def _cmd_config_show(config_path: Path) -> int:
    """Print the effective config as YAML."""
    from .config import dump_effective_config, load_config

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        sys.stderr.write(f"config load failed: {exc}\n")
        return 2
    sys.stdout.write(dump_effective_config(cfg))
    return 0


def _cmd_doctor() -> int:
    """Run preflight checks for symphony's tool dependencies."""
    from claude_on_the_fly.checks import check_all

    groups = check_all()
    failed = 0
    for name, results in groups.items():
        sys.stdout.write(f"\n[{name}]\n")
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


def _cmd_takeover(ticket: str, source: str | None = None) -> int:
    """Print the interactive resume command for a ticket's session."""
    try:
        resolved_source = resolve_source(ticket, explicit=source)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    workspace = ensure_workspace(ticket, source=resolved_source)
    session_uuid = session_uuid_for(
        ticket, source=resolved_source, backend_key=current_backend_key()
    )
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
    session_uuid = session_uuid_for(
        ticket, source=resolved_source, backend_key=current_backend_key()
    )
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
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll tick then exit (debug).",
    )

    config_parser = sub.add_parser("config", help="Inspect the effective config")
    config_sub = config_parser.add_subparsers(dest="config_cmd", required=True)
    show_parser = config_sub.add_parser(
        "show", help="Dump the effective config to stdout (YAML)"
    )
    show_parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))

    sub.add_parser(
        "doctor",
        help="Run symphony's preflight checks (acli/gh/config/binaries)",
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

    if args.cmd == "config":
        path = Path(args.config).expanduser()
        if args.config_cmd == "show":
            return _cmd_config_show(path)
        return 2

    if args.cmd == "doctor":
        return _cmd_doctor()

    return _cmd_run(Path(args.config).expanduser(), once=getattr(args, "once", False))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
