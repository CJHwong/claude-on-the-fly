#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil>=5.9"]
# ///
"""Exit 1 when CPU or memory stays high, so the cron daemon's failure alert fires.

How to make it work:
  1. Install uv (https://docs.astral.sh/uv/). It installs psutil on the first run.
  2. Set `slack.alert_target` or `telegram.alert_target` in
     ~/.claude-on-the-fly/config.yaml, and restart claude-cron. Without one, an alert
     only reaches the entry's log.
  3. Copy the entry in cron.yaml, next to this file, into
     ~/.claude-on-the-fly/cron.yaml.
  4. Optional: pass --recovery-command to hear when usage drops again. The cron daemon
     alerts only on failure, so recovery needs its own sender, for example
       --recovery-command 'slacker.sh send @U0123 "$RESOURCE_WATCH_MESSAGE"'
  Run it by hand once to check: it prints nothing and exits 0 on a quiet host.

No agent runs and no Slack code is needed for the alert itself. A state file in
~/.claude-on-the-fly/state/ keeps three things quiet that a plain threshold would not:
  - a short spike: usage must stay at or above --threshold for --sustain-seconds;
  - a repeat: after an alert, the next one waits --reminder-seconds;
  - flapping: the alert clears only once usage drops below --recovery-threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

STATE = Path.home() / ".claude-on-the-fly" / "state" / "resource-watch.json"


def sample() -> dict[str, float]:
    return {
        "cpu": psutil.cpu_percent(interval=1.0),
        "memory": psutil.virtual_memory().percent,
    }


def step(
    state: dict, usage: dict[str, float], now: float, args
) -> tuple[dict, str | None, str | None]:
    """The next state, the alert text, and the recovery text, for this run."""
    peak = max(usage, key=lambda name: usage[name])
    reading = ", ".join(f"{name} {value:.0f}%" for name, value in usage.items())
    if usage[peak] < args.recovery_threshold:
        recovered = f"recovered: {reading}" if "alerted_at" in state else None
        return {}, None, recovered
    if usage[peak] < args.threshold:
        # Between the two thresholds: an open alert stays open, a pending one waits.
        return state, None, None
    high_since = state.get("high_since", now)
    alerted_at = state.get("alerted_at")
    state = {**state, "high_since": high_since}
    if now - high_since < args.sustain_seconds:
        return state, None, None
    if alerted_at is not None and now - alerted_at < args.reminder_seconds:
        return state, None, None
    minutes = (now - high_since) / 60
    alert = f"{reading}; {peak} high for {minutes:.0f} min"
    return {**state, "alerted_at": now}, alert, None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--threshold", type=float, default=90)
    parser.add_argument("--recovery-threshold", type=float, default=80)
    parser.add_argument("--sustain-seconds", type=float, default=300)
    parser.add_argument("--reminder-seconds", type=float, default=3600)
    parser.add_argument(
        "--recovery-command",
        help="shell command run once when an alert clears; "
        "the text is in $RESOURCE_WATCH_MESSAGE",
    )
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    if args.recovery_threshold > args.threshold:
        parser.error("--recovery-threshold must not exceed --threshold")
    return args


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"ignoring unreadable state {path}: {exc}", file=sys.stderr)
        return {}


def notify_recovery(command: str, message: str) -> int:
    """Run the recovery command; a failure exits 1 so the daemon alerts about it."""
    env = {**os.environ, "RESOURCE_WATCH_MESSAGE": message}
    result = subprocess.run(command, shell=True, env=env, timeout=60, check=False)
    if result.returncode != 0:
        print(f"{message}; --recovery-command exited {result.returncode}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state, alert, recovered = step(load(args.state), sample(), time.time(), args)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state))
    if alert:
        print(alert)
        return 1
    if recovered:
        print(recovered, flush=True)  # before the command's own output in the log
        if args.recovery_command:
            return notify_recovery(args.recovery_command, recovered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
