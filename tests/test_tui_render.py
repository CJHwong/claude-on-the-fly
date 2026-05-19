"""Tests for tui.render — formatting helpers and json shape."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from rich.console import Console

from claude_on_the_fly.tui.render import (
    _fmt_age,
    _fmt_uptime,
    render_snapshot_json,
    render_snapshot_rich,
)
from claude_on_the_fly.tui.state import FrontendStatus, JobInfo, Snapshot


def _make_snapshot(**overrides):
    defaults = {
        "timestamp": datetime(2026, 5, 19, 13, 0, 5, tzinfo=timezone.utc),
        "frontends": [
            FrontendStatus(
                name="telegram",
                state="running",
                pid=12345,
                started_at="2026-05-19T12:00:00Z",
                last_heartbeat="2026-05-19T13:00:00Z",
                last_heartbeat_age_s=5.0,
                extra={"queue_depth": 0},
            ),
            FrontendStatus(name="slack", state="stopped"),
        ],
        "jobs": [
            JobInfo(
                name="digest",
                cron="0 9 * * *",
                kind="prompt",
                next_fire=datetime(2026, 5, 20, 9, 0, 0),
            )
        ],
        "schedule_error": None,
    }
    defaults.update(overrides)
    return Snapshot(
        timestamp=defaults["timestamp"],
        frontends=defaults["frontends"],
        jobs=defaults["jobs"],
        schedule_error=defaults["schedule_error"],
    )


class TestFmtHelpers:
    def test_fmt_age_none(self):
        assert _fmt_age(None) == "-"

    def test_fmt_age_seconds(self):
        assert _fmt_age(5) == "5s"

    def test_fmt_age_minutes(self):
        assert _fmt_age(120) == "2.0m"

    def test_fmt_age_hours(self):
        assert _fmt_age(7200) == "2.0h"

    def test_fmt_uptime_invalid_returns_dash(self):
        now = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        assert _fmt_uptime(None, now) == "-"
        assert _fmt_uptime("garbage", now) == "-"


class TestRichRender:
    def test_rich_render_runs_without_error(self):
        snap = _make_snapshot()
        console = Console(record=True, width=120)
        render_snapshot_rich(snap, console)
        text = console.export_text()
        assert "Frontends" in text
        assert "telegram" in text
        assert "running" in text
        assert "Scheduled jobs" in text
        assert "digest" in text

    def test_no_schedule_shown_when_empty(self):
        snap = _make_snapshot(jobs=[])
        console = Console(record=True, width=120)
        render_snapshot_rich(snap, console)
        text = console.export_text()
        assert "No schedule.yaml" in text

    def test_schedule_error_shown(self):
        snap = _make_snapshot(jobs=[], schedule_error="bad cron")
        console = Console(record=True, width=120)
        render_snapshot_rich(snap, console)
        text = console.export_text()
        assert "Scheduler config error" in text
        assert "bad cron" in text


class TestJsonRender:
    def test_json_shape(self):
        snap = _make_snapshot()
        payload = json.loads(render_snapshot_json(snap))
        assert "timestamp" in payload
        assert "frontends" in payload
        assert "jobs" in payload
        assert payload["frontends"][0]["name"] == "telegram"
        assert payload["frontends"][0]["state"] == "running"
        assert payload["jobs"][0]["name"] == "digest"

    def test_json_is_valid(self):
        snap = _make_snapshot()
        # Round-trips.
        payload = render_snapshot_json(snap)
        assert json.loads(payload)

    def test_schedule_error_in_json(self):
        snap = _make_snapshot(jobs=[], schedule_error="boom")
        payload = json.loads(render_snapshot_json(snap))
        assert payload["schedule_error"] == "boom"
