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

    def test_preflight_reads_migrated_settings_from_config_yaml(
        self, isolated_state, operator_settings
    ):
        """A setting that moved to config.yaml must satisfy the spawn preflight
        even when absent from the environment. The daemon's own startup checks
        settings.environment(), which layers the file; a preflight that read the
        bare env would refuse a daemon that would have started fine."""
        operator_settings.write_text("telegram:\n  allowed_user_id: 8760905177\n")
        popen = MagicMock(return_value=MagicMock(pid=4242))
        pid = supervisor.spawn(
            "telegram",
            env={"TELEGRAM_BOT_TOKEN": "tok"},
            popen_factory=popen,
            wait_for_heartbeat=False,
        )
        assert pid == 4242

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
            return not sigterm_called["v"] or (pid not in (11, 22) and False) or False

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
        supervisor._write_last_running(["slack", "telegram"])
        monkeypatch.setattr(supervisor, "_process_exists", lambda p: False)

        supervisor.stop_all()

        assert supervisor.read_last_running() == ["slack", "telegram"]


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


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------


def _result(name, status, detail="", hint=""):
    from claude_on_the_fly.checks import CheckResult

    return CheckResult(name=name, status=status, detail=detail, fix_hint=hint)


class TestPreflightFailedMessage:
    def test_the_first_blocking_check_is_the_headline(self):
        """A spawn refused for four reasons still has one to lead with, and it has to
        be a blocking one rather than whichever came first."""
        exc = supervisor.PreflightFailed(
            "slack",
            [
                _result("advisory", "warn", "no worker running"),
                _result("SLACK_TOKEN", "missing", "not set"),
            ],
        )
        assert "SLACK_TOKEN" in str(exc)
        assert "not set" in str(exc)

    def test_no_blocking_check_still_says_which_frontend(self):
        """Reachable when every failure is advisory, and a bare "preflight failed"
        with no name is unactionable in a stop-all/resume log."""
        exc = supervisor.PreflightFailed(
            "slack", [_result("advisory", "warn", "no worker running")]
        )
        assert str(exc) == "preflight failed for slack"


class TestSpawnTimeoutMessage:
    def test_a_child_that_died_reports_its_exit_code(self):
        """ "Exited rc=1" and "started but never heartbeated" are different problems,
        and the message is where an operator learns which one they have."""
        exc = supervisor.SpawnTimeout(
            frontend="slack",
            pid=42,
            log_path=Path("/logs/slack.stdout"),
            log_tail="Traceback...\nImportError: slack_bolt\n",
            exited=True,
            returncode=1,
        )
        message = str(exc)
        assert "exited (rc=1) before heartbeat" in message
        assert "/logs/slack.stdout" in message
        # The tail is the whole reason this exception carries one.
        assert "ImportError: slack_bolt" in message

    def test_a_child_that_hung_says_so(self):
        exc = supervisor.SpawnTimeout(
            frontend="slack", pid=42, log_path=Path("/logs/slack.stdout")
        )
        message = str(exc)
        assert "did not heartbeat within timeout" in message
        assert "last lines" not in message, "no tail, so no empty tail section"


# ---------------------------------------------------------------------------
# Reading pids and heartbeats off disk
# ---------------------------------------------------------------------------


class TestReadPid:
    def test_a_corrupt_heartbeat_falls_back_to_the_pid_file(self, isolated_state):
        state = isolated_state / "state"
        (state / "slack.json").write_text("{not json")
        (state / "slack.pid").write_text("4242\n")
        assert supervisor._resolve_pid("slack") == 4242

    def test_a_heartbeat_without_an_integer_pid_falls_back(self, isolated_state):
        state = isolated_state / "state"
        (state / "slack.json").write_text(json.dumps({"pid": "not-an-int"}))
        (state / "slack.pid").write_text("4242\n")
        assert supervisor._resolve_pid("slack") == 4242

    def test_a_corrupt_pid_file_reads_as_nothing_running(self, isolated_state):
        (isolated_state / "state" / "slack.pid").write_text("not-a-number")
        assert supervisor._resolve_pid("slack") is None

    def test_neither_file_reads_as_nothing_running(self):
        assert supervisor._resolve_pid("slack") is None


class TestHeartbeatFreshness:
    def test_a_missing_file_is_not_fresh(self):
        assert supervisor._heartbeat_fresh("slack") is False

    def test_a_recent_heartbeat_is_fresh(self, isolated_state):
        from datetime import UTC, datetime

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (isolated_state / "state" / "slack.json").write_text(
            json.dumps({"last_heartbeat": now})
        )
        assert supervisor._heartbeat_fresh("slack") is True

    def test_an_old_heartbeat_is_not_fresh(self, isolated_state):
        """A SIGKILLed daemon leaves its file behind, so age is the only thing that
        distinguishes it from a live one."""
        from datetime import UTC, datetime, timedelta

        long_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        (isolated_state / "state" / "slack.json").write_text(
            json.dumps({"last_heartbeat": long_ago})
        )
        assert supervisor._heartbeat_fresh("slack") is False

    def test_a_non_string_timestamp_is_not_fresh(self, isolated_state):
        (isolated_state / "state" / "slack.json").write_text(
            json.dumps({"last_heartbeat": 1785382860})
        )
        assert supervisor._heartbeat_fresh("slack") is False

    def test_an_unparseable_timestamp_is_not_fresh(self, isolated_state):
        (isolated_state / "state" / "slack.json").write_text(
            json.dumps({"last_heartbeat": "yesterday"})
        )
        assert supervisor._heartbeat_fresh("slack") is False

    def test_corrupt_json_is_not_fresh(self, isolated_state):
        (isolated_state / "state" / "slack.json").write_text("{not json")
        assert supervisor._heartbeat_fresh("slack") is False


class TestTailFile:
    def test_a_missing_file_tails_to_nothing(self, tmp_path):
        """The tail decorates a SpawnTimeout, so a missing log must not raise inside
        the exception that is already reporting a failure."""
        assert supervisor._tail_file(tmp_path / "gone.stdout") == ""

    def test_the_last_lines_come_back(self, tmp_path):
        path = tmp_path / "slack.stdout"
        path.write_text("".join(f"line {i}\n" for i in range(50)))
        tail = supervisor._tail_file(path, n_lines=3)
        assert tail.splitlines() == ["line 47", "line 48", "line 49"]


class TestLastRunningRecord:
    def test_a_corrupt_record_resumes_nothing(self, isolated_state):
        (isolated_state / "state" / "last_running.json").write_text("{not json")
        assert supervisor.read_last_running() == []

    def test_a_record_that_is_not_a_list_resumes_nothing(self, isolated_state):
        (isolated_state / "state" / "last_running.json").write_text(
            json.dumps({"frontends": "slack"})
        )
        assert supervisor.read_last_running() == []

    def test_unknown_frontends_are_filtered_out(self, isolated_state):
        """The record is a file on disk; a stale or hand-edited name must not be
        handed to spawn."""
        (isolated_state / "state" / "last_running.json").write_text(
            json.dumps({"frontends": ["slack", "not-a-frontend", 42]})
        )
        assert supervisor.read_last_running() == ["slack"]


# ---------------------------------------------------------------------------
# Waiting for the first heartbeat
# ---------------------------------------------------------------------------


TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "1"}


class TestSpawnWaitsForAHeartbeat:
    """Returning a pid the moment Popen succeeds is what made `claude-tui start`
    report success for a daemon that then died on an ImportError. The wait is what
    turns that into an actionable failure."""

    def test_a_daemon_that_heartbeats_returns_its_pid(
        self, isolated_state, monkeypatch
    ):
        from datetime import UTC, datetime

        proc = MagicMock(pid=4242)
        proc.poll.return_value = None

        def heartbeat_now(_frontend):
            (supervisor.STATE_DIR / "telegram.json").write_text(
                json.dumps(
                    {"last_heartbeat": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
                )
            )
            return True

        monkeypatch.setattr(supervisor, "_heartbeat_fresh", heartbeat_now)
        pid = supervisor.spawn(
            "telegram",
            env=TELEGRAM_ENV,
            popen_factory=MagicMock(return_value=proc),
            wait_for_heartbeat=True,
        )
        assert pid == 4242

    def test_a_daemon_that_dies_first_reports_its_exit_code_and_log(
        self, isolated_state, monkeypatch
    ):
        proc = MagicMock(pid=4242)
        proc.poll.return_value = 1
        monkeypatch.setattr(supervisor, "_heartbeat_fresh", lambda _f: False)
        stdout = supervisor.LOG_DIR / "telegram.stdout"
        stdout.write_text("Traceback...\nImportError: no module named telegram\n")
        monkeypatch.setattr(supervisor, "_stdout_file", lambda _f: stdout)

        with pytest.raises(supervisor.SpawnTimeout) as caught:
            supervisor.spawn(
                "telegram",
                env=TELEGRAM_ENV,
                popen_factory=MagicMock(return_value=proc),
                wait_for_heartbeat=True,
            )
        assert caught.value.exited is True
        assert caught.value.returncode == 1
        assert "ImportError" in caught.value.log_tail
        # The stale pid file must not survive a failed spawn, or `stop` would
        # signal a pid the OS has already recycled.
        assert not (supervisor.STATE_DIR / "telegram.pid").exists()

    def test_a_daemon_that_hangs_is_killed_and_reported(
        self, isolated_state, monkeypatch
    ):
        """Still running but never heartbeating: leaving it would mean a process
        holding the queue that nothing will ever manage."""
        proc = MagicMock(pid=4242)
        proc.poll.return_value = None
        monkeypatch.setattr(supervisor, "_heartbeat_fresh", lambda _f: False)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        with pytest.raises(supervisor.SpawnTimeout) as caught:
            supervisor.spawn(
                "telegram",
                env=TELEGRAM_ENV,
                popen_factory=MagicMock(return_value=proc),
                wait_for_heartbeat=True,
                spawn_timeout_s=0.01,
            )
        assert caught.value.exited is False
        assert killed == [(4242, signal.SIGTERM)]
        assert not (supervisor.STATE_DIR / "telegram.pid").exists()

    def test_a_kill_that_fails_does_not_mask_the_timeout(
        self, isolated_state, monkeypatch
    ):
        """The timeout is the thing the operator needs to hear about; a failed
        cleanup signal on top of it is not."""
        proc = MagicMock(pid=4242)
        proc.poll.return_value = None
        monkeypatch.setattr(supervisor, "_heartbeat_fresh", lambda _f: False)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            supervisor.os,
            "getpgid",
            lambda _pid: (_ for _ in ()).throw(ProcessLookupError),
        )
        with pytest.raises(supervisor.SpawnTimeout):
            supervisor.spawn(
                "telegram",
                env=TELEGRAM_ENV,
                popen_factory=MagicMock(return_value=proc),
                wait_for_heartbeat=True,
                spawn_timeout_s=0.01,
            )


class TestStopSignalFailures:
    def test_a_sigterm_that_fails_still_escalates(self, isolated_state, monkeypatch):
        """The pid may have exited between the liveness check and the signal, and
        SIGKILL on an already-dead pid is harmless."""
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=4242)
        monkeypatch.setattr(supervisor, "_process_exists", lambda _p: True)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
        sent: list[int] = []

        def kill(_pid, sig):
            sent.append(sig)
            if sig == signal.SIGTERM:
                raise OSError("no such process")

        monkeypatch.setattr(supervisor.os, "kill", kill)
        assert supervisor.stop("telegram", grace_s=0.01) == 4242
        assert signal.SIGKILL in sent

    def test_a_sigkill_that_fails_is_logged_not_raised(
        self, isolated_state, monkeypatch, caplog
    ):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=4242)
        monkeypatch.setattr(supervisor, "_process_exists", lambda _p: True)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            supervisor.os,
            "kill",
            lambda _pid, _sig: (_ for _ in ()).throw(OSError("permission denied")),
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.tui.supervisor"):
            assert supervisor.stop("telegram", grace_s=0.01) == 4242
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "SIGTERM" in logged and "SIGKILL" in logged

    def test_a_process_that_dies_during_the_second_wait_breaks_early(
        self, isolated_state, monkeypatch
    ):
        _write_heartbeat(supervisor.STATE_DIR, "telegram", pid=4242)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
        killed = {"sigkill": False}

        def kill(_pid, sig):
            if sig == signal.SIGKILL:
                killed["sigkill"] = True

        monkeypatch.setattr(supervisor.os, "kill", kill)
        # Alive for the whole grace window, gone the moment SIGKILL lands. Keyed off
        # the signal rather than a call count, because the grace loop's iteration
        # count depends on wall-clock and would make a counter flaky.
        monkeypatch.setattr(
            supervisor, "_process_exists", lambda _pid: not killed["sigkill"]
        )
        assert supervisor.stop("telegram", grace_s=0.01) == 4242
        assert killed["sigkill"] is True


class TestStopAllSkipsWhatVanishes:
    def test_a_daemon_that_exits_between_the_check_and_the_stop_is_skipped(
        self, isolated_state, monkeypatch
    ):
        """`is_running` and `stop` are two separate observations, and a daemon that
        exits in between must not turn stop-all into an error."""
        monkeypatch.setattr(supervisor, "is_running", lambda name: name == "telegram")
        monkeypatch.setattr(
            supervisor,
            "stop",
            lambda _name, **_kw: (_ for _ in ()).throw(
                supervisor.NotRunning("gone already")
            ),
        )
        assert supervisor.stop_all() == []


class TestSignallingRespectsGroupOwnership:
    """Signalling a process *group* reaches the agent CLIs a daemon spawned.
    It is only safe when the daemon proved the group is its own; otherwise the
    group could be the operator's shell."""

    def test_a_daemon_that_owns_its_group_gets_the_group_signalled(
        self, isolated_state, monkeypatch
    ):
        state = isolated_state / "state"
        (state / "slack.json").write_text(
            json.dumps({"frontend": "slack", "pid": 4321, "process_group": 4321})
        )
        killed: list[tuple] = []
        monkeypatch.setattr(
            supervisor.os, "killpg", lambda pid, sig: killed.append(("group", pid, sig))
        )
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append(("single", pid, sig))
        )

        supervisor._signal_daemon("slack", 4321, signal.SIGTERM)

        assert killed == [("group", 4321, signal.SIGTERM)]

    def test_a_daemon_that_does_not_advertise_a_group_gets_only_its_own_pid(
        self, isolated_state, monkeypatch
    ):
        state = isolated_state / "state"
        _write_heartbeat(state, "slack", 4321)  # no process_group key
        killed: list[tuple] = []
        monkeypatch.setattr(
            supervisor.os, "killpg", lambda pid, sig: killed.append(("group", pid, sig))
        )
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append(("single", pid, sig))
        )

        supervisor._signal_daemon("slack", 4321, signal.SIGTERM)

        assert killed == [("single", 4321, signal.SIGTERM)]


class TestSweepingDetachedAgentGroups:
    def test_no_ledger_means_nothing_to_do(self, tmp_path, monkeypatch):
        monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
        supervisor._sweep_agent_groups("slack")

    def test_a_ledger_is_swept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
        ledger = tmp_path / "state" / "slack.pids"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("")
        swept: list[Path] = []

        class _Ledger:
            def __init__(self, path):
                self.path = path

            def sweep(self):
                swept.append(self.path)

        monkeypatch.setattr("claude_on_the_fly.jobs.orphans.ProcessLedger", _Ledger)
        supervisor._sweep_agent_groups("slack")
        assert swept == [ledger]

    def test_an_unreadable_ledger_does_not_stop_the_daemon_stopping(
        self, tmp_path, monkeypatch, caplog
    ):
        """This runs on the stop path. A recovery pass that cannot read a stale
        ledger must not leave the operator unable to stop a daemon."""
        monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
        ledger = tmp_path / "state" / "slack.pids"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("garbage")

        class _Ledger:
            def __init__(self, path):
                raise OSError("stale ledger")

        monkeypatch.setattr("claude_on_the_fly.jobs.orphans.ProcessLedger", _Ledger)
        with caplog.at_level("ERROR"):
            supervisor._sweep_agent_groups("slack")
        assert "could not sweep detached agent groups for slack" in caplog.text


# ---------------------------------------------------------------------------
# pending_work — what a stop is about to interrupt
# ---------------------------------------------------------------------------


def _write_extra(state_dir: Path, frontend: str, extra: dict) -> None:
    _write_heartbeat(state_dir, frontend, os.getpid())
    path = state_dir / f"{frontend}.json"
    payload = json.loads(path.read_text())
    payload["extra"] = extra
    path.write_text(json.dumps(payload))


class TestPendingWork:
    def test_a_stopped_daemon_has_nothing_pending(self, isolated_state):
        assert supervisor.pending_work("slack") is None

    def test_an_idle_chat_daemon_has_nothing_pending(self, isolated_state):
        _write_extra(isolated_state / "state", "slack", {"running_jobs": []})

        assert supervisor.pending_work("slack") is None

    def test_a_chat_daemons_turns_are_reported_as_unrecoverable(self, isolated_state):
        """Nothing replays a chat turn, so this is the number that has to reach
        the operator before they agree to a stop."""
        _write_extra(
            isolated_state / "state",
            "slack",
            {"running_jobs": [{"identifier": "slack/1"}], "queued_turns": 2},
        )

        pending = supervisor.pending_work("slack")

        assert pending is not None
        assert (pending.running, pending.queued) == (1, 2)
        assert pending.recoverable is False
        assert pending.at_risk == 3
        assert "lost, needs resending" in pending.describe()

    def test_a_heartbeat_without_extras_reads_as_idle(self, isolated_state):
        """An older daemon publishes no queued count. Absent is zero, not a crash."""
        _write_heartbeat(isolated_state / "state", "slack", os.getpid())

        assert supervisor.pending_work("slack") is None

    def test_an_unparseable_heartbeat_reads_as_idle(self, isolated_state):
        """The pid still resolves from the PID file, so the daemon is alive and
        only its extras are unreadable. Degrade the report, not the stop."""
        state = isolated_state / "state"
        (state / "slack.pid").write_text(str(os.getpid()))
        (state / "slack.json").write_text("{not json")

        assert supervisor.pending_work("slack") is None

    def test_a_non_mapping_extra_reads_as_idle(self, isolated_state):
        state = isolated_state / "state"
        _write_extra(state, "slack", {})
        payload = json.loads((state / "slack.json").read_text())
        payload["extra"] = "nope"
        (state / "slack.json").write_text(json.dumps(payload))

        assert supervisor.pending_work("slack") is None

    def test_a_junk_queued_count_reads_as_zero(self, isolated_state):
        _write_extra(
            isolated_state / "state",
            "slack",
            {"running_jobs": [{"identifier": "slack/1"}], "queued_turns": "many"},
        )

        pending = supervisor.pending_work("slack")

        assert pending is not None
        assert (pending.running, pending.queued) == (1, 0)

    def test_crons_running_commands_are_reported_as_recoverable(self, isolated_state):
        """A cancelled command re-fires on its own schedule, so this postpones
        work rather than losing it."""
        _write_extra(
            isolated_state / "state", "cron", {"running_commands": ["daily-digest"]}
        )

        pending = supervisor.pending_work("cron")

        assert pending is not None
        assert (pending.running, pending.at_risk) == (1, 0)
        assert "resumes after the restart" in pending.describe()

    def test_the_job_queues_depth_comes_from_the_maildir(
        self, isolated_state, monkeypatch
    ):
        from claude_on_the_fly.jobs.file_queue import QueueDepth

        _write_extra(isolated_state / "state", "jobs", {})
        monkeypatch.setattr(
            "claude_on_the_fly.tui.state.jobs_queue_depth",
            lambda: QueueDepth(new=4, running=1, done=0, failed=0),
        )

        pending = supervisor.pending_work("jobs")

        assert pending is not None
        assert (pending.running, pending.queued) == (1, 4)
        assert pending.recoverable is True

    def test_an_empty_job_queue_has_nothing_pending(self, isolated_state, monkeypatch):
        from claude_on_the_fly.jobs.file_queue import QueueDepth

        _write_extra(isolated_state / "state", "jobs", {})
        monkeypatch.setattr(
            "claude_on_the_fly.tui.state.jobs_queue_depth",
            lambda: QueueDepth(new=0, running=0, done=9, failed=1),
        )

        assert supervisor.pending_work("jobs") is None

    def test_an_unreadable_queue_says_so_instead_of_reporting_zero(
        self, isolated_state, monkeypatch
    ):
        """A broker-backed queue lives where this cannot look, and "nothing
        pending" is exactly the lie the report exists to prevent."""
        _write_extra(isolated_state / "state", "jobs", {})
        monkeypatch.setattr(
            "claude_on_the_fly.tui.state.jobs_queue_depth", lambda: None
        )

        pending = supervisor.pending_work("jobs")

        assert pending is not None
        assert pending.known is False
        assert "unknown" in pending.describe()

    def test_all_pending_work_covers_every_running_daemon(self, isolated_state):
        state = isolated_state / "state"
        _write_extra(state, "slack", {"running_jobs": [{"identifier": "slack/1"}]})
        _write_extra(state, "cron", {"running_commands": ["nightly"]})

        names = [item.frontend for item in supervisor.all_pending_work()]

        assert names == ["slack", "cron"]


class TestStopGrace:
    def test_the_default_grace_leaves_room_for_the_notices(self):
        """The notices are posted inside the grace window: a 5s stop (what
        --force still uses) would SIGKILL the daemon mid-sentence."""
        from claude_on_the_fly import orchestrator

        assert supervisor.SAFE_GRACE_S > orchestrator.SHUTDOWN_NOTICE_BUDGET_S
        assert supervisor.FORCE_GRACE_S < supervisor.SAFE_GRACE_S
