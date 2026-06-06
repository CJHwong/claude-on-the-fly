"""Tests for tui.render — formatting helpers and json shape."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from claude_on_the_fly.tui.render import (
    _fmt_age,
    _fmt_uptime,
    _format_extra_notes,
    chat_header,
    frontends_table,
    render_snapshot_json,
    render_snapshot_rich,
    tab_label,
    tail_lines,
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


class TestTabLabel:
    def test_carries_index_and_health_glyph(self):
        assert tab_label(1, "symphony", "running").plain == "[1] ● symphony"
        assert tab_label(2, "scheduler", "stopped").plain == "[2] ○ scheduler"
        assert tab_label(1, "symphony", "broken").plain == "[1] ⚠ symphony"

    def test_styles_glyph_by_state(self):
        # The glyph reuses state_cell's style table, keyed by state.
        label = tab_label(1, "symphony", "running")
        glyph_span = next(s for s in label.spans if s.start == 4)
        assert glyph_span.style == "bold green"


class TestChatHeader:
    def _fe(self, name, state, age=2.0):
        return FrontendStatus(name=name, state=state, last_heartbeat_age_s=age)

    def test_single_frontend_reads_as_its_own_line(self):
        line = chat_header([self._fe("telegram", "running")], selected=0, active=1)
        assert "telegram" in line
        assert "running" in line
        assert "hb 2s" in line
        assert "1 active" in line
        # No roster prefix or selection highlight when there's only one.
        assert "CHAT" not in line
        assert "reverse" not in line

    def test_single_frontend_idle_when_no_active_jobs(self):
        line = chat_header([self._fe("slack", "running")], selected=0, active=0)
        assert "idle" in line
        assert "active" not in line.replace("idle", "")

    def test_multiple_frontends_collapse_to_glyph_strip(self):
        line = chat_header(
            [
                self._fe("telegram", "running"),
                self._fe("slack", "running"),
                self._fe("gmail", "stopped"),
            ],
            selected=1,
            active=3,
        )
        assert "CHAT" in line
        for name in ("telegram", "slack", "gmail"):
            assert name in line
        # Non-running frontends carry their state word so a down daemon shows.
        assert "stopped" in line
        assert "3 active" in line

    def test_selected_frontend_is_reverse_highlighted(self):
        frontends = [self._fe("telegram", "running"), self._fe("slack", "running")]
        line = chat_header(frontends, selected=1, active=0)
        # The selected cell is wrapped in reverse video; the other isn't.
        assert "[reverse] slack" in line
        assert "[reverse] telegram" not in line


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


class TestFormatExtraNotes:
    def test_scalars_only(self):
        assert _format_extra_notes({"a": 1, "b": "x"}) == "a=1, b=x"

    def test_lists_and_dicts_omitted(self):
        notes = _format_extra_notes(
            {"running": 2, "running_tickets": [{"id": "P-1"}], "meta": {"x": 1}}
        )
        assert notes == "running=2"

    def test_empty_dict(self):
        assert _format_extra_notes({}) == ""


class TestFrontendsTableWithNestedExtras:
    def test_running_tickets_omitted_from_notes(self):
        from claude_on_the_fly.tui.state import FrontendStatus

        f = FrontendStatus(
            name="symphony",
            state="running",
            pid=42,
            started_at="2026-05-19T12:00:00Z",
            last_heartbeat="2026-05-19T13:00:00Z",
            last_heartbeat_age_s=5.0,
            extra={
                "running": 2,
                "running_tickets": [{"identifier": "PROJ-1"}],
            },
        )
        out = self._render(
            frontends_table([f], datetime(2026, 5, 19, 13, 0, 5, tzinfo=timezone.utc))
        )
        assert "running=2" in out
        assert "PROJ-1" not in out

    def _render(self, table) -> str:
        import io

        buf = io.StringIO()
        Console(file=buf, width=160, force_terminal=False).print(table)
        return buf.getvalue()


class TestTailLines:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert tail_lines(tmp_path / "missing.log", 5) == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.log"
        p.write_text("")
        assert tail_lines(p, 5) == []

    def test_n_zero_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("a\nb\n")
        assert tail_lines(p, 0) == []

    def test_fewer_lines_than_n(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("one\ntwo\nthree\n")
        # Caller-visible contract: lines include their trailing newline.
        assert tail_lines(p, 10) == ["one\n", "two\n", "three\n"]

    def test_exactly_n_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("a\nb\nc\n")
        assert tail_lines(p, 3) == ["a\n", "b\n", "c\n"]

    def test_more_lines_than_n_returns_last_n(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("\n".join(str(i) for i in range(100)) + "\n")
        assert tail_lines(p, 5) == ["95\n", "96\n", "97\n", "98\n", "99\n"]

    def test_no_trailing_newline(self, tmp_path: Path) -> None:
        # File without a final newline still returns the last line intact.
        p = tmp_path / "log.log"
        p.write_text("alpha\nbeta\ngamma")
        # Last segment has no \n in the source, so we preserve it as-is
        # (callers strip trailing newlines anyway).
        result = tail_lines(p, 2)
        assert result == ["beta\n", "gamma\n"]

    def test_handles_large_file_without_loading_all(self, tmp_path: Path) -> None:
        # 200KB file with 2000 short lines; we ask for the last 5.
        p = tmp_path / "big.log"
        with p.open("w") as f:
            for i in range(2000):
                f.write(f"line-{i:04d}\n")
        result = tail_lines(p, 5)
        assert result == [
            "line-1995\n",
            "line-1996\n",
            "line-1997\n",
            "line-1998\n",
            "line-1999\n",
        ]

    def test_very_long_lines_force_chunk_growth(self, tmp_path: Path) -> None:
        # Lines longer than the initial 8KB read chunk — exercises the
        # exponential-growth path that handles worst-case line widths.
        p = tmp_path / "wide.log"
        big = "x" * 20_000
        p.write_text(f"first\n{big}\nlast\n")
        assert tail_lines(p, 1) == ["last\n"]
        assert tail_lines(p, 2) == [f"{big}\n", "last\n"]
