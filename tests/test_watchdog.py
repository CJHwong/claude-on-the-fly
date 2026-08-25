from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly import watchdog

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _payload(**overrides):
    return {
        "pid": 42,
        "last_heartbeat": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extra": {"running_jobs": []},
        **overrides,
    }


def _diagnose(payload, *, process=True):
    return watchdog.diagnose(
        payload,
        now=NOW,
        stale_s=90,
        timeout_grace_s=120,
        process_check=lambda _pid: process,
    )


def test_missing_heartbeat_restarts() -> None:
    assert _diagnose(None) == watchdog.Decision("restart", "heartbeat_missing")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {"pid": "x", "last_heartbeat": "2026-08-14T12:00:00Z"},
            "heartbeat_pid_invalid",
        ),
        ({"pid": 1}, "heartbeat_timestamp_invalid"),
        ({"pid": 1, "last_heartbeat": "bad"}, "heartbeat_timestamp_invalid"),
    ],
)
def test_invalid_heartbeat_restarts(payload, reason) -> None:
    assert _diagnose(payload).reason == reason


def test_missing_process_restarts_even_with_fresh_heartbeat() -> None:
    assert _diagnose(_payload(), process=False).reason == "daemon_process_missing"


def test_stale_heartbeat_restarts() -> None:
    old = (NOW - timedelta(seconds=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    decision = _diagnose(_payload(last_heartbeat=old))
    assert decision.action == "restart"
    assert decision.reason.startswith("heartbeat_stale")


def test_healthy_daemon_is_left_alone() -> None:
    assert _diagnose(_payload()) == watchdog.Decision("healthy", "ok")


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("bad", "heartbeat_extra_invalid"),
        ({"running_jobs": "bad"}, "running_jobs_invalid"),
    ],
)
def test_invalid_extra_restarts(extra, reason) -> None:
    assert _diagnose(_payload(extra=extra)).reason == reason


def test_managed_turn_uses_advertised_execution_limit() -> None:
    row = {"uptime_s": 125, "timeout_s": 10, "background": True}
    decision = _diagnose(_payload(extra={"running_jobs": [row]}))
    assert decision == watchdog.Decision("healthy", "ok")
    row["uptime_s"] = 131
    decision = _diagnose(_payload(extra={"running_jobs": [row]}))
    assert decision.reason == "background_turn_over_limit:131s>130s"


@pytest.mark.parametrize(
    "row",
    [
        "not a mapping",
        {"uptime_s": 999},
        {"uptime_s": "bad", "timeout_s": 1},
        {"uptime_s": 999, "timeout_s": "nan"},
        {"uptime_s": 999, "timeout_s": -1},
    ],
)
def test_unusable_turn_limits_are_ignored(row) -> None:
    assert _diagnose(_payload(extra={"running_jobs": [row]})).action == "healthy"


@pytest.mark.parametrize("raw", ["bad", "0", "inf"])
def test_bad_settings_fall_back(raw, monkeypatch, caplog) -> None:
    monkeypatch.setenv("WATCHDOG_HEARTBEAT_STALE_SECONDS", raw)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.watchdog"):
        assert watchdog.heartbeat_stale_s() == watchdog.DEFAULT_HEARTBEAT_STALE_S
    assert "using" in caplog.text


def test_settings_are_live(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_HEARTBEAT_STALE_SECONDS", "12.5")
    monkeypatch.setenv("WATCHDOG_LIMIT_GRACE_SECONDS", "6")
    assert watchdog.heartbeat_stale_s() == 12.5
    assert watchdog.limit_grace_s() == 6


def test_read_payload_fails_open(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert watchdog._read_payload(missing) is None
    missing.write_text("[]")
    assert watchdog._read_payload(missing) is None


def test_run_once_restarts_unhealthy_daemon(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "slack.json").write_text(json.dumps(_payload()))
    with (
        patch("claude_on_the_fly.watchdog.datetime") as clock,
        patch("claude_on_the_fly.watchdog.process_exists", return_value=True),
        patch("claude_on_the_fly.watchdog.supervisor.restart") as restart,
    ):
        clock.now.return_value = NOW + timedelta(days=1)
        clock.strptime = datetime.strptime
        decision = watchdog.run_once(frontend="slack", state_dir=state)
    assert decision.action == "restart"
    restart.assert_called_once_with("slack")


def test_run_once_dry_run_does_not_restart(tmp_path: Path) -> None:
    with patch("claude_on_the_fly.watchdog.supervisor.restart") as restart:
        decision = watchdog.run_once(frontend="slack", state_dir=tmp_path, dry_run=True)
    assert decision.reason == "heartbeat_missing"
    restart.assert_not_called()


def test_run_once_skips_when_restart_owned(tmp_path: Path) -> None:
    with patch(
        "claude_on_the_fly.watchdog.supervisor.restart",
        side_effect=watchdog.supervisor.RestartInProgress("slack"),
    ):
        decision = watchdog.run_once(frontend="slack", state_dir=tmp_path)
    assert decision == watchdog.Decision("healthy", "restart_in_progress")


def test_unknown_frontend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown frontend"):
        watchdog.run_once(frontend="nope", state_dir=tmp_path)


def test_main_returns_zero_and_supervisor_errors_return_two() -> None:
    with patch("claude_on_the_fly.watchdog.run_once") as run:
        assert watchdog.main(["--frontend", "slack", "--dry-run"]) == 0
        run.assert_called_once_with(frontend="slack", dry_run=True)
    with patch(
        "claude_on_the_fly.watchdog.run_once",
        side_effect=watchdog.supervisor.ControllerOutOfDate("old", "new"),
    ):
        assert watchdog.main(["--frontend", "slack"]) == 2
