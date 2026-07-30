"""The history screen driven as a real Textual screen.

The pure row formatters are covered in test_tui_history.py. What is left is the
behaviour an operator actually depends on: the two view modes, the source filter,
the takeover clipboard, the PR link, and the watch pane that tails a row's session
log. Every one of those has a "this row cannot do that" path, and each has to say so
rather than doing nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable, RichLog, Static

from claude_on_the_fly.events import EventLog
from claude_on_the_fly.tui.screens import history as history_mod
from claude_on_the_fly.tui.screens.history import HistoryScreen


class _Host(App):
    CSS = """
    #overlay-box { height: 1fr; }
    #hist-table-wrap { height: 1fr; min-height: 6; }
    #hist-watch-wrap { height: 50%; min-height: 6; }
    #history-table, #hist-watch-pane { height: 1fr; min-height: 4; }
    """


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    monkeypatch.setattr(history_mod, "EventLog", lambda *_a, **_kw: EventLog(path))
    return log


def _event(**fields) -> dict:
    base = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "type": "dispatched",
        "source": "cron",
        "identifier": "ACE-1",
    }
    base.update(fields)
    return base


def _write(log: EventLog, *events: dict) -> None:
    for event in events:
        extra = {
            k: v for k, v in event.items() if k not in ("type", "source", "identifier")
        }
        log.append(
            event["type"],
            source=event["source"],
            identifier=event["identifier"],
            **extra,
        )


async def _open(app: _Host, pilot) -> HistoryScreen:
    screen = HistoryScreen()
    await app.push_screen(screen)
    await pilot.pause()
    return screen


def _rows(app: _Host) -> list[list[str]]:
    table = app.screen.query_one("#history-table", DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


class TestEmptyLog:
    async def test_an_empty_log_shows_a_placeholder_row(self, event_log):
        """An empty table with no explanation reads as a broken screen."""
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            assert any("no events" in " ".join(row) for row in _rows(app))

    async def test_the_events_view_also_shows_a_placeholder(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("a")
            await pilot.pause()
            assert any("no events" in " ".join(row) for row in _rows(app))


class TestViewModes:
    async def test_aggregated_collapses_retries_into_one_row(self, event_log):
        """The event log gets noisy fast under retries, which is why this is the
        default view."""
        _write(
            event_log,
            _event(type="dispatched"),
            _event(type="dispatched"),
            _event(type="worker_done"),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert screen._view_mode == "aggregated"
            rows = _rows(app)
        assert len(rows) == 1
        # The run count is what tells the operator this was retried.
        assert "2" in rows[0]

    async def test_the_events_view_shows_every_row(self, event_log):
        _write(
            event_log,
            _event(type="dispatched"),
            _event(type="dispatched"),
            _event(type="worker_done"),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("a")
            await pilot.pause()
            assert screen._view_mode == "events"
            assert len(_rows(app)) == 3

    async def test_the_toggle_round_trips(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("a")
            await pilot.press("a")
            await pilot.pause()
        assert screen._view_mode == "aggregated"

    async def test_each_view_has_its_own_columns(self, event_log):
        """DataTable cannot hide columns, so the toggle rebuilds them; a stale set
        would shift every value one cell left."""
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            aggregated = [str(c.label) for c in table.columns.values()]
            await pilot.press("a")
            await pilot.pause()
            events = [str(c.label) for c in table.columns.values()]
        assert "runs" in aggregated
        assert "runs" not in events


class TestSourceFilter:
    async def test_cycling_narrows_to_one_source(self, event_log):
        _write(
            event_log,
            _event(source="cron", identifier="ACE-1"),
            _event(source="slack", identifier="C123"),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert len(_rows(app)) == 2
            seen = {screen._filter}
            # Cycle until a filter that excludes something.
            for _ in range(len(history_mod._SOURCE_CYCLE)):
                await pilot.press("s")
                await pilot.pause()
                seen.add(screen._filter)
            assert len(seen) > 1

    async def test_the_header_names_the_current_filter_and_view(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            header = str(app.screen.query_one("#history-header", Static).content)
        assert "filter=all" in header
        assert "view=aggregated" in header


class TestRefreshOnlyOnChange:
    async def test_an_unchanged_log_is_not_re_read(self, event_log):
        """The screen polls every 2s, and re-tailing a quiet log is pure waste."""
        _write(event_log, _event())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            refreshes = {"n": 0}
            real = screen._refresh
            screen._refresh = lambda: (  # type: ignore[method-assign]
                refreshes.__setitem__("n", refreshes["n"] + 1),
                real(),
            )[1]
            screen._refresh_if_changed()
        assert refreshes["n"] == 0

    async def test_a_missing_log_is_not_an_error(self, event_log, tmp_path):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            event_log.path.unlink(missing_ok=True)
            screen._refresh_if_changed()
            screen._refresh()
            await pilot.pause()
        assert screen._mtime is None

    async def test_r_forces_a_reload(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            _write(event_log, _event(identifier="ACE-NEW"))
            await pilot.press("r")
            await pilot.pause()
            assert any("ACE-NEW" in " ".join(row) for row in _rows(app))
        assert screen._mtime is not None


class TestRowUrl:
    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("owner/repo#42", "https://github.com/owner/repo/pull/42"),
            ("org/some-repo#1", "https://github.com/org/some-repo/pull/1"),
        ],
    )
    def test_a_github_pr_shape_becomes_a_pull_url(self, identifier, expected):
        """Detected from the identifier alone, so it needs no configuration."""
        screen = HistoryScreen()
        assert screen._row_url(identifier) == expected

    @pytest.mark.parametrize(
        "identifier",
        [
            "ACE-1234",  # a ticket key: nothing here knows the instance
            "owner/repo",  # no number
            "owner/repo#notanumber",
            "a/b/c#1",  # too many slashes to be owner/repo
            "C123456",  # a Slack channel
            "",
        ],
    )
    def test_anything_else_has_no_url(self, identifier):
        screen = HistoryScreen()
        assert screen._row_url(identifier) is None


class TestOpenLink:
    async def test_a_pr_row_opens_in_the_browser(self, event_log, monkeypatch):
        _write(event_log, _event(source="cron", identifier="owner/repo#42"))
        opened: list[str] = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("o")
            await pilot.pause()
        assert opened == ["https://github.com/owner/repo/pull/42"]

    async def test_a_row_with_no_link_says_so(self, event_log):
        _write(event_log, _event(identifier="ACE-1"))
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("o")
            await pilot.pause()
        assert any("no link for ACE-1" in msg for msg, _sev in notices)

    async def test_no_row_selected_says_so(self, event_log, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            monkeypatch.setattr(screen, "_cursor_row_key", lambda: None)
            screen.action_open_link()
            await pilot.pause()
        assert any("no row selected" in msg for msg, _sev in notices)

    async def test_a_browser_that_will_not_open_is_reported(
        self, event_log, monkeypatch
    ):
        _write(event_log, _event(source="cron", identifier="owner/repo#42"))
        import webbrowser

        monkeypatch.setattr(
            webbrowser,
            "open",
            lambda _url: (_ for _ in ()).throw(RuntimeError("no display")),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("o")
            await pilot.pause()
        assert any("open failed" in msg for msg, _sev in notices)


def _capture_notices(app: _Host, screen: HistoryScreen) -> list[tuple[str, str]]:
    notices: list[tuple[str, str]] = []
    screen._notify = lambda msg, severity: notices.append((msg, severity))  # type: ignore[method-assign]
    return notices


class TestCursorRowKey:
    async def test_an_empty_table_has_no_row_key(self, event_log, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            table.clear()
            await pilot.pause()
            assert screen._cursor_row_key() is None

    async def test_a_coordinate_lookup_failure_reads_as_no_selection(
        self, event_log, monkeypatch
    ):
        _write(event_log, _event())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            monkeypatch.setattr(
                table,
                "coordinate_to_cell_key",
                lambda _c: (_ for _ in ()).throw(RuntimeError("no such cell")),
            )
            assert screen._cursor_row_key() is None


def _with_session(**fields) -> dict:
    """An event carrying what `_resolve_session` needs to find its JSONL."""
    defaults = {"session_uuid": "s-uuid-1", "workspace": "/tmp/ws"}
    defaults.update(fields)
    return _event(**defaults)


class TestResolveSession:
    def test_an_event_with_no_session_uuid_cannot_be_resolved(self):
        """Nothing has run a turn yet, so there is no JSONL to watch or resume."""
        screen = HistoryScreen()
        assert screen._resolve_session("ACE-1", "cron", _event()) is None

    def test_the_recorded_workspace_wins(self):
        """The dispatching side is the only thing that knows the layout it used, so a
        row from a different source still resolves."""
        screen = HistoryScreen()
        resolved = screen._resolve_session(
            "ACE-1", "cron", _with_session(workspace="/custom/ws")
        )
        assert resolved == (Path("/custom/ws"), "s-uuid-1")

    def test_rows_written_before_the_workspace_was_recorded_fall_back(self):
        from claude_on_the_fly.agent import DATA_DIR

        screen = HistoryScreen()
        resolved = screen._resolve_session(
            "C123", "slack", _event(session_uuid="s-uuid-1")
        )
        assert resolved == (DATA_DIR / "workspaces" / "C123", "s-uuid-1")


class TestCopyTakeover:
    async def test_an_empty_table_says_there_is_no_row(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            table.clear()
            notices = _capture_notices(app, screen)
            screen.action_copy_takeover()
            await pilot.pause()
        assert any("no row selected" in msg for msg, _sev in notices)

    async def test_a_row_with_no_session_says_so(self, event_log):
        _write(event_log, _event(identifier="ACE-1"))
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("no takeover for this row" in msg for msg, _sev in notices)

    async def test_a_resolvable_row_copies_a_cd_and_resume_command(
        self, event_log, monkeypatch
    ):
        """The command has to carry the row's own workspace and uuid, so a takeover
        copied from an old row resumes that exact session rather than whatever the
        current env points at."""
        _write(event_log, _with_session(identifier="ACE-1"))
        backend = type(
            "B", (), {"takeover_command": lambda _s, _w, _u: "claude --resume x"}
        )()
        monkeypatch.setattr(history_mod, "get_backend", lambda: backend)
        copied: list[str] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
            notices = _capture_notices(app, screen)
            await pilot.press("t")
            await pilot.pause()
        assert copied == ["cd /tmp/ws && claude --resume x"]
        assert any("copied takeover cmd for ACE-1" in msg for msg, _sev in notices)

    async def test_a_backend_with_no_session_yet_says_so(self, event_log, monkeypatch):
        _write(event_log, _with_session(identifier="ACE-1"))
        backend = type("B", (), {"takeover_command": lambda _s, _w, _u: None})()
        monkeypatch.setattr(history_mod, "get_backend", lambda: backend)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("agent hasn't run a turn" in msg for msg, _sev in notices)

    async def test_a_backend_that_raises_is_reported(self, event_log, monkeypatch):
        _write(event_log, _with_session(identifier="ACE-1"))

        def boom(_self, _w, _u):
            raise RuntimeError("store unreadable")

        backend = type("B", (), {"takeover_command": boom})()
        monkeypatch.setattr(history_mod, "get_backend", lambda: backend)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("takeover failed" in msg for msg, _sev in notices)

    async def test_a_clipboard_that_will_not_write_is_reported(
        self, event_log, monkeypatch
    ):
        """Over SSH without a clipboard bridge this always fails, and silence would
        look like a successful copy."""
        _write(event_log, _with_session(identifier="ACE-1"))
        backend = type("B", (), {"takeover_command": lambda _s, _w, _u: "claude"})()
        monkeypatch.setattr(history_mod, "get_backend", lambda: backend)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            app.copy_to_clipboard = lambda _t: (_ for _ in ()).throw(  # type: ignore[method-assign]
                RuntimeError("no clipboard")
            )
            notices = _capture_notices(app, screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("clipboard write failed" in msg for msg, _sev in notices)

    async def test_a_coordinate_lookup_failure_says_no_row(
        self, event_log, monkeypatch
    ):
        _write(event_log, _with_session())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            monkeypatch.setattr(
                table,
                "coordinate_to_cell_key",
                lambda _c: (_ for _ in ()).throw(RuntimeError("gone")),
            )
            notices = _capture_notices(app, screen)
            screen.action_copy_takeover()
            await pilot.pause()
        assert any("no row selected" in msg for msg, _sev in notices)

    async def test_a_non_string_row_key_says_no_row(self, event_log, monkeypatch):
        _write(event_log, _with_session())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)

            class FakeKey:
                row_key = type("K", (), {"value": 42})()

            monkeypatch.setattr(table, "coordinate_to_cell_key", lambda _c: FakeKey())
            notices = _capture_notices(app, screen)
            screen.action_copy_takeover()
            await pilot.pause()
        assert any("no row selected" in msg for msg, _sev in notices)


def _session_jsonl(tmp_path: Path, *events: dict) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


class TestWatchPane:
    async def test_it_stays_hidden_until_asked_for(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            assert app.screen.query_one("#hist-watch-wrap").display is False

    async def test_a_row_with_no_session_refuses_to_open_it(self, event_log):
        _write(event_log, _event(identifier="ACE-1"))
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture_notices(app, screen)
            await pilot.press("w")
            await pilot.pause()
        assert screen._watch_open is False
        assert any("no watchable session" in msg for msg, _sev in notices)

    async def test_a_resolvable_row_opens_and_renders_the_session(
        self, event_log, monkeypatch, tmp_path
    ):
        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hello from the agent"}]
                },
            },
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            pane = app.screen.query_one("#hist-watch-pane", RichLog)
            rendered = "\n".join(
                seg.text for line in pane.lines for seg in line._segments
            )
            header = str(app.screen.query_one("#hist-watch-header", Static).content)
        assert screen._watch_open is True
        assert "hello from the agent" in rendered
        assert "ACE-1" in header

    async def test_no_session_log_yet_shows_a_placeholder(self, event_log, monkeypatch):
        _write(event_log, _with_session(identifier="ACE-1"))
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: None)
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            pane = app.screen.query_one("#hist-watch-pane", RichLog)
            rendered = "\n".join(
                seg.text for line in pane.lines for seg in line._segments
            )
        assert "no session log yet" in rendered

    async def test_a_log_with_nothing_displayable_says_so(
        self, event_log, monkeypatch, tmp_path
    ):
        """A JSONL of only tool bookkeeping renders to nothing, and a blank pane looks
        like a hung agent."""
        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(tmp_path, {"type": "system", "subtype": "init"})
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            pane = app.screen.query_one("#hist-watch-pane", RichLog)
            rendered = "\n".join(
                seg.text for line in pane.lines for seg in line._segments
            )
        assert "no displayable events yet" in rendered

    async def test_malformed_and_blank_lines_are_skipped(
        self, event_log, monkeypatch, tmp_path
    ):
        _write(event_log, _with_session(identifier="ACE-1"))
        path = tmp_path / "session.jsonl"
        path.write_text(
            "\n"
            "not json\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "real content"}]},
                }
            )
            + "\n"
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: path)
        app = _Host()
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            pane = app.screen.query_one("#hist-watch-pane", RichLog)
            rendered = "\n".join(
                seg.text for line in pane.lines for seg in line._segments
            )
        assert "real content" in rendered

    async def test_pressing_w_again_closes_it(self, event_log, monkeypatch, tmp_path):
        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(
            tmp_path, {"type": "assistant", "message": {"content": []}}
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()
            assert app.screen.query_one("#hist-watch-wrap").display is False
        assert screen._watch_open is False
        assert screen._watch_target is None

    async def test_a_quiet_session_is_not_re_rendered(
        self, event_log, monkeypatch, tmp_path
    ):
        """It polls at 1Hz, and a full re-read of a 10MB JSONL every second for an
        idle agent is what the mtime check exists to avoid."""
        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x"}]},
            },
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            reads = {"n": 0}
            real_tail = history_mod.render.tail_lines
            monkeypatch.setattr(
                history_mod.render,
                "tail_lines",
                lambda p, n: (reads.__setitem__("n", reads["n"] + 1), real_tail(p, n))[
                    1
                ],
            )
            screen._refresh_watch_if_changed()
            screen._refresh_watch_if_changed()
        assert reads["n"] == 0

    async def test_an_appended_session_is_re_rendered(
        self, event_log, monkeypatch, tmp_path
    ):
        import os

        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "first"}]},
            },
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            with log.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "second"}]
                            },
                        }
                    )
                    + "\n"
                )
            os.utime(log, (9999, 9999))
            screen._refresh_watch_if_changed()
            await pilot.pause()
            pane = app.screen.query_one("#hist-watch-pane", RichLog)
            rendered = "\n".join(
                seg.text for line in pane.lines for seg in line._segments
            )
        assert "second" in rendered

    async def test_a_closed_pane_is_not_polled(self, event_log, monkeypatch):
        called: list[bool] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(
                screen,
                "_refresh_watch_pane",
                lambda *, force_reload: called.append(force_reload),
            )
            screen._refresh_watch_if_changed()
        assert called == []

    async def test_moving_the_cursor_retargets_the_pane(
        self, event_log, monkeypatch, tmp_path
    ):
        _write(
            event_log,
            _with_session(identifier="ACE-1", session_uuid="uuid-1"),
            _with_session(identifier="ACE-2", session_uuid="uuid-2"),
        )
        first = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "one"}]},
            },
        )
        second = tmp_path / "second.jsonl"
        second.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "two"}]},
                }
            )
            + "\n"
        )
        monkeypatch.setattr(
            history_mod,
            "resolve_session_log",
            lambda _w, uuid: first if uuid == "uuid-1" else second,
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            before = screen._watch_target
            table = app.screen.query_one("#history-table", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
        assert screen._watch_target != before

    async def test_a_row_without_a_session_leaves_the_pane_alone(
        self, event_log, monkeypatch, tmp_path
    ):
        """Otherwise moving onto an unresolvable row would blank a pane the operator
        was reading."""
        _write(
            event_log,
            _with_session(identifier="ACE-1"),
            _event(identifier="ACE-NO-SESSION"),
        )
        log = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "kept"}]},
            },
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#history-table", DataTable)
            # Land on the row that has a session, open the pane, then move away.
            for row in range(table.row_count):
                table.move_cursor(row=row)
                await pilot.pause()
                if screen._cursor_row_key() in screen._row_watch:
                    break
            await pilot.press("w")
            await pilot.pause()
            watched = screen._watch_target
            for row in range(table.row_count):
                table.move_cursor(row=row)
                await pilot.pause()
        assert screen._watch_target == watched

    async def test_a_session_log_that_vanishes_stops_quietly(
        self, event_log, monkeypatch, tmp_path
    ):
        _write(event_log, _with_session(identifier="ACE-1"))
        log = _session_jsonl(
            tmp_path,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x"}]},
            },
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: log)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            log.unlink()
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()

    async def test_no_target_means_nothing_to_render(self, event_log):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_target = None
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()

    async def test_a_target_that_is_no_longer_in_the_row_map_is_left_alone(
        self, event_log
    ):
        """A source filter can remove the watched row; the pane stays on its last good
        target so the reader keeps their place."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_target = "agg:cron:GONE"
            screen._row_watch = {}
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()


class TestUnreadableEventLog:
    async def test_a_stat_error_skips_the_tick(self, event_log, monkeypatch):
        """A permissions problem or a syncer mid-write is not a reason to redraw with
        nothing; the next tick will try again."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            refreshes = {"n": 0}
            monkeypatch.setattr(
                screen,
                "_refresh",
                lambda: refreshes.__setitem__("n", refreshes["n"] + 1),
            )
            monkeypatch.setattr(
                Path,
                "stat",
                lambda self, *a, **k: (_ for _ in ()).throw(
                    OSError("permission denied")
                ),
            )
            screen._refresh_if_changed()
        assert refreshes["n"] == 0

    async def test_a_stat_error_during_a_forced_refresh_keeps_the_old_mtime(
        self, event_log, monkeypatch
    ):
        """`pass`, not `return`: the refresh still has to draw the rows it can read."""
        _write(event_log, _event())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            before = screen._mtime
            real_stat = Path.stat
            failed = {"once": False}

            def stat_fails_once(self, *args, **kwargs):
                # Only the mtime read at the top of _refresh: the tail below stats
                # the same file again and has to keep working, which is the whole
                # point of `pass` rather than `return` there.
                if self.name == "events.jsonl" and not failed["once"]:
                    failed["once"] = True
                    raise OSError("permission denied")
                return real_stat(self, *args, **kwargs)

            monkeypatch.setattr(Path, "stat", stat_fails_once)
            screen._refresh()
            await pilot.pause()
        assert screen._mtime == before

    async def test_a_changed_log_triggers_a_refresh(self, event_log):
        import os

        _write(event_log, _event())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            _write(event_log, _event(identifier="ACE-LATER"))
            os.utime(event_log.path, (9999, 9999))
            screen._refresh_if_changed()
            await pilot.pause()
            assert any("ACE-LATER" in " ".join(row) for row in _rows(app))


class TestWatchPaneScrollHandling:
    async def test_a_reader_scrolled_up_keeps_their_place_on_an_update(
        self, event_log, monkeypatch, tmp_path
    ):
        """Yanking the viewport to the bottom on every 1Hz tick makes the pane
        unreadable while an agent is talking."""
        import os

        _write(event_log, _with_session(identifier="ACE-1"))
        path = tmp_path / "session.jsonl"
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": f"line {i}"}]},
                    }
                )
                + "\n"
                for i in range(40)
            )
        )
        monkeypatch.setattr(history_mod, "resolve_session_log", lambda _w, _u: path)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            # Pretend the reader scrolled up: not at the bottom any more.
            restores: list[int] = []
            monkeypatch.setattr(
                history_mod.render, "capture_scroll", lambda _p: (False, 7)
            )
            monkeypatch.setattr(
                history_mod.render,
                "restore_scroll",
                lambda _p, *, prev_y: restores.append(prev_y),
            )
            with path.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "new"}]},
                        }
                    )
                    + "\n"
                )
            os.utime(path, (9999, 9999))
            screen._refresh_watch_if_changed()
            await pilot.pause()
        assert restores == [7], "the reader's offset was not restored"


async def test_the_events_view_also_resolves_watchable_sessions(event_log):
    """Both renderers build the row->session map, and only the aggregated one being
    right would make `w` and `t` dead in the events view."""
    _write(event_log, _with_session(identifier="ACE-1"))
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        assert screen._view_mode == "events"
        assert screen._row_watch
        assert all(key.startswith("evt:") for key in screen._row_watch)
