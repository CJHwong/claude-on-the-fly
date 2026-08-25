"""Unattended recovery for a daemon that is running and no longer answering.

Everything else that can restart a daemon needs somebody watching. `heartbeat`
writes the state, `tui.state` reads it, and the dashboard colours the cell red,
but nothing acts on it. A daemon that wedges at 03:00 stays wedged until a
person opens the TUI.

This is a one-shot check. An external scheduler (launchd, systemd, cron, a
container platform) invokes it, it decides, and it exits. Installing that timer
is the operator's job and stays outside the package: a daemon supervisor that
installs its own supervisor has two of them.

Scope is deliberately one state. `broken` means the process is alive and its
heartbeat has gone stale, which is never something an operator asked for, so
recovering it cannot fight one.

A `stopped` daemon is left alone on purpose, and that is the interesting half of
the design. Nothing on disk distinguishes a crash from `claude-tui stop slack`,
or from the window inside `claude-tui upgrade` between `stop_all()` and
`resume()`. A watchdog that started stopped daemons would undo a deliberate stop
on its next tick, and during an upgrade it would spawn the daemon from
half-replaced code while racing the upgrade's own resume. Covering that safely
needs a durable record of intent, which does not exist yet:
`supervisor.read_last_running` is written only by `stop_all`, so it cannot speak
for a single `stop`. Recovering a crashed daemon is worth having and belongs in
the change that adds that record, with the upgrade window handled explicitly.

Recovery goes through `supervisor.restart`, the same path an operator's restart
takes, rather than a private stop/spawn pair.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_on_the_fly import checks, settings
from claude_on_the_fly.tui import state as tui_state
from claude_on_the_fly.tui import supervisor

logger = logging.getLogger(__name__)

# How long a live process may go without a heartbeat before this restarts it.
#
# Deliberately not `tui.state.STALENESS_S` (15s), which is tuned for a 1Hz
# dashboard: showing a cell amber a few seconds early costs nothing, and
# restarting a daemon a few seconds early costs every turn in flight. The
# comment on that table says the heartbeat coroutine can be starved by a poll
# cadence or a tracker call, so an acting threshold has to tolerate what a
# displaying one only has to notice. At a 5s write interval this is 18
# consecutive misses.
DEFAULT_STALE_S = 90.0

STALE_SETTING = "WATCHDOG_STALE_SECONDS"


@dataclass(frozen=True)
class Decision:
    """What this tick did, and why.

    `action` is the machine-readable half: a scheduler or a log alert keys on
    it, so a tick that skipped recovery never reports itself as healthy.
    """

    action: str
    reason: str


def stale_after_s() -> float:
    raw = settings.get(STALE_SETTING, "").strip()
    if not raw:
        return DEFAULT_STALE_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %s", STALE_SETTING, raw, DEFAULT_STALE_S
        )
        return DEFAULT_STALE_S
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s=%r must be positive; using %s", STALE_SETTING, raw, DEFAULT_STALE_S
        )
        return DEFAULT_STALE_S
    return value


def diagnose(status: tui_state.FrontendStatus, *, stale_after: float) -> Decision:
    """Classify one frontend without touching process state."""
    if status.state != "broken":
        return Decision("skip", f"state:{status.state}")
    age = status.last_heartbeat_age_s
    if age is None:  # pragma: no cover - `broken` always carries an age
        return Decision("skip", "state:broken_without_age")
    if age < stale_after:
        return Decision("skip", f"stale_below_threshold:{age:.0f}s<{stale_after:.0f}s")
    return Decision("restart", f"heartbeat_stale:{age:.0f}s")


def run_once(
    *,
    frontend: str,
    state_dir: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    process_check: Callable[[int], bool] | None = None,
) -> Decision:
    """Check one daemon and restart it when it is wedged.

    `process_check` mirrors the seam `tui_state.snapshot` already exposes,
    forwarded rather than reinvented so a test can describe a live process
    without one existing.
    """
    if frontend not in checks.SUPERVISABLE_FRONTENDS:
        raise ValueError(f"unknown frontend: {frontend!r}")

    snapshot = tui_state.snapshot(
        state_dir,
        now=now or datetime.now(UTC),
        process_check=process_check or tui_state.process_exists,
    )
    status = next(f for f in snapshot.frontends if f.name == frontend)
    decision = diagnose(status, stale_after=stale_after_s())

    if decision.action == "skip":
        logger.info("watchdog: %s %s (%s)", frontend, decision.action, decision.reason)
        return decision
    if dry_run:
        logger.warning(
            "watchdog: %s would restart (%s); dry run, nothing done",
            frontend,
            decision.reason,
        )
        return Decision("dry_run", decision.reason)

    logger.warning("watchdog: restarting %s (%s)", frontend, decision.reason)
    supervisor.restart(frontend)
    return decision


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="claude-watchdog",
        description="Restart a daemon that is running but no longer heartbeating.",
    )
    parser.add_argument(
        "--frontend", default="slack", choices=sorted(checks.SUPERVISABLE_FRONTENDS)
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen and change nothing",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        run_once(frontend=args.frontend, dry_run=args.dry_run)
    except supervisor.SupervisorError as exc:
        # One line and exit 2, the shape every other operator-facing refusal in
        # this package uses. A scheduler logs stderr; a traceback in a cron mail
        # is noise around a sentence.
        logger.error("watchdog: %s", exc)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
