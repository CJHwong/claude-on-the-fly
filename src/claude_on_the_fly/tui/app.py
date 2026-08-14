"""Entry point for `claude-tui`.

Subcommands:
    claude-tui                       interactive dashboard (Textual)
    claude-tui status [--json]       one-shot snapshot, exits 0
    claude-tui start <frontend>      spawn a detached daemon
    claude-tui stop <frontend>       SIGTERM then SIGKILL
    claude-tui restart <frontend>    stop + spawn
    claude-tui stop-all              stop every running daemon
    claude-tui resume                respawn whatever stop-all stopped
    claude-tui upgrade               stop, fetch new code, start again

Every stop reports what it is about to interrupt first, and gives the daemon
long enough to tell whoever was waiting. `--force` is the old immediate kill.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from claude_on_the_fly import upgrade
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


def _grace(force: bool) -> float:
    """The stop grace. --force is the old 5s kill; otherwise leave room for the
    daemon to tell whoever is waiting that their work just died."""
    return supervisor.FORCE_GRACE_S if force else supervisor.SAFE_GRACE_S


def _report_pending(items: list[supervisor.PendingWork]) -> None:
    """Print what a stop is about to interrupt. Silent when it is nothing."""
    if not items:
        return
    print("pending work:")
    for item in items:
        print(f"  {item.describe()}")


def _report_one(frontend: str) -> None:
    """Report one daemon's pending work, if it has any."""
    pending = supervisor.pending_work(frontend)
    _report_pending([pending] if pending else [])


def cmd_stop(frontend: str, *, force: bool) -> int:
    _report_one(frontend)
    try:
        pid = supervisor.stop(frontend, grace_s=_grace(force))
    except supervisor.NotRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"stopped {frontend} (pid {pid})")
    return 0


def cmd_stop_all(*, force: bool) -> int:
    _report_pending(supervisor.all_pending_work())
    stopped = supervisor.stop_all(grace_s=_grace(force))
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


def cmd_restart(frontend: str, env_file: Path | None, *, force: bool) -> int:
    _report_one(frontend)
    try:
        pid = supervisor.restart(frontend, env_file=env_file, grace_s=_grace(force))
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


def _confirm(question: str, reader=input) -> bool:
    """Ask, default no. A closed stdin answers no rather than crashing."""
    try:
        answer = reader(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def cmd_upgrade(
    env_file: Path | None,
    *,
    force: bool,
    assume_yes: bool,
    do_resume: bool,
    reader=input,
) -> int:
    """Stop the daemons, fetch new code, start them again.

    The daemons come back even when the upgrade command failed: the alternative
    is leaving an operator with everything down and old code still on disk.
    """
    try:
        plan = upgrade.resolve()
    except upgrade.UnknownInstall as exc:
        print(f"upgrade: {exc}", file=sys.stderr)
        return 2
    print(f"upgrade: {upgrade.describe(plan)}")

    pending = supervisor.all_pending_work()
    _report_pending(pending)
    at_risk = sum(item.at_risk for item in pending)
    if not assume_yes:
        question = (
            f"stop everything ({at_risk} unrecoverable) and upgrade?"
            if at_risk
            else "stop everything and upgrade?"
        )
        if not _confirm(question, reader):
            print("upgrade: cancelled, nothing was stopped")
            return 1

    for name, pid in supervisor.stop_all(grace_s=_grace(force)):
        print(f"stopped {name} (pid {pid})")

    rc = upgrade.run(plan)
    if rc != 0:
        print(
            f"upgrade: command failed (exit {rc}) — restarting on the old code",
            file=sys.stderr,
        )
    if do_resume:
        rc = cmd_resume(env_file) or rc
    return rc


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


def _add_force_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            f"SIGKILL after {supervisor.FORCE_GRACE_S:.0f}s instead of "
            f"{supervisor.SAFE_GRACE_S:.0f}s, cutting off the notices a daemon "
            "sends to whoever was waiting on it"
        ),
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
    _add_force_arg(stop)

    restart = sub.add_parser("restart", help="Stop then spawn")
    _add_frontend_arg(restart)
    _add_env_arg(restart)
    _add_force_arg(restart)

    stop_all = sub.add_parser("stop-all", help="Stop every running daemon")
    _add_force_arg(stop_all)

    resume = sub.add_parser("resume", help="Respawn whatever stop-all stopped")
    _add_env_arg(resume)

    upgrade_cmd = sub.add_parser(
        "upgrade", help="Stop the daemons, fetch new code, start them again"
    )
    _add_env_arg(upgrade_cmd)
    _add_force_arg(upgrade_cmd)
    upgrade_cmd.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    upgrade_cmd.add_argument(
        "--no-resume",
        action="store_true",
        help="Leave the daemons stopped after upgrading",
    )

    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(json_output=args.json)
    if args.cmd == "start":
        return cmd_start(args.frontend, args.env_file)
    if args.cmd == "stop":
        return cmd_stop(args.frontend, force=args.force)
    if args.cmd == "restart":
        return cmd_restart(args.frontend, args.env_file, force=args.force)
    if args.cmd == "stop-all":
        return cmd_stop_all(force=args.force)
    if args.cmd == "resume":
        return cmd_resume(args.env_file)
    if args.cmd == "upgrade":
        return cmd_upgrade(
            args.env_file,
            force=args.force,
            assume_yes=args.yes,
            do_resume=not args.no_resume,
        )
    return cmd_interactive()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
