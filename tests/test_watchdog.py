"""The unattended recovery path.

Nothing else in the package restarts a daemon without a person, so a wiring
mistake here is silent: the scheduler runs, exits 0, and the wedged daemon stays
wedged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_on_the_fly import watchdog
from claude_on_the_fly.tui import state as tui_state
from claude_on_the_fly.tui import supervisor


def _status(state: str, *, age_s: float | None = None) -> tui_state.FrontendStatus:
    return tui_state.FrontendStatus(
        name="slack", state=state, pid=123, last_heartbeat_age_s=age_s
    )


def _write_heartbeat(state_dir: Path, frontend: str, *, pid: int, age_s: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    last = datetime.now(UTC) - timedelta(seconds=age_s)
    (state_dir / f"{frontend}.json").write_text(
        json.dumps(
            {
                "frontend": frontend,
                "pid": pid,
                "started_at": "2026-01-01T00:00:00Z",
                "last_heartbeat": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "extra": {},
            }
        )
    )


class TestDiagnose:
    def test_a_healthy_daemon_is_left_alone(self):
        assert watchdog.diagnose(_status("running", age_s=2), stale_after=90) == (
            watchdog.Decision("skip", "state:running")
        )

    def test_a_stopped_daemon_is_never_restarted(self):
        """The design decision, not an oversight.

        Nothing on disk separates a crash from `claude-tui stop slack`, or from
        the window inside `claude-tui upgrade` between stop_all and resume.
        Starting one here would undo an operator's action and race the upgrade.
        """
        assert watchdog.diagnose(_status("stopped"), stale_after=90) == (
            watchdog.Decision("skip", "state:stopped")
        )

    def test_a_wedged_daemon_over_the_threshold_is_restarted(self):
        decision = watchdog.diagnose(_status("broken", age_s=120), stale_after=90)
        assert decision.action == "restart"
        assert decision.reason == "heartbeat_stale:120s"

    def test_a_wedged_daemon_under_the_threshold_waits(self):
        """The dashboard calls it broken at 15s; acting that early would kill
        every in-flight turn over a starved coroutine."""
        decision = watchdog.diagnose(_status("broken", age_s=20), stale_after=90)
        assert decision.action == "skip"
        assert decision.reason == "stale_below_threshold:20s<90s"


class TestStaleSetting:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.setattr(watchdog.settings, "get", lambda *a, **k: "")
        assert watchdog.stale_after_s() == watchdog.DEFAULT_STALE_S

    def test_operator_value_is_used(self, monkeypatch):
        monkeypatch.setattr(watchdog.settings, "get", lambda *a, **k: " 45 ")
        assert watchdog.stale_after_s() == 45.0

    @pytest.mark.parametrize("raw", ["soon", "0", "-5", "nan", "inf"])
    def test_a_bad_value_warns_and_falls_back(self, raw, monkeypatch, caplog):
        monkeypatch.setattr(watchdog.settings, "get", lambda *a, **k: raw)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.watchdog"):
            assert watchdog.stale_after_s() == watchdog.DEFAULT_STALE_S
        assert watchdog.STALE_SETTING in caplog.text


class TestRunOnce:
    def test_unknown_frontend_is_refused(self):
        with pytest.raises(ValueError, match="unknown frontend"):
            watchdog.run_once(frontend="nope")

    def test_a_wedged_daemon_is_restarted_through_the_supervisor(
        self, tmp_path, monkeypatch
    ):
        """Through supervisor.restart, not a private stop/spawn pair, so it
        takes the same preflight and locking an operator's restart takes."""
        state = tmp_path / "state"
        _write_heartbeat(state, "slack", pid=4242, age_s=300)
        calls: list[str] = []
        monkeypatch.setattr(supervisor, "restart", lambda name: calls.append(name))

        decision = watchdog.run_once(
            frontend="slack", state_dir=state, process_check=lambda pid: True
        )

        assert decision.action == "restart"
        assert calls == ["slack"]

    def test_dry_run_reports_without_touching_anything(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        _write_heartbeat(state, "slack", pid=4242, age_s=300)
        monkeypatch.setattr(
            supervisor, "restart", lambda name: pytest.fail("dry run restarted")
        )

        decision = watchdog.run_once(
            frontend="slack",
            state_dir=state,
            dry_run=True,
            process_check=lambda pid: True,
        )

        assert decision.action == "dry_run"
        assert "heartbeat_stale" in decision.reason

    def test_a_healthy_daemon_is_not_restarted(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        _write_heartbeat(state, "slack", pid=4242, age_s=1)
        monkeypatch.setattr(
            supervisor, "restart", lambda name: pytest.fail("restarted a live daemon")
        )

        assert (
            watchdog.run_once(
                frontend="slack", state_dir=state, process_check=lambda pid: True
            ).action
            == "skip"
        )


class TestMain:
    def test_exits_zero_on_a_healthy_daemon(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            watchdog,
            "run_once",
            lambda **kw: watchdog.Decision("skip", "state:running"),
        )
        assert watchdog.main(["--frontend", "slack"]) == 0

    def test_a_supervisor_refusal_is_one_line_and_exit_two(self, monkeypatch, caplog):
        def refuse(**_kw):
            raise supervisor.AlreadyRunning("slack", 99)

        monkeypatch.setattr(watchdog, "run_once", refuse)
        with caplog.at_level("ERROR", logger="claude_on_the_fly.watchdog"):
            assert watchdog.main(["--frontend", "slack"]) == 2
        assert "already running" in caplog.text
        assert "Traceback" not in caplog.text

    def test_dry_run_flag_reaches_run_once(self, monkeypatch):
        seen: dict = {}

        def record(**kw):
            seen.update(kw)
            return watchdog.Decision("skip", "state:stopped")

        monkeypatch.setattr(watchdog, "run_once", record)
        assert watchdog.main(["--frontend", "cron", "--dry-run"]) == 0
        assert seen == {"frontend": "cron", "dry_run": True}
