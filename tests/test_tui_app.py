"""Smoke tests for the Textual app — boot, navigate, exit without crashing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claude_on_the_fly.tui.tui_app import (
    ClaudeTuiApp,
    _event_sig,
    _events_since,
)


def _ev(ts, type_, ident, uuid="u", **extra):
    return {"ts": ts, "type": type_, "identifier": ident, "session_uuid": uuid, **extra}


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
    """The redesigned dashboard: hero panels + Tab-cycled focus + retargeting."""

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
    async def test_chat_strip_lists_the_three_chat_daemons(self):
        from textual.widgets import DataTable

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            strip = app.screen.query_one("#chat-strip", DataTable)
            assert strip.row_count == 3  # telegram / slack / gmail

    @pytest.mark.asyncio
    async def test_boot_focuses_symphony_then_tab_cycles_zones(self):
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            assert app.focused is not None and app.focused.id == "symphony-tickets"
            assert screen._active_daemon() == "symphony"

            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "jobs-content"
            assert screen._active_daemon() == "schedule"

            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "chat-strip"
            # Resolves to the highlighted chat daemon row (first = telegram).
            assert screen._active_daemon() == "telegram"

    def test_footer_shows_every_key_ordered_by_usability(self):
        from claude_on_the_fly.tui.screens.dashboard import DashboardScreen

        keys = [
            (b[0] if isinstance(b, tuple) else b.key) for b in DashboardScreen.BINDINGS
        ]
        # Nothing is hidden — every action is reachable from the footer.
        # `e` is gone: config editing folded into the contextual `g`.
        assert set(keys) == {
            "s",
            "k",
            "r",
            "u",
            "l",
            "h",
            "g",
            "d",
            "c",
            "R",
            "K",
            "q",
        }
        # Daemon lifecycle leads; Quit trails.
        assert keys[:4] == ["s", "k", "r", "u"]
        assert keys[-1] == "q"
        # The `?` help panel and the standalone `e` Edit-.env were removed.
        assert "question_mark" not in keys
        assert "e" not in keys

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
    async def test_action_cue_follows_focus(self):
        from textual.widgets import Static

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            cue = app.screen.query_one("#action-cue", Static)
            assert "symphony" in str(cue.render())

            await pilot.press("tab")
            await pilot.pause()
            assert "schedule" in str(cue.render())

    @pytest.mark.asyncio
    async def test_supervisor_action_targets_focused_daemon(self, monkeypatch):
        from textual.widgets import DataTable

        from claude_on_the_fly.tui import supervisor

        captured: list[str] = []

        def fake_spawn(name):
            captured.append(name)
            return 4242

        monkeypatch.setattr(supervisor, "spawn", fake_spawn)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            # Move focus to the scheduler panel, then trigger start.
            screen.query_one("#jobs-content", DataTable).focus()
            await pilot.pause()
            await screen._run_supervisor_action("start", supervisor.spawn)
            assert captured == ["schedule"]

    @pytest.mark.asyncio
    async def test_start_with_no_active_daemon_warns_instead_of_acting(
        self, monkeypatch
    ):
        from claude_on_the_fly.tui import supervisor

        called: list[str] = []
        monkeypatch.setattr(supervisor, "spawn", lambda name: called.append(name) or 1)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            # No recognized zone focused → the guard must warn, not spawn.
            monkeypatch.setattr(screen, "_active_daemon", lambda: None)
            await screen._run_supervisor_action("start", supervisor.spawn)
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
