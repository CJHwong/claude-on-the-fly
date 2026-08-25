"""Tests for tui.state.snapshot()."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.tui import state
from claude_on_the_fly.tui.state import (
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
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


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

    def test_extra_is_propagated(self, empty_state, alive_check):
        _write_heartbeat(
            empty_state,
            "cron",
            last_heartbeat="2026-05-19T13:00:00Z",
            extra={"running": 3, "retry_queue": 1},
        )
        snap = snapshot(
            empty_state,
            None,
            now=_at("2026-05-19T13:00:05Z"),
            process_check=alive_check,
        )
        s = next(f for f in snap.frontends if f.name == "cron")
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
    path.write_text(yaml.safe_dump({"entries": jobs}))


class TestJobs:
    def test_no_schedule_yaml_means_empty_jobs(self, empty_state, alive_check):
        snap = snapshot(
            empty_state, Path("/nonexistent/schedule.yaml"), process_check=alive_check
        )
        assert snap.jobs == []
        assert snap.schedule_error is None

    def test_well_formed_schedule_yields_jobs(self, tmp_path, empty_state, alive_check):
        schedule = tmp_path / "cron.yaml"
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
        schedule = tmp_path / "cron.yaml"
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
        schedule = tmp_path / "cron.yaml"
        schedule.write_text("entries: not-a-list")
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs == []
        assert snap.schedule_error is not None

    def test_job_detail_prefers_the_command(self, tmp_path, empty_state, alive_check):
        """A producer's prompt is the template applied to each item the command
        lists; the command is what distinguishes the job, so it wins."""
        schedule = tmp_path / "cron.yaml"
        _write_schedule(
            schedule,
            [
                {
                    "name": "prune",
                    "cron": "0 4 * * *",
                    "command": "~/scripts/prune.sh --verbose",
                    "prompt": "summarise the output",
                },
            ],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs[0].detail == "~/scripts/prune.sh --verbose"

    def test_job_detail_keeps_the_prompt_text(self, tmp_path, empty_state, alive_check):
        """The detail block shows the prompt as written (newlines intact); the
        table cell collapses whitespace at display time."""
        schedule = tmp_path / "cron.yaml"
        _write_schedule(
            schedule,
            [
                {
                    "name": "digest",
                    "cron": "0 9 * * *",
                    "prompt": "summarise\nmy inbox\n\nplease",
                },
            ],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs[0].detail == "summarise\nmy inbox\n\nplease"

    def test_job_detail_shows_the_prompt_file_path(
        self, tmp_path, empty_state, alive_check
    ):
        """The file is not read per tick (a fire happens at most once a minute;
        the table refreshes every second) — the path is what the operator needs
        to see."""
        prompt_file = tmp_path / "prompts" / "digest.md"
        prompt_file.parent.mkdir()
        prompt_file.write_text("summarise my inbox")
        schedule = tmp_path / "cron.yaml"
        _write_schedule(
            schedule,
            [
                {
                    "name": "digest",
                    "cron": "0 9 * * *",
                    "prompt_file": str(prompt_file),
                },
            ],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs[0].detail == str(prompt_file)

    def test_job_detail_keeps_a_long_prompt_in_full(
        self, tmp_path, empty_state, alive_check
    ):
        """The detail block shows the whole prompt; only the table cell clips."""
        schedule = tmp_path / "cron.yaml"
        _write_schedule(
            schedule,
            [{"name": "digest", "cron": "0 9 * * *", "prompt": "x" * 500}],
        )
        snap = snapshot(empty_state, schedule, process_check=alive_check)
        assert snap.jobs[0].detail == "x" * 500


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
        monkeypatch.setattr(state_mod, "DEFAULT_SCHEDULE_YAML", tmp_path / "cron.yaml")
        snap = state_mod.snapshot(process_check=alive_check)
        assert isinstance(snap, Snapshot)


# ---------------------------------------------------------------------------
# Snapshot — background-job queue
# ---------------------------------------------------------------------------


class TestJobsQueueView:
    """snapshot() observes the jobs maildir directly, so queue depth is visible
    even with the worker stopped — and observing it must never build it."""

    @pytest.fixture
    def jobs_root(self, isolate_jobs_dir):
        """conftest's autouse fixture already points DEFAULT_JOBS_DIR at a tmp
        maildir — this is only a local name for it."""
        return isolate_jobs_dir

    @staticmethod
    def _drop(root: Path, stage: str, job_id: str, prompt: str = "do it") -> None:
        """Hand-place a job file — the queue's own writers are exercised in
        tests/jobs/test_file_queue.py; here we only need the layout."""
        (root / stage).mkdir(parents=True, exist_ok=True)
        (root / stage / f"{job_id}.json").write_text(
            json.dumps({"id": job_id, "prompt": prompt, "origin": {}}),
            encoding="utf-8",
        )

    def test_missing_maildir_reads_as_empty_and_stays_missing(
        self, jobs_root, empty_state, alive_check
    ):
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is not None
        depth = snap.jobs_queue.depth
        assert (depth.new, depth.running, depth.done, depth.failed) == (0, 0, 0, 0)
        assert snap.jobs_queue.rows == []
        # The read-only contract: the TUI must never create the worker's maildir.
        assert not jobs_root.exists()

    def test_counts_and_rows_reflect_the_maildir(
        self, jobs_root, empty_state, alive_check
    ):
        self._drop(jobs_root, "cur", "100-a", prompt="in flight")
        self._drop(jobs_root, "new", "200-b", prompt="queued next")
        self._drop(jobs_root, "failed", "050-x")
        (jobs_root / "done").mkdir(parents=True, exist_ok=True)
        # complete() writes BOTH of these; the job must count once.
        (jobs_root / "done" / "010-z.json").write_text("{}", encoding="utf-8")
        (jobs_root / "done" / "010-z.result.json").write_text("{}", encoding="utf-8")

        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is not None
        depth = snap.jobs_queue.depth
        assert (depth.new, depth.running, depth.done, depth.failed) == (1, 1, 1, 1)
        assert [(r.id, r.in_flight) for r in snap.jobs_queue.rows] == [
            ("100-a", True),
            ("200-b", False),
        ]
        assert snap.jobs_queue.rows[0].prompt == "in flight"

    def test_rows_are_capped(self, jobs_root, empty_state, alive_check):
        from claude_on_the_fly.jobs.file_queue import DEFAULT_ROW_LIMIT

        for i in range(DEFAULT_ROW_LIMIT + 5):
            self._drop(jobs_root, "new", f"{1000 + i}-x")
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is not None
        assert len(snap.jobs_queue.rows) == DEFAULT_ROW_LIMIT
        assert snap.jobs_queue.hidden == 5  # what the cap left out

    def test_a_short_page_is_never_reported_as_truncated(
        self, jobs_root, empty_state, alive_check, monkeypatch
    ):
        """Depth and rows are two reads and a job can finish between them, so a
        page shorter than the cap is that race — never a hidden row."""
        from claude_on_the_fly.tui import state as state_mod

        for i in range(3):
            self._drop(jobs_root, "new", f"{100 + i}-x")
        # As if all three were claimed and completed after the depth read.
        monkeypatch.setattr(state_mod, "read_queue_rows", lambda root, limit: [])

        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is not None
        assert snap.jobs_queue.depth.new == 3
        assert snap.jobs_queue.hidden == 0

    def test_non_file_queue_kind_yields_none(
        self, jobs_root, empty_state, alive_check, monkeypatch
    ):
        """A broker-backed queue keeps its state somewhere this reader can't
        see; reporting zeros would be a lie, so the view is None."""
        self._drop(jobs_root, "new", "100-a")
        monkeypatch.setenv("JOBS_QUEUE_KIND", "redis")
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is None

    def test_queue_kind_is_read_from_the_env_file(
        self, jobs_root, empty_state, alive_check, isolate_env_file, monkeypatch
    ):
        """Set in the .env FILE, not the process environment — which is where a
        real deployment sets it, and no TUI module calls load_dotenv(). Reading
        os.environ alone renders a broker-backed queue as an all-zero maildir."""
        # The file has to be the only source, or a shell that exports the var
        # passes this test against the very read it exists to rule out.
        monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)
        self._drop(jobs_root, "new", "100-a")
        isolate_env_file.write_text("JOBS_QUEUE_KIND=redis\n", encoding="utf-8")
        snap = snapshot(empty_state, None, process_check=alive_check)
        assert snap.jobs_queue is None


class TestTuiVersion:
    def test_an_uninstalled_package_reports_unknown(self, monkeypatch):
        """The version sits in the dashboard header; a raise there would take the
        whole TUI down over a cosmetic field."""
        from importlib.metadata import PackageNotFoundError

        from claude_on_the_fly.tui import state as state_mod

        def not_found(_name):
            raise PackageNotFoundError("claude-on-the-fly")

        monkeypatch.setattr(state_mod, "_pkg_version", not_found)
        assert state_mod.tui_version() == "unknown"


class TestUnusableHeartbeats:
    def test_a_bad_timestamp_reads_as_stopped_with_the_reason(self, tmp_path):
        """ "Stopped, because the timestamp is unreadable" is actionable; a crashed
        dashboard is not."""
        import json
        from datetime import UTC, datetime

        from claude_on_the_fly.tui import state as state_mod

        path = tmp_path / "slack.json"
        path.write_text(json.dumps({"pid": 4242, "last_heartbeat": "yesterday"}))
        status = state_mod._frontend_status_from_heartbeat(
            "slack",
            state_dir=tmp_path,
            now=datetime.now(UTC),
            process_check=lambda _pid: True,
            self_version="0.1.0",
            self_executable="/usr/bin/python",
        )
        assert status.state == "stopped"
        assert "bad heartbeat timestamp" in (status.error or "")


class TestScheduleCache:
    def test_an_unchanged_file_is_not_reparsed(self, tmp_path, monkeypatch):
        """The dashboard ticks once a second, and reparsing the YAML every tick is
        pure waste when the file has not moved."""
        import yaml

        from claude_on_the_fly.tui import state as state_mod

        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {"entries": [{"name": "a", "cron": "* * * * *", "prompt": "x"}]}
            )
        )
        monkeypatch.setattr(state_mod, "_schedule_cache", None)
        parses = {"n": 0}
        real_load = state_mod.load_cron_config

        def counting_load(p):
            parses["n"] += 1
            return real_load(p)

        monkeypatch.setattr(state_mod, "load_cron_config", counting_load)
        first = state_mod._load_schedule_cached(path)
        second = state_mod._load_schedule_cached(path)
        assert parses["n"] == 1
        assert first is second

    def test_a_touched_file_is_reparsed(self, tmp_path, monkeypatch):
        import os

        import yaml

        from claude_on_the_fly.tui import state as state_mod

        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {"entries": [{"name": "a", "cron": "* * * * *", "prompt": "x"}]}
            )
        )
        monkeypatch.setattr(state_mod, "_schedule_cache", None)
        state_mod._load_schedule_cached(path)
        os.utime(path, (0, 0))
        parses = {"n": 0}
        real_load = state_mod.load_cron_config

        def counting_load(p):
            parses["n"] += 1
            return real_load(p)

        monkeypatch.setattr(state_mod, "load_cron_config", counting_load)
        state_mod._load_schedule_cached(path)
        assert parses["n"] == 1


def test_the_queue_kind_is_cached_until_the_env_file_changes(tmp_path, monkeypatch):
    """Read on every dashboard tick, and each miss re-reads and re-parses the env
    file for a value that almost never changes."""
    from claude_on_the_fly import envfile
    from claude_on_the_fly.tui import state as state_mod

    env_file = tmp_path / ".env"
    env_file.write_text("JOBS_QUEUE_KIND=file\n")
    monkeypatch.setattr(envfile, "default_env_file", lambda: env_file)
    monkeypatch.setattr(state_mod, "_queue_kind_cache", None)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)
    reads = {"n": 0}
    real_load = envfile.merged

    def counting_load(path):
        reads["n"] += 1
        return real_load(path)

    monkeypatch.setattr(envfile, "merged", counting_load)
    assert state_mod._queue_kind() == "file"
    assert state_mod._queue_kind() == "file"
    assert reads["n"] == 1


class TestManagedReleaseStaleness:
    """A deployment that flips a `current` release symlink.

    Both the daemon and the reader are launched through the symlink, so both
    record the same literal path. Comparing those two strings, or two live
    resolutions of them, can never show a release change.
    """

    @staticmethod
    def _releases(tmp_path):
        for name in ("A", "B"):
            (tmp_path / name / ".venv" / "bin").mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(tmp_path / "A")
        return current, str(current / ".venv" / "bin" / "python")

    def test_the_exact_string_comparison_cannot_see_a_flip(self, tmp_path):
        """Why the resolved field exists. This is the old behaviour, and it is
        the bug: after the switch the daemon still reads as current."""
        current, launcher = self._releases(tmp_path)
        current.unlink()
        current.symlink_to(tmp_path / "B")

        assert (
            state._is_stale("running", "1.0", launcher, "1.0", launcher, None) is False
        )

    def test_a_flip_makes_the_running_daemon_stale(self, tmp_path):
        current, launcher = self._releases(tmp_path)
        # What the daemon froze at startup, while current still pointed at A.
        daemon_resolved = str((tmp_path / "A" / ".venv" / "bin").resolve())

        current.unlink()
        current.symlink_to(tmp_path / "B")

        assert (
            state._is_stale(
                "running", "1.0", launcher, "1.0", launcher, daemon_resolved
            )
            is True
        )

    def test_before_a_flip_the_daemon_is_current(self, tmp_path):
        _current, launcher = self._releases(tmp_path)
        daemon_resolved = str((tmp_path / "A" / ".venv" / "bin").resolve())

        assert (
            state._is_stale(
                "running", "1.0", launcher, "1.0", launcher, daemon_resolved
            )
            is False
        )

    def test_an_older_daemon_without_the_field_keeps_the_old_comparison(self):
        """Missing fields read as "cannot tell" so a rollout does not go amber
        for every daemon started before this shipped."""
        assert (
            state._is_stale("running", "1.0", "/a/bin/py", "1.0", "/b/bin/py") is True
        )
        assert state._is_stale("running", "1.0", None, "1.0", "/b/bin/py") is False

    def test_a_stopped_daemon_is_never_stale(self, tmp_path):
        _current, launcher = self._releases(tmp_path)
        assert (
            state._is_stale("stopped", "9.9", launcher, "1.0", launcher, "/gone")
            is False
        )

    def test_a_version_difference_still_wins(self, tmp_path):
        _current, launcher = self._releases(tmp_path)
        daemon_resolved = str((tmp_path / "A" / ".venv" / "bin").resolve())
        assert (
            state._is_stale(
                "running", "0.9", launcher, "1.0", launcher, daemon_resolved
            )
            is True
        )
