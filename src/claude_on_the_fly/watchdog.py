"""One-shot daemon health check for an external scheduler.

The watchdog does not install or run a scheduler. launchd, systemd, cron, or a
container platform invokes it periodically; unhealthy recovery goes through the
same serialized supervisor restart as an operator action.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_on_the_fly import checks, settings
from claude_on_the_fly.heartbeat import STATE_DIR, process_exists
from claude_on_the_fly.tui import supervisor

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_STALE_S = 90.0
DEFAULT_LIMIT_GRACE_S = 120.0


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def _positive_setting(name: str, fallback: float) -> float:
    raw = settings.get(name).strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, fallback)
        return fallback
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s=%r must be positive; using %s", name, raw, fallback)
        return fallback
    return value


def heartbeat_stale_s() -> float:
    return _positive_setting(
        "WATCHDOG_HEARTBEAT_STALE_SECONDS", DEFAULT_HEARTBEAT_STALE_S
    )


def limit_grace_s() -> float:
    return _positive_setting("WATCHDOG_LIMIT_GRACE_SECONDS", DEFAULT_LIMIT_GRACE_S)


def _heartbeat_age(payload: dict[str, Any], now: datetime) -> float | None:
    value = payload.get("last_heartbeat")
    if not isinstance(value, str):
        return None
    try:
        last = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - last).total_seconds()


def diagnose(
    payload: dict[str, Any] | None,
    *,
    now: datetime,
    stale_s: float = DEFAULT_HEARTBEAT_STALE_S,
    timeout_grace_s: float = DEFAULT_LIMIT_GRACE_S,
    process_check=process_exists,
) -> Decision:
    """Classify one heartbeat without mutating process state."""
    if payload is None:
        return Decision("restart", "heartbeat_missing")
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return Decision("restart", "heartbeat_pid_invalid")
    age = _heartbeat_age(payload, now)
    if age is None:
        return Decision("restart", "heartbeat_timestamp_invalid")
    if not process_check(pid):
        return Decision("restart", "daemon_process_missing")
    if age > stale_s:
        return Decision("restart", f"heartbeat_stale:{age:.0f}s")

    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        return Decision("restart", "heartbeat_extra_invalid")
    rows = extra.get("running_jobs") or []
    if not isinstance(rows, list):
        return Decision("restart", "running_jobs_invalid")
    for row in rows:
        if not isinstance(row, dict):
            continue
        timeout = row.get("timeout_s")
        if timeout is None:
            continue
        try:
            timeout_s = float(timeout)
            uptime_s = float(row.get("uptime_s") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            continue
        limit = timeout_s + timeout_grace_s
        if uptime_s > limit:
            mode = "background" if row.get("background") else "foreground"
            return Decision(
                "restart", f"{mode}_turn_over_limit:{uptime_s:.0f}s>{limit:.0f}s"
            )
    return Decision("healthy", "ok")


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_once(
    *,
    frontend: str,
    state_dir: Path = STATE_DIR,
    dry_run: bool = False,
) -> Decision:
    """Check one daemon and clean-restart it when unhealthy."""
    if frontend not in checks.SUPERVISABLE_FRONTENDS:
        raise ValueError(f"unknown frontend: {frontend!r}")
    decision = diagnose(
        _read_payload(state_dir / f"{frontend}.json"),
        now=datetime.now(UTC),
        stale_s=heartbeat_stale_s(),
        timeout_grace_s=limit_grace_s(),
    )
    if decision.action == "healthy" or dry_run:
        logger.info("watchdog: %s (%s)", decision.action, decision.reason)
        return decision
    logger.warning(
        "watchdog: frontend=%s action=%s reason=%s",
        frontend,
        decision.action,
        decision.reason,
    )
    try:
        supervisor.restart(frontend)
    except supervisor.RestartInProgress:
        logger.info("watchdog: restart already in progress; skipping this tick")
        return Decision("healthy", "restart_in_progress")
    return decision


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="claude-watchdog")
    parser.add_argument(
        "--frontend",
        default="slack",
        choices=checks.SUPERVISABLE_FRONTENDS,
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        run_once(frontend=args.frontend, dry_run=args.dry_run)
    except supervisor.SupervisorError as exc:
        logger.error("watchdog: %s", exc)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
