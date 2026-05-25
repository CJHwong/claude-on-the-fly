"""Integration test for the history screen's watch pane.

Reproduces the live symptom the user reported ("no session yet" while a
JSONL is being written) end-to-end: writes a realistic symphony event log,
drops a session JSONL at the path the watch is supposed to compute, boots
the TUI, navigates to the history screen, toggles watch on the aggregated
row, and asserts the pane rendered something other than the empty-state
placeholder. Single test per scenario — keeps the failure mode obvious if
it regresses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_on_the_fly.symphony.agent_runner import session_uuid_for
from claude_on_the_fly.transcript import _workspace_to_claude_hash


def _write_events(path: Path, events: list[dict]) -> None:
    """Write an ndjson event log at `path` for `EventLog.tail` to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _symphony_dispatched(
    *, ts: str, identifier: str, backend: str, tracker: str = "github"
) -> dict:
    return {
        "ts": ts,
        "type": "dispatched",
        "source": "symphony",
        "tracker": tracker,
        "backend": backend,
        "identifier": identifier,
        "state": "open",
    }


def _claude_assistant_text(text: str) -> dict:
    """A minimal stream-json assistant turn the watch's format_event accepts."""
    return {
        "type": "assistant",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


@pytest.mark.asyncio
async def test_watch_pane_renders_jsonl_for_symphony_row(
    tmp_path: Path, monkeypatch
) -> None:
    """The end-to-end smoke that the live TUI was failing: an aggregated
    symphony row whose JSONL exists on disk must render in the watch pane
    when the user presses `w`. Catches every silent break in the chain:
    event-log read, aggregation, _resolve_session, session_log_path,
    tail_lines, format_event, RichLog write.
    """
    # Isolate every shared path the TUI / backend touch.
    monkeypatch.setenv("CLAUDE_MODE", "native")
    monkeypatch.delenv("CODEX_MODE", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    projects_dir = tmp_path / "claude-projects"
    projects_dir.mkdir()
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir()
    event_log_path = tmp_path / "events.jsonl"

    monkeypatch.setattr(
        "claude_on_the_fly.transcript.CLAUDE_PROJECTS_DIR", projects_dir
    )
    monkeypatch.setattr(
        "claude_on_the_fly.tui.screens.history.WORKSPACES_ROOT", workspaces_root
    )
    # EventLog.__init__'s default arg is captured at function definition,
    # so patching `events.DEFAULT_PATH` is silently ignored. Wrap the class
    # so `EventLog()` (no-arg) reads from our tmp file.
    from claude_on_the_fly.events import EventLog as _RealEventLog

    class _TestEventLog(_RealEventLog):
        def __init__(self, path: Path = event_log_path) -> None:
            super().__init__(path)

    monkeypatch.setattr("claude_on_the_fly.tui.screens.history.EventLog", _TestEventLog)

    identifier = "owner/repo#42"
    backend_key = "claude:native:sonnet"
    workspace = workspaces_root / "github" / "owner_repo_42"
    workspace.mkdir(parents=True)

    # The session uuid the watch pane will derive from the row's backend
    # field — match exactly so the JSONL we drop on disk is found.
    sid = session_uuid_for(identifier, source="github", backend_key=backend_key)
    session_dir = projects_dir / _workspace_to_claude_hash(workspace)
    session_dir.mkdir(parents=True, exist_ok=True)
    jsonl = session_dir / f"{sid}.jsonl"
    jsonl.write_text(json.dumps(_claude_assistant_text("hello from worker")) + "\n")

    _write_events(
        event_log_path,
        [
            _symphony_dispatched(
                ts="2026-05-25T20:00:00+00:00",
                identifier=identifier,
                backend=backend_key,
            ),
        ],
    )

    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp
    from textual.widgets import DataTable, RichLog

    from claude_on_the_fly.tui.screens.history import HistoryScreen

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Dashboard → History.
        await pilot.press("h")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)

        # Force a refresh now that the env is patched in (the screen's
        # on_mount ran before our patches landed for some symbols).
        screen.action_refresh_now()
        await pilot.pause()

        table = screen.query_one("#history-table", DataTable)
        assert table.row_count >= 1, "aggregated row should be present"

        # Toggle the watch pane on the highlighted row (the only one).
        await pilot.press("w")
        await pilot.pause()
        # action_toggle_watch already called _refresh_watch_pane(force_reload=True)
        # — but the periodic 1s refresher hasn't ticked yet. One more explicit
        # call mirrors what the live TUI does after the file's mtime advances.
        screen._refresh_watch_pane(force_reload=True)
        await pilot.pause()

        pane = screen.query_one("#hist-watch-pane", RichLog)
        # RichLog renders into a list of Strip objects; flatten their Segments
        # to plain text so the assertion is content-only.
        rendered = "\n".join(str(line) for line in pane.lines)
        assert "no session log yet" not in rendered, (
            "watch pane should NOT show the empty-state placeholder when the "
            f"JSONL exists at {jsonl}; rendered={rendered!r}"
        )
        assert "hello from worker" in rendered, (
            f"watch pane should tail the session JSONL; rendered={rendered!r}"
        )


@pytest.mark.asyncio
async def test_dashboard_session_uuid_uses_current_backend_key(
    tmp_path: Path, monkeypatch
) -> None:
    """Dashboard's watch pane needs to derive session_uuid from the
    daemon's CURRENT backend_key, not the `session_uuid_for` default.
    Without this, switching CLAUDE_MODE makes the dashboard look for
    the JSONL at the wrong UUID and the pane gets stuck on
    "agent hasn't run a turn".
    """
    monkeypatch.setenv("CLAUDE_MODE", "snap")
    monkeypatch.delenv("CODEX_MODE", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    # Snap backend resolution checks for the binary at construct time;
    # point CLAUDE_INTERACTIVE_P_HOME at a fake one so get_backend() works
    # without touching the real install.
    fake_snap = tmp_path / "snap-home"
    (fake_snap / "bin").mkdir(parents=True)
    fake_bin = fake_snap / "bin" / "claude-snap"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("CLAUDE_INTERACTIVE_P_HOME", str(fake_snap))

    # Force the dashboard's snapshot to report one running symphony ticket
    # so _refresh populates _job_sessions with our identifier.
    from datetime import datetime, timezone as tz
    from claude_on_the_fly.tui.state import FrontendStatus, Snapshot

    identifier = "owner/repo#99"
    fake_snapshot = Snapshot(
        timestamp=datetime.now(tz.utc),
        frontends=[
            FrontendStatus(
                name="symphony",
                state="running",
                pid=12345,
                started_at="2026-05-25T00:00:00Z",
                last_heartbeat="2026-05-25T00:00:00Z",
                last_heartbeat_age_s=1.0,
                extra={
                    "running_tickets": [
                        {
                            "identifier": identifier,
                            "source": "github",
                            "state": "open",
                            "uptime_s": 5,
                            "last_turn_end_age_s": None,
                            "failure_attempt": 0,
                        }
                    ]
                },
            )
        ],
        jobs=[],
    )
    monkeypatch.setattr(
        "claude_on_the_fly.tui.screens.dashboard.state.snapshot",
        lambda: fake_snapshot,
    )

    from claude_on_the_fly.agent import current_backend_key
    from claude_on_the_fly.symphony.agent_runner import session_uuid_for
    from claude_on_the_fly.tui.screens.dashboard import DashboardScreen
    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp

    expected_uuid = session_uuid_for(
        identifier, source="github", backend_key=current_backend_key()
    )

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        screen._refresh()
        await pilot.pause()
        actual = screen._job_sessions.get(f"symphony:{identifier}")
        assert actual == expected_uuid, (
            f"dashboard derived {actual!r}, expected {expected_uuid!r} from "
            f"current_backend_key()={current_backend_key()!r}"
        )
