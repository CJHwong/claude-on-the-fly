"""Tests for tui.render — pure header/label builders for the dashboard."""

from __future__ import annotations

from types import SimpleNamespace

from claude_on_the_fly.tui.render import (
    scheduler_header,
    symphony_cap,
    symphony_header,
    tracker_labels,
)


def _cfg(**trackers):
    return SimpleNamespace(trackers=dict(trackers))


# ---------------------------------------------------------------------------
# tracker_labels / symphony_cap
# ---------------------------------------------------------------------------


class TestTrackerLabels:
    def test_jira_gets_project_key_suffix(self):
        cfg = _cfg(jira=SimpleNamespace(project_key="ACES", max_concurrent=3))
        assert tracker_labels(cfg) == ["jira:ACES"]

    def test_github_without_project_key_is_bare_name(self):
        cfg = _cfg(github=SimpleNamespace(max_concurrent=15))
        assert tracker_labels(cfg) == ["github"]

    def test_blank_project_key_falls_back_to_name(self):
        cfg = _cfg(jira=SimpleNamespace(project_key="", max_concurrent=1))
        assert tracker_labels(cfg) == ["jira"]

    def test_order_follows_insertion(self):
        cfg = _cfg(
            jira=SimpleNamespace(project_key="ACES", max_concurrent=3),
            github=SimpleNamespace(max_concurrent=15),
        )
        assert tracker_labels(cfg) == ["jira:ACES", "github"]


class TestSymphonyCap:
    def test_sums_per_tracker(self):
        cfg = _cfg(
            jira=SimpleNamespace(max_concurrent=3),
            github=SimpleNamespace(max_concurrent=15),
        )
        assert symphony_cap(cfg) == 18

    def test_missing_field_counts_zero(self):
        cfg = _cfg(jira=SimpleNamespace())
        assert symphony_cap(cfg) == 0


def test_helpers_agree_with_real_example_config(tmp_path):
    """Integration: the shipped EXAMPLE_YAML must drive the helpers, not just a
    SimpleNamespace fake."""
    from claude_on_the_fly.symphony.config import EXAMPLE_YAML, load_config

    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(EXAMPLE_YAML)
    cfg = load_config(cfg_path)

    assert tracker_labels(cfg) == ["jira:PROJ", "github"]
    expected = (
        cfg.trackers["jira"].max_concurrent + cfg.trackers["github"].max_concurrent
    )
    assert symphony_cap(cfg) == expected


# ---------------------------------------------------------------------------
# symphony_header
# ---------------------------------------------------------------------------


class TestSymphonyHeader:
    def test_running_shows_cap_and_labels(self):
        line = symphony_header(
            state="running",
            running=2,
            cap=15,
            labels=["jira:ACES", "github"],
            hb_age_s=3.0,
        )
        assert "SYMPHONY" in line
        assert "2/15" in line
        assert "jira:ACES" in line
        assert "github" in line
        assert "hb 3s" in line
        assert "green" in line  # running is bold green

    def test_stopped_dims_state_and_keeps_cap(self):
        line = symphony_header(
            state="stopped", running=0, cap=15, labels=[], hb_age_s=None
        )
        assert "0/15" in line
        assert "dim" in line  # stopped state style
        assert "hb" not in line  # no heartbeat age when None

    def test_zero_cap_falls_back_to_running_count(self):
        line = symphony_header(
            state="running", running=1, cap=0, labels=[], hb_age_s=None
        )
        assert "1 running" in line
        assert "1/0" not in line

    def test_stale_and_error_annotations(self):
        line = symphony_header(
            state="broken",
            running=1,
            cap=2,
            labels=[],
            hb_age_s=90.0,
            error="config error",
            stale=True,
        )
        assert "stale" in line
        assert "config error" in line


# ---------------------------------------------------------------------------
# scheduler_header
# ---------------------------------------------------------------------------


class TestSchedulerHeader:
    def test_running_with_next_fire(self):
        line = scheduler_header(state="running", next_fire_str="Mon 09:00  (in 4m)")
        assert "SCHEDULER" in line
        assert "next fire" in line
        assert "in 4m" in line

    def test_running_no_jobs(self):
        line = scheduler_header(state="running", next_fire_str=None)
        assert "no jobs" in line

    def test_stopped_omits_next_fire(self):
        line = scheduler_header(state="stopped", next_fire_str="Mon 09:00  (in 4m)")
        assert "next fire" not in line
        assert "dim" in line  # stopped state style

    def test_error_takes_precedence(self):
        line = scheduler_header(
            state="running",
            next_fire_str="Mon 09:00  (in 4m)",
            schedule_error="bad cron",
        )
        assert "bad cron" in line
        assert "next fire" not in line
