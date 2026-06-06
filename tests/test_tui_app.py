"""Smoke tests for the Textual app — boot, navigate, exit without crashing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.coordinate import Coordinate

from claude_on_the_fly.tui.tui_app import (
    ClaudeTuiApp,
    _event_sig,
    _events_since,
)


def _ev(ts, type_, ident, uuid="u", **extra):
    return {"ts": ts, "type": type_, "identifier": ident, "session_uuid": uuid, **extra}


def _write_heartbeat(state_dir, name, running_jobs=()):
    """Write a heartbeat JSON that reads as a running daemon with the given
    in-flight jobs. Uses the live pid so state.snapshot() sees it alive."""
    import json
    import os
    from datetime import datetime, timezone

    state_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (state_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "last_heartbeat": now,
                "started_at": now,
                "extra": {"running_jobs": list(running_jobs)},
            }
        )
    )


class _FakeLog:
    """Stand-in for EventLog: tail(n) returns the last n of a mutable list."""

    def __init__(self, records):
        self._records = records

    def tail(self, n):
        return self._records[-n:]


class TestEventsSince:
    def test_none_baseline_yields_nothing(self):
        # Priming the baseline: nothing is "new" until we've recorded a mark.
        recs = [_ev("t1", "dispatched", "A"), _ev("t2", "worker_done", "B")]
        assert _events_since(recs, None) == []

    def test_returns_rows_after_last_seen(self):
        a = _ev("t1", "dispatched", "A", "u1")
        b = _ev("t2", "worker_done", "B", "u2")
        c = _ev("t3", "worker_failed", "C", "u3")
        assert _events_since([a, b, c], _event_sig(a)) == [b, c]

    def test_aged_out_baseline_treats_all_as_new(self):
        # last_sig no longer in the window → over-notify rather than drop.
        b = _ev("t2", "worker_done", "B", "u2")
        c = _ev("t3", "worker_done", "C", "u3")
        gone = _event_sig(_ev("t0", "dispatched", "Z", "u0"))
        assert _events_since([b, c], gone) == [b, c]

    def test_same_ts_disambiguated_by_uuid(self):
        a = _ev("t1", "worker_done", "A", "u1")
        b = _ev("t1", "worker_done", "B", "u2")  # same ts, different job
        assert _events_since([a, b], _event_sig(a)) == [b]


class TestWorkerEventNotify:
    def _bare_app(self):
        app = ClaudeTuiApp()
        # Stub the Textual methods so the handler can be exercised without a
        # running app/driver.
        app.notify = MagicMock()  # ty: ignore[invalid-assignment]
        app.bell = MagicMock()  # ty: ignore[invalid-assignment]
        return app

    def test_done_notifies_info_with_bell(self):
        app = self._bare_app()
        app._notify_worker_event(
            _ev("t", "worker_done", "owner/repo#9", reason="inactive")
        )
        app.notify.assert_called_once()
        assert "owner/repo#9" in app.notify.call_args.args[0]
        assert app.notify.call_args.kwargs["severity"] == "information"
        app.bell.assert_called_once()

    def test_failed_notifies_error_with_bell(self):
        app = self._bare_app()
        app._notify_worker_event(_ev("t", "worker_failed", "ACES-3"))
        assert app.notify.call_args.kwargs["severity"] == "error"
        assert "ACES-3" in app.notify.call_args.args[0]
        app.bell.assert_called_once()

    def test_poll_primes_then_notifies_only_new_worker_events(self):
        app = self._bare_app()
        seed = _ev("t1", "dispatched", "X", "u1")
        log = _FakeLog([seed])
        app._event_log = log
        app._last_event_sig = app._latest_event_sig()  # baseline = seed

        app._poll_worker_events()  # nothing newer than the baseline
        app.notify.assert_not_called()

        log._records.append(_ev("t2", "dispatched", "Y", "u2"))  # not a worker event
        log._records.append(_ev("t3", "worker_done", "owner/repo#1", "u3"))
        app._poll_worker_events()

        app.notify.assert_called_once()
        assert "owner/repo#1" in app.notify.call_args.args[0]
        app.bell.assert_called_once()


def test_command_palette_disabled():
    # Fixed-purpose supervisor: no ctrl+p palette.
    assert ClaudeTuiApp.ENABLE_COMMAND_PALETTE is False


def test_app_title_is_claude_on_the_fly():
    assert ClaudeTuiApp.TITLE == "Claude On The Fly"


@pytest.mark.asyncio
async def test_app_boots_to_dashboard():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"


def _dashboard(app):
    """Narrow app.screen to DashboardScreen — for the type checker and as a guard."""
    from claude_on_the_fly.tui.screens.dashboard import DashboardScreen

    screen = app.screen
    assert isinstance(screen, DashboardScreen)
    return screen


class TestDashboardLayout:
    """The tabbed dashboard: daemon tabs + chat-strip override + retargeting."""

    @pytest.mark.asyncio
    async def test_hero_panels_render_with_headers(self):
        from textual.widgets import Static

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            sym = str(app.screen.query_one("#symphony-header", Static).render())
            sched = str(app.screen.query_one("#scheduler-header", Static).render())
            # Panels render even with nothing running (daemons stopped in tests).
            assert "SYMPHONY" in sym
            assert "stopped" in sym
            assert "SCHEDULER" in sched

    @pytest.mark.asyncio
    async def test_chat_tab_shows_daemons_in_header_and_idle_table(
        self, tmp_path, monkeypatch
    ):
        """The chat tab no longer rosters daemons as table rows. With nothing
        running, the header carries the daemon health and the table is the live
        activity monitor — idle, so a single placeholder row.

        Isolate the heartbeat state dir the dashboard reads from real disk."""
        from textual.widgets import DataTable, Static

        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            header = str(app.screen.query_one("#chat-strip-header", Static).render())
            # Cold start (no heartbeats): all chat daemons surface in the header.
            for name in ("telegram", "slack", "gmail"):
                assert name in header
            strip = app.screen.query_one("#chat-strip", DataTable)
            # Table is the live monitor, not a roster. Cold start → the selected
            # frontend is stopped, so the empty row hints how to start it.
            assert strip.row_count == 1
            cell = strip.get_cell_at(Coordinate(0, 0))
            assert "press r to start" in str(getattr(cell, "plain", cell))

    @pytest.mark.asyncio
    async def test_stopped_frontend_stays_selectable_when_another_runs(
        self, tmp_path, monkeypatch
    ):
        """Starting one frontend must not hide the others: ←/→ can still land on
        a stopped frontend (so k/r can start it), even while telegram runs."""
        from textual.widgets import DataTable

        state_dir = tmp_path / "state"
        _write_heartbeat(
            state_dir,
            "telegram",
            running_jobs=[
                {"identifier": "telegram/H", "uptime_s": 4, "session_uuid": "u1"}
            ],
        )
        # slack + gmail never started (no heartbeat) — must still be reachable.
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", state_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            strip = app.screen.query_one("#chat-strip", DataTable)

            # telegram is selected and running.
            assert screen._active_daemon() == "telegram"

            # → reaches slack even though it never ran; the table shows the
            # start hint and k/r now target slack.
            await pilot.press("right")
            await pilot.pause()
            assert screen._active_daemon() == "slack"
            assert "press r to start" in str(strip.get_cell_at(Coordinate(0, 0)))

            # → again reaches gmail.
            await pilot.press("right")
            await pilot.pause()
            assert screen._active_daemon() == "gmail"

    @pytest.mark.asyncio
    async def test_chat_tab_lists_running_jobs_from_heartbeat(
        self, tmp_path, monkeypatch
    ):
        """The selected frontend's currently-running jobs (from the heartbeat)
        render one row each with their uptime — no done/failed history."""
        from textual.widgets import DataTable

        state_dir = tmp_path / "state"
        _write_heartbeat(
            state_dir,
            "telegram",
            running_jobs=[
                {"identifier": "telegram/H", "uptime_s": 14, "session_uuid": "u1"},
                {"identifier": "telegram/Bob", "uptime_s": 5, "session_uuid": "u2"},
            ],
        )
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", state_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            strip = app.screen.query_one("#chat-strip", DataTable)
            # Only telegram has a heartbeat → single relevant frontend, selected.
            assert strip.row_count == 2
            idents = [str(strip.get_cell_at(Coordinate(r, 0))) for r in range(2)]
            assert idents == ["telegram/H", "telegram/Bob"]
            uptimes = [str(strip.get_cell_at(Coordinate(r, 1))) for r in range(2)]
            assert uptimes == ["14s", "5s"]

    @pytest.mark.asyncio
    async def test_arrow_keys_switch_chat_frontend_and_scope_table(
        self, tmp_path, monkeypatch
    ):
        """←/→ move the selected chat frontend: the table scopes to that
        frontend's running jobs and k/r retarget to it."""
        from textual.widgets import DataTable

        state_dir = tmp_path / "state"
        _write_heartbeat(
            state_dir,
            "telegram",
            running_jobs=[
                {"identifier": "telegram/H", "uptime_s": 9, "session_uuid": "u1"}
            ],
        )
        _write_heartbeat(
            state_dir,
            "slack",
            running_jobs=[
                {"identifier": "slack/ops", "uptime_s": 3, "session_uuid": "u2"}
            ],
        )
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", state_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            strip = app.screen.query_one("#chat-strip", DataTable)

            # telegram + slack are both relevant; selection starts on telegram.
            assert screen._active_daemon() == "telegram"
            assert [
                str(strip.get_cell_at(Coordinate(r, 0))) for r in range(strip.row_count)
            ] == ["telegram/H"]

            # Right → next frontend (slack): table scopes to slack, target flips.
            await pilot.press("right")
            await pilot.pause()
            assert screen._active_daemon() == "slack"
            assert [
                str(strip.get_cell_at(Coordinate(r, 0))) for r in range(strip.row_count)
            ] == ["slack/ops"]

            # Left wraps back to telegram.
            await pilot.press("left")
            await pilot.pause()
            assert screen._active_daemon() == "telegram"

    @pytest.mark.asyncio
    async def test_tab_keys_switch_active_daemon(self, tmp_path, monkeypatch):
        """[1]/[2]/[3] switch tabs, which is how the supervisor keys + log row
        pick a daemon now (tab, not focus). Switching also lands focus on the
        new tab's table. Tab order: chat / scheduler / symphony."""
        from textual.widgets import TabbedContent

        # Isolate disk: cold start → chat target falls back to the first
        # frontend (telegram) regardless of the dev machine's real state.
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            tabs = screen.query_one("#daemon-tabs", TabbedContent)
            # Opens on chat; cold start → target is the first chat daemon.
            assert tabs.active == "tab-chat"
            assert app.focused is not None and app.focused.id == "chat-strip"
            assert screen._active_daemon() == "telegram"

            await pilot.press("2")
            await pilot.pause()
            assert tabs.active == "tab-scheduler"
            assert screen._active_daemon() == "schedule"
            assert getattr(app.focused, "id", None) == "jobs-content"

            await pilot.press("3")
            await pilot.pause()
            assert tabs.active == "tab-symphony"
            assert screen._active_daemon() == "symphony"
            assert getattr(app.focused, "id", None) == "symphony-tickets"

            await pilot.press("1")
            await pilot.pause()
            assert tabs.active == "tab-chat"
            assert screen._active_daemon() == "telegram"
            assert getattr(app.focused, "id", None) == "chat-strip"

    def test_footer_keeps_core_keys_visible_hides_rest_in_modal(self):
        from claude_on_the_fly.tui.screens.dashboard import DashboardScreen

        visible = {b.key for b in DashboardScreen.BINDINGS if b.show}
        hidden = {b.key for b in DashboardScreen.BINDINGS if not b.show}
        # Slim footer: lifecycle (k/r), help, quit.
        assert visible == {"k", "r", "question_mark", "q"}
        # Everything else stays bound but lives in the `?` help modal.
        assert hidden == {
            "l",
            "h",
            "g",
            "u",
            "d",
            "c",
            "R",
            "K",
            "1",
            "2",
            "3",
            "left",
            "right",
        }

    @pytest.mark.asyncio
    async def test_help_modal_lists_keys_hidden_from_footer(self):
        from textual.widgets import Static

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "HelpScreen"
            keymap = str(app.screen.query_one("#help-keys", Static).render())
            # The keys pulled off the footer must be discoverable here.
            for label in ("Logs", "History", "Config", "Stop all", "Refresh"):
                assert label in keymap
            # esc closes it.
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "DashboardScreen"

    @pytest.mark.asyncio
    async def test_g_opens_config_picker_dialog(self):
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "ConfigPickerScreen"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "DashboardScreen"

    @pytest.mark.asyncio
    async def test_config_picker_choice_routes(self, monkeypatch):
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            calls: list[object] = []
            monkeypatch.setattr(
                screen, "_edit_schedule_config", lambda: calls.append("schedule")
            )
            monkeypatch.setattr(screen, "_edit_env", lambda: calls.append("env"))
            monkeypatch.setattr(
                app, "push_screen", lambda *a, **k: calls.append(("screen", a[0]))
            )
            # The dialog dismisses with one of these ids (or None on cancel).
            screen._open_config_target("symphony")
            screen._open_config_target("schedule")
            screen._open_config_target("env")
            screen._open_config_target(None)  # cancel → no-op
            assert calls == [("screen", "config"), "schedule", "env"]

    @pytest.mark.asyncio
    async def test_empty_symphony_table_shows_placeholder_row(self):
        from textual.widgets import DataTable

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # No symphony daemon in tests → no tickets → one placeholder row,
            # not a bare header (and the table stays focusable for Tab).
            table = app.screen.query_one("#symphony-tickets", DataTable)
            assert table.row_count == 1
            assert "no active jobs" in str(table.get_row_at(0)[0])

    @pytest.mark.asyncio
    async def test_action_cue_follows_active_tab(self):
        from textual.widgets import Static

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            cue = app.screen.query_one("#action-cue", Static)
            # Opens on chat → first chat daemon (telegram) is the target.
            assert "telegram" in str(cue.render())

            await pilot.press("3")
            await pilot.pause()
            assert "symphony" in str(cue.render())

    @pytest.mark.asyncio
    async def test_tab_labels_reflect_daemon_health(self, tmp_path, monkeypatch):
        """Each tab title carries its daemon's health glyph, so the tab bar
        shows every zone's liveness regardless of which tab is active. Isolate
        the state dir so the dev machine's real daemons don't bleed in — empty
        → all three read stopped (○)."""
        from textual.widgets import TabbedContent

        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            tabs = app.screen.query_one("#daemon-tabs", TabbedContent)
            assert str(tabs.get_tab("tab-chat").label) == "[1] ○ chat"
            assert str(tabs.get_tab("tab-scheduler").label) == "[2] ○ scheduler"
            assert str(tabs.get_tab("tab-symphony").label) == "[3] ○ symphony"

    @pytest.mark.asyncio
    async def test_supervisor_action_targets_active_tab_daemon(self, monkeypatch):
        from claude_on_the_fly.tui import supervisor

        captured: list[str] = []

        def fake_restart(name):
            captured.append(name)
            return 4242

        monkeypatch.setattr(supervisor, "restart", fake_restart)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            # Switch to the scheduler tab, then trigger restart.
            await pilot.press("2")
            await pilot.pause()
            await screen._run_supervisor_action("restart", supervisor.restart)
            assert captured == ["schedule"]

    @pytest.mark.asyncio
    async def test_action_with_no_active_daemon_warns_instead_of_acting(
        self, monkeypatch
    ):
        from claude_on_the_fly.tui import supervisor

        called: list[str] = []
        monkeypatch.setattr(
            supervisor, "restart", lambda name: called.append(name) or 1
        )

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            # No recognized zone focused → the guard must warn, not act.
            monkeypatch.setattr(screen, "_active_daemon", lambda: None)
            await screen._run_supervisor_action("restart", supervisor.restart)
            assert called == []


@pytest.mark.asyncio
async def test_press_d_pushes_doctor():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DoctorScreen"


@pytest.mark.asyncio
async def test_press_l_pushes_logs():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "LogsScreen"


@pytest.mark.asyncio
async def test_escape_returns_from_doctor():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"


@pytest.mark.asyncio
async def test_g_picks_symphony_then_shows_config_preview(tmp_path, monkeypatch):
    """`g` → picker → symphony.yaml shows the resolved config; Esc returns."""
    import claude_on_the_fly.tui.screens.config_preview as cp

    # Point the preview at a throwaway config so it renders real content
    # without touching the user's real ~/.claude-on-the-fly/symphony.yaml.
    cfg = tmp_path / "symphony.yaml"
    cfg.write_text(
        "tracker:\n  base_url: https://x.atlassian.net\n  project_key: PROJ\n"
    )
    monkeypatch.setattr(cp, "CONFIG_PATH", cfg)

    from textual.widgets import Static

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfigPickerScreen"
        # symphony.yaml is the first/highlighted option — Enter selects it.
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfigPreviewScreen"
        body = app.screen.query_one("#config-preview", Static)
        assert "project_key: PROJ" in str(body.render())
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"


class TestLogScrollPreservation:
    """A live log rewrite must not yank a reader who scrolled up, but should
    still follow the tail when they're parked at the bottom."""

    @pytest.mark.asyncio
    async def test_preserves_scroll_up_then_sticks_at_bottom(self):
        from textual.app import App, ComposeResult
        from textual.widgets import RichLog

        from claude_on_the_fly.tui import render

        class _App(App):
            def compose(self) -> ComposeResult:
                yield RichLog(id="log", auto_scroll=False, max_lines=10000)

        app = _App()
        async with app.run_test(size=(80, 10)) as pilot:
            log = app.query_one("#log", RichLog)
            for i in range(100):
                log.write(f"line-{i:03d}")
            await pilot.pause()

            # Reader scrolls up; a live rewrite must keep their offset.
            log.scroll_to(y=30, animate=False)
            await pilot.pause()
            was_bottom, prev_y = render.capture_scroll(log)
            render.begin_scroll_aware_rewrite(log, stick_to_bottom=was_bottom)
            for i in range(100):
                log.write(f"line-{i:03d}")
            if not was_bottom:
                render.restore_scroll(log, prev_y=prev_y)
            await pilot.pause()
            assert log.scroll_y == 30

            # Parked at the bottom: a rewrite with MORE content follows to the
            # true new end, not the stale old one.
            log.scroll_end(animate=False)
            await pilot.pause()
            was_bottom, prev_y = render.capture_scroll(log)
            assert was_bottom is True
            render.begin_scroll_aware_rewrite(log, stick_to_bottom=was_bottom)
            for i in range(140):
                log.write(f"line-{i:03d}")
            if not was_bottom:
                render.restore_scroll(log, prev_y=prev_y)
            await pilot.pause()
            assert log.scroll_y == log.max_scroll_y
