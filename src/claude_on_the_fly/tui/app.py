"""Entry point for `claude-tui`.

Subcommands:
    claude-tui                       interactive dashboard (Textual)
    claude-tui status [--json]       one-shot snapshot, exits 0
    claude-tui start <frontend>      spawn a detached daemon
    claude-tui stop <frontend>       SIGTERM then SIGKILL
    claude-tui restart <frontend>    stop + spawn
    claude-tui stop-all              stop every running daemon
    claude-tui resume                respawn whatever stop-all stopped
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.tui import render, state, supervisor


def cmd_status(*, json_output: bool) -> int:
    snap = state.snapshot()
    if json_output:
        print(render.render_snapshot_json(snap))
    else:
        render.render_snapshot_rich(snap)
    return 0


def cmd_start(frontend: str, env_file: Path | None) -> int:
    try:
        pid = supervisor.spawn(frontend, env_file=env_file)
    except supervisor.AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except supervisor.PreflightFailed as exc:
        print(f"preflight failed for {frontend}:", file=sys.stderr)
        for r in exc.results:
            if r.status != "ok":
                hint = f"  hint: {r.fix_hint}" if r.fix_hint else ""
                print(f"  {r.name}: {r.status} — {r.detail}", file=sys.stderr)
                if hint:
                    print(hint, file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"started {frontend} (pid {pid})")
    return 0


def cmd_stop(frontend: str) -> int:
    try:
        pid = supervisor.stop(frontend)
    except supervisor.NotRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"stopped {frontend} (pid {pid})")
    return 0


def cmd_stop_all() -> int:
    stopped = supervisor.stop_all()
    if not stopped:
        print("nothing running")
        return 0
    for name, pid in stopped:
        print(f"stopped {name} (pid {pid})")
    print(f"\n{len(stopped)} daemon(s) stopped. Use `claude-tui resume` to restart.")
    return 0


def cmd_resume(env_file: Path | None) -> int:
    results = supervisor.resume(env_file=env_file)
    if not results:
        print("nothing to resume (no last-running record found)")
        return 0
    failed = 0
    for name, pid, exc in results:
        if exc is None:
            print(f"started {name} (pid {pid})")
        else:
            failed += 1
            print(f"failed {name}: {exc}", file=sys.stderr)
    return 0 if failed == 0 else 2


def cmd_restart(frontend: str, env_file: Path | None) -> int:
    try:
        pid = supervisor.restart(frontend, env_file=env_file)
    except supervisor.PreflightFailed as exc:
        print(f"preflight failed for {frontend}:", file=sys.stderr)
        for r in exc.results:
            if r.status != "ok":
                print(f"  {r.name}: {r.status} — {r.detail}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"restarted {frontend} (pid {pid})")
    return 0


def cmd_interactive() -> int:
    try:
        from claude_on_the_fly.tui.tui_app import run_app
    except ImportError as exc:
        print(
            f"Interactive mode unavailable ({exc}). Falling back to status.",
            file=sys.stderr,
        )
        return cmd_status(json_output=False)
    run_app()
    return 0


def _add_frontend_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "frontend",
        choices=SUPERVISABLE_FRONTENDS,
        help="Which frontend to act on",
    )


def _add_env_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--env-file",
        type=Path,
        default=supervisor.DEFAULT_ENV_FILE,
        help=f"Env file path (default: {supervisor.DEFAULT_ENV_FILE})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-tui",
        description="Unified dashboard for claude-on-the-fly frontends.",
    )
    sub = parser.add_subparsers(dest="cmd")

    status = sub.add_parser("status", help="Print a snapshot and exit")
    status.add_argument(
        "--json", action="store_true", help="Output JSON instead of a table"
    )

    start = sub.add_parser("start", help="Spawn a detached daemon")
    _add_frontend_arg(start)
    _add_env_arg(start)

    stop = sub.add_parser("stop", help="Signal a daemon to exit")
    _add_frontend_arg(stop)

    restart = sub.add_parser("restart", help="Stop then spawn")
    _add_frontend_arg(restart)
    _add_env_arg(restart)

    sub.add_parser("stop-all", help="Stop every running daemon")

    resume = sub.add_parser("resume", help="Respawn whatever stop-all stopped")
    _add_env_arg(resume)

    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(json_output=args.json)
    if args.cmd == "start":
        return cmd_start(args.frontend, args.env_file)
    if args.cmd == "stop":
        return cmd_stop(args.frontend)
    if args.cmd == "restart":
        return cmd_restart(args.frontend, args.env_file)
    if args.cmd == "stop-all":
        return cmd_stop_all()
    if args.cmd == "resume":
        return cmd_resume(args.env_file)
    return cmd_interactive()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
