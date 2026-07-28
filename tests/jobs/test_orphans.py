"""ProcessLedger: durably records agent process groups so a SIGKILLed worker's
orphans can be reaped, and refuses to signal a pid the OS has recycled."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from claude_on_the_fly.jobs.orphans import ProcessLedger, _same_command


def _ledger(tmp_path: Path) -> ProcessLedger:
    return ProcessLedger(tmp_path / "jobs" / "worker.pids")


def _detached_sleeper() -> subprocess.Popen:
    """A process in its OWN session, exactly like agent._exec spawns.

    Not reachable from this process's group, which is the whole reason the
    ledger has to exist: killing our own group would not touch it.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestRecordAndForget:
    def test_record_then_forget_leaves_nothing_to_sweep(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.record(4242, "claude")
        assert ledger._entries() == [(4242, "claude")]

        ledger.forget(4242)
        assert ledger._entries() == []

    def test_forget_leaves_other_groups_recorded(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.record(1, "claude")
        ledger.record(2, "codex")

        ledger.forget(1)

        assert ledger._entries() == [(2, "codex")]

    def test_listener_shape_matches_agent_announcements(self, tmp_path: Path) -> None:
        """on_process is registered with agent.add_process_listener, so its
        signature is a contract with agent._announce_process."""
        ledger = _ledger(tmp_path)
        ledger.on_process(99, "claude", True)
        assert ledger._entries() == [(99, "claude")]

        ledger.on_process(99, "claude", False)
        assert ledger._entries() == []

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.path.parent.mkdir(parents=True)
        ledger.path.write_text(
            "not json\n"
            + json.dumps({"pgid": "abc", "command": "claude"})
            + "\n"
            + json.dumps({"pgid": 7, "command": "claude"})
            + "\n"
        )
        assert ledger._entries() == [(7, "claude")]

    def test_missing_ledger_reads_as_empty(self, tmp_path: Path) -> None:
        assert _ledger(tmp_path)._entries() == []
        assert _ledger(tmp_path).sweep() == 0


class TestSweep:
    def test_kills_a_real_orphaned_process_group(self, tmp_path: Path) -> None:
        """The failure this exists for: a process in its own session, which no
        signal to the worker's own group could ever reach."""
        proc = _detached_sleeper()
        ledger = _ledger(tmp_path)
        ledger.record(proc.pid, sys.executable)

        try:
            assert ledger.sweep() == 1
            # Wait for the exit status rather than probing with kill(pid, 0):
            # this test is the process's parent, so an unreaped zombie still
            # answers that probe (the trap supervisor.py:192 documents). In
            # production the orphan's parent is dead and init reaps it.
            assert proc.wait(timeout=5) == -signal.SIGKILL
        finally:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

    def test_sweep_clears_the_ledger(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.record(999_999, "claude")  # long gone

        ledger.sweep()

        assert ledger._entries() == []

    def test_refuses_to_kill_a_recycled_pid(self, tmp_path: Path) -> None:
        """A pid the OS reassigned must survive: being wrong here kills a
        stranger's process, while a missed orphan is only the status quo."""
        proc = _detached_sleeper()
        ledger = _ledger(tmp_path)
        ledger.record(proc.pid, "some-other-agent-cli")

        try:
            assert ledger.sweep() == 0
            os.kill(proc.pid, 0)  # still alive: raises if it was killed
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def test_dead_pid_is_not_counted_as_killed(self, tmp_path: Path) -> None:
        proc = _detached_sleeper()
        pid = proc.pid
        os.killpg(pid, signal.SIGKILL)
        proc.wait(timeout=5)

        ledger = _ledger(tmp_path)
        ledger.record(pid, sys.executable)

        assert ledger.sweep() == 0


class TestSameCommand:
    def test_matches_bare_name_against_full_path(self) -> None:
        # Recorded as resolved through PATH, reported by ps as a full path.
        assert _same_command("claude", "/opt/homebrew/bin/claude")

    def test_matches_linux_truncated_comm(self) -> None:
        # Linux truncates comm to 15 characters.
        assert _same_command("claude-code-cli-long", "claude-code-cli")

    def test_rejects_a_different_command(self) -> None:
        assert not _same_command("claude", "postgres")

    def test_rejects_empty(self) -> None:
        assert not _same_command("", "claude")
        assert not _same_command("claude", "")
