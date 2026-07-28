"""Tests for tui.supervisor — spawn / stop / restart with mocked Popen."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_on_the_fly.tui import supervisor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect STATE_DIR and LOG_DIR into tmp_path."""
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    monkeypatch.setattr(supervisor, "STATE_DIR", state)
    monkeypatch.setattr(supervisor, "LOG_DIR", logs)
    return tmp_path


def _write_heartbeat(state_dir: Path, frontend: str, pid: int) -> None:
    payload = {
        "frontend": frontend,
        "pid": pid,
        "started_at": "2026-05-19T13:00:00Z",
        "last_heartbeat": "2026-05-19T13:00:00Z",
        "version": "0.1.0",
        "extra": {},
    }
    (state_dir / f"{frontend}.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# frontend registry
# ---------------------------------------------------------------------------


class TestFrontendModule:
    def test_jobs_runs_the_jobs_cli(self):
        # Acceptance #16: claude-jobs is spawnable via `python -m <module>`.
        assert supervisor._FRONTEND_MODULE["jobs"] == "claude_on_the_fly.jobs.cli"


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    def test_unknown_frontend_raises(self, monkeypatch):
        with pytest.raises(ValueError, match="unknown frontend"):
            supervisor.spawn("nope", env={})

    def test_preflight_failure_refuses_spawn(self, monkeypatch):
        popen = MagicMock()
        with pytest.raises(supervisor.PreflightFailed) as excinfo:
            supervisor.spawn("telegram", env={}, popen_factory=popen)
        assert excinfo.value.frontend == "telegram"
        bad_names = {r.name for r in excinfo.value.results if r.status != "ok"}
        assert "TELEGRAM_BOT_TOKEN" in bad_names
        popen.assert_not_called()

    def test_success_writes_pid_file(self, isolated_state):
        popen = MagicMock(return_value=MagicMock(pid=4242))
        pid = supervisor.spawn(
            "telegram",
            env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"},
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        assert pid == 4242
        pid_file = supervisor.STATE_DIR / "telegram.pid"
        assert pid_file.read_text() == "4242"

    def test_popen_called_detached(self, isolated_state):
        popen = MagicMock(return_value=MagicMock(pid=1234))
        supervisor.spawn(
            "telegram",
            env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"},
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        args, kwargs = popen.call_args
        assert args[0][1] == "-m"
        assert args[0][2] == "claude_on_the_fly.telegram"
        assert kwargs["start_new_session"] is True
        assert "stdout" in kwargs and "stderr" in kwargs

    def test_passes_env_to_popen(self, isolated_state):
        popen = MagicMock(return_value=MagicMock(pid=1234))
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"}
        supervisor.spawn(
            "telegram", env=env, popen_factory=popen, wait_for_heartbeat=False
        )
        passed = popen.call_args.kwargs["env"]
        assert passed["TELEGRAM_BOT_TOKEN"] == "tok"
        assert passed["TELEGRAM_ALLOWED_USER_ID"] == "1"

    def test_refuses_when_already_running(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=9999)
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)
        popen = MagicMock()
        with pytest.raises(supervisor.AlreadyRunning) as excinfo:
            supervisor.spawn(
                "telegram",
                env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"},
                popen_factory=popen,
            )
        assert excinfo.value.pid == 9999
        popen.assert_not_called()

    def test_env_file_overrides_os_environ(self, isolated_state, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TELEGRAM_BOT_TOKEN=from-file\nTELEGRAM_ALLOWED_USER_ID=1\n"
        )
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-os")
        popen = MagicMock(return_value=MagicMock(pid=1))
        supervisor.spawn(
            "telegram",
            env_file=env_file,
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        passed = popen.call_args.kwargs["env"]
        assert passed["TELEGRAM_BOT_TOKEN"] == "from-file"


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_not_running_raises(self, isolated_state):
        with pytest.raises(supervisor.NotRunning):
            supervisor.stop("telegram")

    def test_dead_pid_cleans_up(self, isolated_state, monkeypatch):
        (supervisor.STATE_DIR / "telegram.pid").write_text("9999")
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        with pytest.raises(supervisor.NotRunning):
            supervisor.stop("telegram")
        # Stale PID file cleaned.
        assert not (supervisor.STATE_DIR / "telegram.pid").exists()

    def test_clean_exit_on_sigterm(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1234)
        (supervisor.STATE_DIR / "telegram.pid").write_text("1234")

        # Simulate: process exists, signal kills it instantly.
        exists_calls = [True]  # first liveness check

        def fake_exists(pid):
            v = exists_calls.pop(0) if exists_calls else False
            return v

        monkeypatch.setattr(supervisor, "_process_exists", fake_exists)

        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

        pid = supervisor.stop("telegram", grace_s=0.5)
        assert pid == 1234
        # First call: SIGTERM. SIGKILL should NOT have been sent.
        assert kills[0] == (1234, signal.SIGTERM)
        assert all(sig != signal.SIGKILL for _, sig in kills)
        assert not (supervisor.STATE_DIR / "telegram.pid").exists()
        # Heartbeat gone too, so the TUI reads it stopped right away.
        assert not (supervisor.STATE_DIR / "telegram.json").exists()

    def test_escalates_to_sigkill_on_timeout(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1234)
        # Always alive — never dies.
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)

        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
        # Speed up the loop.
        monkeypatch.setattr(supervisor, "KILL_POLL_INTERVAL_S", 0.01)

        supervisor.stop("telegram", grace_s=0.05)

        assert (1234, signal.SIGTERM) in kills
        assert (1234, signal.SIGKILL) in kills

    def test_force_kill_removes_stale_heartbeat(self, isolated_state, monkeypatch):
        """A force-killed daemon never runs its own heartbeat cleanup. stop()
        must delete it, else its pid reads alive (os.kill(pid, 0) is true for an
        unreaped zombie) and the TUI shows the daemon running, then broken,
        after it's been stopped."""
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1234)
        # Process never dies on its own (drains past grace) → SIGKILL path.
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(supervisor, "KILL_POLL_INTERVAL_S", 0.01)

        supervisor.stop("telegram", grace_s=0.05)

        assert not (supervisor.STATE_DIR / "telegram.json").exists()


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


class TestRestart:
    def test_restart_stops_then_spawns(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1111)

        # First _process_exists call (spawn check) is True (still running);
        # we'll go through stop path, then spawn path checks again (must be False).
        states = iter([True, False, False])
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: next(states))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(supervisor, "KILL_POLL_INTERVAL_S", 0.001)

        popen = MagicMock(return_value=MagicMock(pid=2222))
        new_pid = supervisor.restart(
            "telegram",
            env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"},
            grace_s=0.02,
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        # Need to clear heartbeat between old + new to avoid AlreadyRunning;
        # in real use the daemon writes its own. Here we simulate by removing it.
        # Actually: stop() removes pid file but heartbeat lingers. The spawn
        # check uses _resolve_pid → heartbeat → 1111. _process_exists(1111) is
        # False (second iter), so spawn proceeds.
        assert new_pid == 2222

    def test_restart_when_not_running(self, isolated_state, monkeypatch):
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        popen = MagicMock(return_value=MagicMock(pid=2222))
        pid = supervisor.restart(
            "telegram",
            env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"},
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        assert pid == 2222


# ---------------------------------------------------------------------------
# is_running / read_pid
# ---------------------------------------------------------------------------


class TestQuery:
    def test_is_running_no_file(self, isolated_state):
        assert supervisor.is_running("telegram") is False

    def test_is_running_with_heartbeat_and_alive_pid(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1)
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)
        assert supervisor.is_running("telegram") is True

    def test_is_running_with_heartbeat_and_dead_pid(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=1)
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        assert supervisor.is_running("telegram") is False

    def test_read_pid_prefers_heartbeat_over_pid_file(self, isolated_state):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=42)
        (supervisor.STATE_DIR / "telegram.pid").write_text("99")
        assert supervisor.read_pid("telegram") == 42

    def test_read_pid_falls_back_to_pid_file(self, isolated_state):
        (supervisor.STATE_DIR / "telegram.pid").write_text("99")
        assert supervisor.read_pid("telegram") == 99


# ---------------------------------------------------------------------------
# stop_all / resume
# ---------------------------------------------------------------------------


class TestStopAll:
    def test_no_running_returns_empty(self, isolated_state, monkeypatch):
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        assert supervisor.stop_all() == []

    def test_stops_each_running(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=11)
        _write_heartbeat(supervisor.STATE_DIR, "slack", pid=22)
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
        # Simulate clean SIGTERM by flipping process_exists to False after SIGTERM.
        sigterm_called = {"v": False}

        def fake_exists(pid: int) -> bool:
            return not sigterm_called["v"] or pid not in (11, 22) and False or False

        # Simpler: keep alive for is_running checks, then after stop's SIGTERM,
        # report dead. Use a stateful counter per pid.
        liveness = {11: True, 22: True}

        def liveness_check(pid: int) -> bool:
            return liveness.get(pid, False)

        def kill_kills(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == signal.SIGTERM:
                liveness[pid] = False

        monkeypatch.setattr(supervisor, "_process_exists", liveness_check)
        monkeypatch.setattr(os, "kill", kill_kills)
        monkeypatch.setattr(supervisor, "KILL_POLL_INTERVAL_S", 0.001)

        stopped = supervisor.stop_all(grace_s=0.05)
        names = {n for n, _ in stopped}
        assert names == {"telegram", "slack"}

    def test_writes_last_running_file(self, isolated_state, monkeypatch):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=11)
        liveness = {11: True}
        monkeypatch.setattr(
            supervisor, "_process_exists", lambda pid: liveness.get(pid, False)
        )

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:
                liveness[pid] = False

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(supervisor, "KILL_POLL_INTERVAL_S", 0.001)

        supervisor.stop_all(grace_s=0.05)

        last = supervisor.read_last_running()
        assert last == ["telegram"]

    def test_no_running_does_not_overwrite_last_running(
        self, isolated_state, monkeypatch
    ):
        # Pre-populate with a stale list; if nothing was running, don't clobber.
        supervisor._write_last_running(["slack", "gmail"])
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)

        supervisor.stop_all()

        assert supervisor.read_last_running() == ["slack", "gmail"]


class TestResume:
    def test_no_last_running_returns_empty(self, isolated_state):
        assert supervisor.resume() == []

    def test_spawns_each_recorded(self, isolated_state, monkeypatch):
        supervisor._write_last_running(["telegram"])
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "1")
        # schedule.yaml is not in last_running, no config check needed.
        popen = MagicMock(return_value=MagicMock(pid=4242))

        results = supervisor.resume(popen_factory=popen, wait_for_heartbeat=False)

        assert len(results) == 1
        name, pid, exc = results[0]
        assert name == "telegram"
        assert pid == 4242
        assert exc is None

    def test_skips_already_running(self, isolated_state, monkeypatch):
        supervisor._write_last_running(["telegram"])
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=999)
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: True)
        popen = MagicMock()

        results = supervisor.resume(popen_factory=popen)

        assert len(results) == 1
        name, pid, exc = results[0]
        assert name == "telegram"
        assert pid == 999
        assert exc is None
        popen.assert_not_called()

    def test_captures_per_frontend_failures(self, isolated_state, monkeypatch):
        supervisor._write_last_running(["telegram", "slack"])
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)
        # Telegram has valid env; slack has nothing -> PreflightFailed.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "1")
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        popen = MagicMock(return_value=MagicMock(pid=4242))

        # env_file=None so preflight reads only the monkeypatched env, not the
        # developer's real ~/.claude-on-the-fly/.env, which may define SLACK_*
        # and would mask the intended "slack has nothing" case.
        results = supervisor.resume(
            env_file=None, popen_factory=popen, wait_for_heartbeat=False
        )

        by_name = {r[0]: r for r in results}
        assert by_name["telegram"][2] is None  # success
        assert isinstance(by_name["slack"][2], supervisor.PreflightFailed)

    def test_ignores_unknown_frontends_in_state(self, isolated_state):
        # Deliberately tamper with the file to include a junk entry.
        path = supervisor._last_running_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"frontends": ["telegram", "not-real"]}')

        assert supervisor.read_last_running() == ["telegram"]

    def test_corrupt_state_file_yields_empty(self, isolated_state):
        path = supervisor._last_running_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")

        assert supervisor.read_last_running() == []
