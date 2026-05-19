"""Tests for tui.state.snapshot()."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.tui.state import (
    STALENESS_S,
    Snapshot,
    snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_heartbeat(
    state_dir: Path,
    frontend: str,
    *,
    pid: int = 12345,
    last_heartbeat: str = "2026-05-19T13:00:00Z",
    started_at: str = "2026-05-19T12:00:00Z",
    extra: dict | None = None,
    version: str | None = "0.1.0",
    executable: str | None = "/venv/bin/python",
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "frontend": frontend,
        "pid": pid,
        "started_at": started_at,
        "last_heartbeat": last_heartbeat,
        "extra": extra or {},
    }
    if version is not None:
        payload["version"] = version
    if executable is not None:
        payload["executable"] = executable
    (state_dir / f"{frontend}.json").write_text(json.dumps(payload))


def _at(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@pytest.fixture
def empty_state(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def alive_check():
    return lambda pid: True


@pytest.fixture
def dead_check():
    return lambda pid: False


# ---------------------------------------------------------------------------
# Snapshot — frontends
# ---------------------------------------------------------------------------


class TestFrontends:
    def test_empty_state_all_stopped(self, empty_state, alive_check):
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert {f.name for f in snap.frontends} == set(SUPERVISABLE_FRONTENDS)
        assert all(f.state == "stopped" for f in snap.frontends)
        assert all(f.pid is None for f in snap.frontends)

    def test_running_when_fresh_and_alive(self, empty_state, alive_check):
        _write_heartbeat(
            empty_state, "telegram", pid=999, last_heartbeat="2026-05-19T13:00:00Z"
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "running"
        assert tg.pid == 999
        assert tg.last_heartbeat_age_s == pytest.approx(5.0)

    def test_stopped_when_pid_missing(self, empty_state, dead_check):
        _write_heartbeat(empty_state, "telegram", pid=999)
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=dead_check,
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "stopped"

    def test_broken_when_stale_but_pid_alive(self, empty_state, alive_check):
        _write_heartbeat(empty_state, "telegram", last_heartbeat="2026-05-19T13:00:00Z")
        # Default staleness for telegram is 15s; advance well past.
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:01:00Z"),
            process_check=alive_check,
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "broken"

    def test_symphony_gets_longer_staleness(self, empty_state, alive_check):
        _write_heartbeat(empty_state, "symphony", last_heartbeat="2026-05-19T13:00:00Z")
        # 30s old: telegram would be broken, symphony should still be running.
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:30Z"),
            process_check=alive_check,
        )
        s = next(f for f in snap.frontends if f.name == "symphony")
        assert s.state == "running"
        assert STALENESS_S["symphony"] > STALENESS_S["default"]

    def test_extra_is_propagated(self, empty_state, alive_check):
        _write_heartbeat(
            empty_state,
            "symphony",
            last_heartbeat="2026-05-19T13:00:00Z",
            extra={"running": 3, "retry_queue": 1},
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
        )
        s = next(f for f in snap.frontends if f.name == "symphony")
        assert s.extra == {"running": 3, "retry_queue": 1}

    def test_corrupted_heartbeat_is_stopped_with_error(
        self, empty_state, alive_check, tmp_path
    ):
        empty_state.mkdir()
        (empty_state / "telegram.json").write_text("{ not valid json")
        snap = snapshot(empty_state, None, process_check=alive_check)
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "stopped"
        assert tg.error is not None

    def test_missing_fields_yields_stopped_with_error(self, empty_state, alive_check):
        empty_state.mkdir()
        (empty_state / "telegram.json").write_text(json.dumps({"frontend": "telegram"}))
        snap = snapshot(empty_state, None, process_check=alive_check)
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "stopped"
        assert "pid" in (tg.error or "")


# ---------------------------------------------------------------------------
# Snapshot — scheduler jobs
# ---------------------------------------------------------------------------


def _write_schedule(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"jobs": jobs}))


class TestJobs:
    def test_no_schedule_yaml_means_empty_jobs(self, empty_state, alive_check):
        snap = snapshot(
            empty_state, Path("/nonexistent/schedule.yaml"), process_check=alive_check
        )
        assert snap.jobs == []
        assert snap.schedule_error is None

    def test_well_formed_schedule_yields_jobs(self, tmp_path, empty_state, alive_check):
        schedule = tmp_path / "schedule.yaml"
        _write_schedule(
            schedule,
            [
                {"name": "digest", "cron": "0 9 * * *", "prompt": "summarize my inbox"},
                {
                    "name": "pr-watch",
                    "cron": "*/15 * * * *",
                    "prompt": "check open prs",
                },
            ],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert len(snap.jobs) == 2
        names = {j.name for j in snap.jobs}
        assert names == {"digest", "pr-watch"}
        # All jobs are prompt kind here.
        assert all(j.kind == "prompt" for j in snap.jobs)

    def test_jobs_sorted_by_next_fire(self, tmp_path, empty_state, alive_check):
        schedule = tmp_path / "schedule.yaml"
        _write_schedule(
            schedule,
            [
                {"name": "daily", "cron": "0 9 * * *", "prompt": "x"},
                {"name": "every-min", "cron": "* * * * *", "prompt": "y"},
            ],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        # every-min fires before daily on any reasonable now.
        assert snap.jobs[0].name == "every-min"

    def test_malformed_yaml_reports_error(self, tmp_path, empty_state, alive_check):
        schedule = tmp_path / "schedule.yaml"
        schedule.write_text("jobs: not-a-list")
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs == []
        assert snap.schedule_error is not None


# ---------------------------------------------------------------------------
# Snapshot — stale detection (post-upgrade)
# ---------------------------------------------------------------------------


class TestStaleDetection:
    def test_matching_version_and_executable_not_stale(self, empty_state, alive_check):
        _write_heartbeat(
            empty_state,
            "telegram",
            last_heartbeat="2026-05-19T13:00:00Z",
            version="0.1.0",
            executable="/venv/bin/python",
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
            self_version="0.1.0",
            self_executable="/venv/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "running"
        assert tg.stale is False

    def test_different_executable_marks_running_daemon_stale(
        self, empty_state, alive_check
    ):
        _write_heartbeat(
            empty_state,
            "telegram",
            last_heartbeat="2026-05-19T13:00:00Z",
            version="0.1.0",
            executable="/old-venv/bin/python",
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
            self_version="0.1.0",
            self_executable="/new-venv/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.stale is True

    def test_different_version_marks_running_daemon_stale(
        self, empty_state, alive_check
    ):
        _write_heartbeat(
            empty_state,
            "telegram",
            last_heartbeat="2026-05-19T13:00:00Z",
            version="0.1.0",
            executable="/venv/bin/python",
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
            self_version="0.2.0",
            self_executable="/venv/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.stale is True

    def test_stopped_daemon_never_stale(self, empty_state, dead_check):
        _write_heartbeat(
            empty_state,
            "telegram",
            version="0.1.0",
            executable="/old/bin/python",
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=dead_check,
            self_version="0.2.0",
            self_executable="/new/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.state == "stopped"
        assert tg.stale is False

    def test_missing_executable_field_does_not_false_positive(
        self, empty_state, alive_check
    ):
        # Pre-feature heartbeat (no executable field) — can't compare, treat as not stale.
        _write_heartbeat(
            empty_state,
            "telegram",
            last_heartbeat="2026-05-19T13:00:00Z",
            version="0.1.0",
            executable=None,
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
            self_version="0.1.0",
            self_executable="/whatever/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.stale is False
        assert tg.executable is None

    def test_propagates_version_and_executable_into_status(
        self, empty_state, alive_check
    ):
        _write_heartbeat(
            empty_state,
            "telegram",
            last_heartbeat="2026-05-19T13:00:00Z",
            version="0.1.0",
            executable="/venv/bin/python",
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
            self_version="0.1.0",
            self_executable="/venv/bin/python",
        )
        tg = next(f for f in snap.frontends if f.name == "telegram")
        assert tg.version == "0.1.0"
        assert tg.executable == "/venv/bin/python"


# ---------------------------------------------------------------------------
# Snapshot — top-level shape
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def test_returns_snapshot_with_timestamp(self, empty_state, alive_check):
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert isinstance(snap, Snapshot)
        assert isinstance(snap.timestamp, datetime)
        assert snap.timestamp.tzinfo is not None  # always tz-aware

    def test_uses_default_dirs_when_not_provided(
        self, monkeypatch, alive_check, tmp_path
    ):
        # Redirect STATE_DIR and DEFAULT_SCHEDULE_YAML via monkeypatch.
        from claude_on_the_fly.tui import state as state_mod

        monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            state_mod, "DEFAULT_SCHEDULE_YAML", tmp_path / "schedule.yaml"
        )
        snap = state_mod.snapshot(process_check=alive_check)
        assert isinstance(snap, Snapshot)
