"""Integration test for the history screen's watch pane.

Reproduces the live symptom this originally caught ("no session yet" while a
JSONL is being written) end to end: writes a realistic event log, drops a session
JSONL at the path the watch is supposed to compute, boots the TUI, navigates to
the history screen, toggles watch on the row, and asserts the pane rendered
something other than the empty-state placeholder.

It covers a chain no unit test spans: event-log read → aggregation →
`_resolve_session` → `session_log_path` → `tail_lines` → `format_event` →
`RichLog` write. A break anywhere in it shows up as a placeholder, which is
indistinguishable from "nothing has run yet" to anyone looking at the screen.

The row is a cron row, and the session is resolved from the `workspace` and
`session_uuid` the dispatching side recorded on the event — not re-derived here.
That is the point: the resolution has to agree with whatever the producer wrote,
and a test that recomputed the path would agree with itself instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_on_the_fly.transcript import _workspace_to_claude_hash


def _write_events(path: Path, events: list[dict]) -> None:
    """Write an ndjson event log at `path` for `EventLog.tail` to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _cron_dispatched(
    *, ts: str, identifier: str, backend: str, workspace: Path, session_uuid: str
) -> dict:
    """A `dispatched` row shaped like the one the job path records."""
    return {
        "ts": ts,
        "type": "dispatched",
        "source": "cron",
        "backend": backend,
        "identifier": identifier,
        "workspace": str(workspace),
        "session_uuid": session_uuid,
        "state": "open",
    }


def _claude_assistant_text(text: str) -> dict:
    """A minimal stream-json assistant turn that `format_event` accepts."""
    return {
        "type": "assistant",
        "timestamp": datetime.now(UTC).isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


@pytest.mark.asyncio
async def test_watch_pane_renders_jsonl_for_a_cron_row(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_MODE", "native")
    monkeypatch.delenv("CODEX_MODE", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    projects_dir = tmp_path / "claude-projects"
    projects_dir.mkdir()
    event_log_path = tmp_path / "events.jsonl"

    monkeypatch.setattr(
        "claude_on_the_fly.transcript.CLAUDE_PROJECTS_DIR", projects_dir
    )
    # EventLog.__init__'s default arg is captured at function definition, so
    # patching `events.DEFAULT_PATH` is silently ignored. Wrap the class so a
    # no-arg `EventLog()` reads from our tmp file.
    from claude_on_the_fly.events import EventLog as _RealEventLog

    class _TestEventLog(_RealEventLog):
        def __init__(self, path: Path = event_log_path) -> None:
            super().__init__(path)

    monkeypatch.setattr("claude_on_the_fly.tui.screens.history.EventLog", _TestEventLog)

    # A keyed cron job's workspace, laid out the way jobs/agent_runner.py does:
    # <data_dir>/workspaces/<platform>/<safe session key>.
    identifier = "jira/ACE-1234"
    backend_key = "claude:native:sonnet"
    workspace = tmp_path / "workspaces" / "cron" / "jira_ACE-1234"
    workspace.mkdir(parents=True)
    session_uuid = "d1d84e57-70e2-5a88-b716-a7c799dca9a0"

    session_dir = projects_dir / _workspace_to_claude_hash(workspace)
    session_dir.mkdir(parents=True, exist_ok=True)
    jsonl = session_dir / f"{session_uuid}.jsonl"
    jsonl.write_text(json.dumps(_claude_assistant_text("hello from worker")) + "\n")

    _write_events(
        event_log_path,
        [
            _cron_dispatched(
                ts="2026-05-25T20:00:00+00:00",
                identifier=identifier,
                backend=backend_key,
                workspace=workspace,
                session_uuid=session_uuid,
            ),
        ],
    )

    from textual.widgets import DataTable, RichLog

    from claude_on_the_fly.tui.screens.history import HistoryScreen
    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")  # dashboard → history
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)

        # Refresh now that the patched EventLog is in place: on_mount ran before
        # these patches landed.
        screen.action_refresh_now()
        await pilot.pause()

        table = screen.query_one("#history-table", DataTable)
        assert table.row_count >= 1, "the dispatched row should be present"

        await pilot.press("w")  # toggle the watch pane on the highlighted row
        await pilot.pause()
        # action_toggle_watch already forced a refresh, but the 1s refresher has
        # not ticked. One explicit call mirrors what the live TUI does once the
        # file's mtime advances.
        screen._refresh_watch_pane(force_reload=True)
        await pilot.pause()

        pane = screen.query_one("#hist-watch-pane", RichLog)
        rendered = "\n".join(str(line) for line in pane.lines)
        assert "no session log yet" not in rendered, (
            "watch pane should NOT show the empty-state placeholder when the "
            f"JSONL exists at {jsonl}; rendered={rendered!r}"
        )
        assert "hello from worker" in rendered, (
            f"watch pane should tail the session JSONL; rendered={rendered!r}"
        )


@pytest.mark.asyncio
async def test_a_row_without_a_recorded_session_stays_on_the_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half of the contract: `_resolve_session` returns None when the
    event recorded no `session_uuid`, and the pane must say so plainly rather
    than pointing at a path it guessed."""
    monkeypatch.setenv("CLAUDE_MODE", "native")
    event_log_path = tmp_path / "events.jsonl"

    from claude_on_the_fly.events import EventLog as _RealEventLog

    class _TestEventLog(_RealEventLog):
        def __init__(self, path: Path = event_log_path) -> None:
            super().__init__(path)

    monkeypatch.setattr("claude_on_the_fly.tui.screens.history.EventLog", _TestEventLog)
    _write_events(
        event_log_path,
        [
            {
                "ts": "2026-05-25T20:00:00+00:00",
                "type": "dispatched",
                "source": "cron",
                "backend": "claude:native:sonnet",
                "identifier": "jira/ACE-9",
                "state": "open",
            }
        ],
    )

    from claude_on_the_fly.tui.screens.history import HistoryScreen
    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        screen.action_refresh_now()
        await pilot.pause()

        assert screen._row_watch == {}, (
            "a row with no recorded session_uuid must not resolve to a guessed path"
        )
