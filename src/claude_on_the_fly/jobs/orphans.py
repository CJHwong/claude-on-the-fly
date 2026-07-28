"""Durable record of the agent process groups a worker owns, so a worker that
dies without unwinding can reap what it left behind.

The hole this fills: `agent._exec` spawns the CLI with `start_new_session=True`,
deliberately — the child leads its own process group, so one `killpg` reaps the
CLI *and* every tool subprocess it forked. The cost is that the child is no
longer reachable from the parent's group. `supervisor.stop()` signals the worker
pid alone and SIGKILLs it after a five-second grace, so a worker that misses
that window dies with a full agent CLI still running under `bypassPermissions`,
with no parent, and with the only record of its pid in the memory of the process
that was just killed.

So the pid goes to disk the moment the group exists, and comes off when it is
reaped. What survives a SIGKILL is a file naming exactly what was orphaned.

Sweeping is the dangerous half — a pid the OS has recycled onto something
unrelated must not be killed — so `sweep` refuses to signal a group whose
current command does not match what was recorded. Being wrong here means
killing a stranger's process, and a missed orphan is merely the status quo.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_NAME = "worker.pids"
# `ps` is asked about at most a handful of leftovers, only at startup.
PS_TIMEOUT_S = 5.0


def _process_command(pid: int) -> str | None:
    """The command backing `pid` right now, or None if it is gone/unreadable."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("jobs: could not ask ps about pid %d: %s", pid, exc)
        return None
    if result.returncode != 0:
        return None  # no such process
    return result.stdout.strip() or None


def _same_command(recorded: str, actual: str) -> bool:
    """Whether `actual` still looks like the `recorded` command.

    Compared on basenames, since a command may be recorded as a bare name
    resolved through PATH and reported by `ps` as a full path. Linux truncates
    `comm` to 15 characters, so a prefix counts — the check exists to catch a
    recycled pid running something else entirely, not to be a fingerprint.
    """
    recorded_name = os.path.basename(recorded.strip())
    actual_name = os.path.basename(actual.strip())
    if not recorded_name or not actual_name:
        return False
    return recorded_name.startswith(actual_name) or actual_name.startswith(
        recorded_name
    )


@dataclass
class ProcessLedger:
    """Agent process groups this worker has running, persisted to `path`.

    Single-writer by construction: `claude-jobs` refuses to start beside a live
    worker, so there is no locking here and none needed.
    """

    path: Path

    # -- agent.ProcessListener -------------------------------------------

    def on_process(self, pgid: int, command: str, running: bool) -> None:
        """Listener for `agent.add_process_listener`."""
        if running:
            self.record(pgid, command)
        else:
            self.forget(pgid)

    # -- writes -----------------------------------------------------------

    def record(self, pgid: int, command: str) -> None:
        """Append a live process group. Best-effort: failing to write the
        ledger must not take down the job that is about to run."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"pgid": pgid, "command": command}) + "\n")
        except OSError as exc:
            logger.warning("jobs: could not record process group %d: %s", pgid, exc)

    def forget(self, pgid: int) -> None:
        """Drop a reaped process group."""
        remaining = [entry for entry in self._entries() if entry[0] != pgid]
        self._rewrite(remaining)

    # -- startup sweep ------------------------------------------------------

    def sweep(self) -> int:
        """Kill process groups a previous worker left running. Returns the count.

        Called before the queue is touched: `recover_stale` re-runs whatever was
        in flight, and starting a second copy of a job whose first copy is still
        running is the failure this exists to prevent.
        """
        entries = self._entries()
        if not entries:
            return 0
        killed = 0
        for pgid, command in entries:
            actual = _process_command(pgid)
            if actual is None:
                continue  # already gone: the ordinary case after a clean stop
            if not _same_command(command, actual):
                logger.warning(
                    "jobs: pid %d is now %r, not the recorded %r — leaving it alone",
                    pgid,
                    actual,
                    command,
                )
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError) as exc:
                logger.warning("jobs: could not kill orphaned group %d: %s", pgid, exc)
                continue
            killed += 1
            logger.warning(
                "jobs: killed orphaned agent process group %d (%s) left by a "
                "previous worker",
                pgid,
                command,
            )
        self._rewrite([])
        return killed

    # -- storage ------------------------------------------------------------

    def _entries(self) -> list[tuple[int, str]]:
        """Recorded (pgid, command) pairs. Unreadable or malformed lines are
        skipped, never fatal — a corrupt ledger must not stop a worker."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        entries: list[tuple[int, str]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                pgid = int(record["pgid"])
                command = str(record["command"])
            except (ValueError, KeyError, TypeError):
                logger.warning("jobs: skipping malformed ledger line: %r", line)
                continue
            entries.append((pgid, command))
        return entries

    def _rewrite(self, entries: list[tuple[int, str]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "".join(
                    json.dumps({"pgid": pgid, "command": command}) + "\n"
                    for pgid, command in entries
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("jobs: could not rewrite the process ledger: %s", exc)
