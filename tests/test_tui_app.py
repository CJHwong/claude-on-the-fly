"""Smoke tests for the Textual app — boot, navigate, exit without crashing."""

from __future__ import annotations

from datetime import UTC
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
    from datetime import datetime

    state_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    async def test_hero_panels_render_with_headers(self, tmp_path, monkeypatch):
        from textual.widgets import Static

        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            sched = str(app.screen.query_one("#cron-header", Static).render())
            assert "CRON" in sched
            assert "stopped" in sched

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
            for name in ("slack", "telegram"):
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
        a stopped frontend (so k/r can start it), even while slack runs."""
        from textual.widgets import DataTable

        state_dir = tmp_path / "state"
        _write_heartbeat(
            state_dir,
            "slack",
            running_jobs=[
                {"identifier": "slack/H", "uptime_s": 4, "session_uuid": "u1"}
            ],
        )
        # telegram never started (no heartbeat) — must still be reachable.
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", state_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            strip = app.screen.query_one("#chat-strip", DataTable)

            # slack is first in the roster, so it is selected, and it is running.
            assert screen._active_daemon() == "slack"

            # → reaches telegram even though it never ran; the table shows the
            # start hint and k/r now target telegram.
            await pilot.press("right")
            await pilot.pause()
            assert screen._active_daemon() == "telegram"
            assert "press r to start" in str(strip.get_cell_at(Coordinate(0, 0)))

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
            "slack",
            running_jobs=[
                {"identifier": "slack/H", "uptime_s": 14, "session_uuid": "u1"},
                {"identifier": "slack/Bob", "uptime_s": 5, "session_uuid": "u2"},
            ],
        )
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", state_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            strip = app.screen.query_one("#chat-strip", DataTable)
            # Only slack has a heartbeat → single relevant frontend, selected.
            assert strip.row_count == 2
            idents = [str(strip.get_cell_at(Coordinate(r, 0))) for r in range(2)]
            assert idents == ["slack/H", "slack/Bob"]
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

            # slack + telegram are both relevant; selection starts on slack.
            assert screen._active_daemon() == "slack"
            assert [
                str(strip.get_cell_at(Coordinate(r, 0))) for r in range(strip.row_count)
            ] == ["slack/ops"]

            # Right → next frontend (telegram): table scopes to it, target flips.
            await pilot.press("right")
            await pilot.pause()
            assert screen._active_daemon() == "telegram"
            assert [
                str(strip.get_cell_at(Coordinate(r, 0))) for r in range(strip.row_count)
            ] == ["telegram/H"]

            # Left wraps back to slack.
            await pilot.press("left")
            await pilot.pause()
            assert screen._active_daemon() == "slack"

    @pytest.mark.asyncio
    async def test_tab_keys_switch_active_daemon(self, tmp_path, monkeypatch):
        """[1]-[4] switch tabs, which is how the supervisor keys + log row
        pick a daemon now (tab, not focus). Switching also lands focus on the
        new tab's table. Tab order: chat / scheduler / jobs."""
        from textual.widgets import DataTable, Static, TabbedContent

        # Isolate disk: cold start → chat target falls back to the first
        # frontend (slack) regardless of the dev machine's real state.
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _dashboard(app)
            tabs = screen.query_one("#daemon-tabs", TabbedContent)
            # Opens on chat; cold start → target is the first chat daemon.
            assert tabs.active == "tab-chat"
            assert app.focused is not None and app.focused.id == "chat-strip"
            assert screen._active_daemon() == "slack"

            await pilot.press("2")
            await pilot.pause()
            assert tabs.active == "tab-cron"
            assert screen._active_daemon() == "cron"
            assert getattr(app.focused, "id", None) == "cron-entries"

            await pilot.press("3")
            await pilot.pause()
            assert tabs.active == "tab-jobs"
            assert screen._active_daemon() == "jobs"
            assert getattr(app.focused, "id", None) == "jobs-queue"
            # The widget ids the DEFAULT_CSS rules name must actually exist:
            # Textual ignores a selector that matches nothing, so a typo here
            # would style nothing and break no test.
            screen.query_one("#jobs-panel")
            screen.query_one("#jobs-queue-header", Static)
            screen.query_one("#jobs-queue", DataTable)
            # The scheduler's own cron table is a DIFFERENT widget, untouched.
            assert screen.query_one("#cron-entries", DataTable) is not screen.query_one(
                "#jobs-queue", DataTable
            )

            await pilot.press("1")
            await pilot.pause()
            assert tabs.active == "tab-chat"
            assert screen._active_daemon() == "slack"
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
            "n",
            "t",
            "s",
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
                screen, "_edit_cron_config", lambda: calls.append("cron")
            )
            monkeypatch.setattr(screen, "_edit_env", lambda: calls.append("env"))
            monkeypatch.setattr(
                screen, "_edit_sandbox_config", lambda: calls.append("sandbox")
            )
            monkeypatch.setattr(
                app, "push_screen", lambda *a, **k: calls.append(("screen", a[0]))
            )
            # The dialog dismisses with one of these ids (or None on cancel).
            screen._open_config_target("env")
            screen._open_config_target("sandbox")
            screen._open_config_target("cron")
            screen._open_config_target(None)  # cancel → no-op
            assert calls == ["env", "sandbox", "cron"]

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_action_cue_follows_active_tab(self):
        from textual.widgets import Static

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            cue = app.screen.query_one("#action-cue", Static)
            # Opens on chat → first chat daemon (slack) is the target.
            assert "slack" in str(cue.render())

            await pilot.press("3")
            await pilot.pause()
            assert "jobs" in str(cue.render())

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
            assert str(tabs.get_tab("tab-cron").label) == "[2] ○ cron"
            assert str(tabs.get_tab("tab-jobs").label) == "[3] ○ jobs"

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
            # Switch to the cron tab, then trigger restart.
            await pilot.press("2")
            await pilot.pause()
            await screen._run_supervisor_action("restart", supervisor.restart)
            assert captured == ["cron"]

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
class TestJobsTab:
    """The [4] jobs tab: a read-only observer of the worker's maildir."""

    @staticmethod
    def _drop(root, stage, job_id, prompt="do it"):
        import json

        (root / stage).mkdir(parents=True, exist_ok=True)
        (root / stage / f"{job_id}.json").write_text(
            json.dumps({"id": job_id, "prompt": prompt, "origin": {}}),
            encoding="utf-8",
        )

    @staticmethod
    def _isolate(tmp_path, monkeypatch):
        """STATE_DIR and DEFAULT_JOBS_DIR, both under this test's tmp_path.

        Patching DEFAULT_JOBS_DIR here duplicates conftest's autouse
        `isolate_jobs_dir` on purpose. These are the only tests that *write* a
        maildir, so leaning on the autouse fixture alone made them the ones that
        populate the developer's live queue the moment that fixture is weakened
        — which is exactly what happened. A test that creates files owns the
        directory it creates them in. Hands back the isolated root for a test
        that wants to populate it."""
        monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")
        root = tmp_path / "jobs"
        monkeypatch.setattr("claude_on_the_fly.tui.state.DEFAULT_JOBS_DIR", root)
        return root

    @pytest.mark.asyncio
    async def test_queue_renders_with_the_worker_stopped(self, tmp_path, monkeypatch):
        """The point of reading the directory instead of the heartbeat: with no
        worker running at all, the backlog is still on screen."""
        from textual.widgets import DataTable, Static

        root = self._isolate(tmp_path, monkeypatch)
        self._drop(root, "cur", "100-aaaaaaaa", prompt="the running one")
        # A prompt is a Slack user's own words — the only third-party text on
        # this dashboard. `[/]` is a closing markup tag with nothing to close:
        # rendered as a bare str it raises MarkupError, which Textual escalates
        # to an app exit. run_test() re-raises, so this fixture fails loudly if
        # the cell ever stops being wrapped in Text.
        self._drop(root, "new", "200-bbbbbbbb", prompt="fix [/] and\nthe [b] route")
        (root / "done").mkdir(parents=True, exist_ok=True)
        (root / "done" / "010-z.json").write_text("{}", encoding="utf-8")
        (root / "done" / "010-z.result.json").write_text("{}", encoding="utf-8")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            header = str(app.screen.query_one("#jobs-queue-header", Static).render())
            assert "JOBS" in header
            assert "stopped" in header  # no heartbeat in the isolated state dir
            assert "new 1" in header
            assert "running 1" in header
            assert "done 1" in header  # counted once, not twice

            table = app.screen.query_one("#jobs-queue", DataTable)
            assert table.row_count == 2
            first = table.get_row_at(0)
            assert str(first[0]) == "aaaaaaaa"  # short id; row key holds the full one
            assert str(first[1]) == "running"
            assert str(first[2]) == "the running one"
            second = table.get_row_at(1)
            assert str(second[1]) == "queued"
            # A newline would break the cell; the preview collapses whitespace.
            # The brackets must survive verbatim — markup would swallow them.
            # (This assertion reads the stored value, so on its own it can't
            # prove markup-safety; reaching it at all is the real check —
            # add_row measures each cell, so an unwrapped str raises there.)
            assert str(second[2]) == "fix [/] and the [b] route"

    @pytest.mark.asyncio
    async def test_capped_queue_shows_what_it_left_out(self, tmp_path, monkeypatch):
        """The 20-row cap is a display limit. With a deeper queue the table has
        to say what it cut — otherwise the operator reads "new 25" in the header
        against 20 rows and has to do the subtraction."""
        from textual.widgets import DataTable

        from claude_on_the_fly.jobs.file_queue import DEFAULT_ROW_LIMIT

        root = self._isolate(tmp_path, monkeypatch)
        for i in range(DEFAULT_ROW_LIMIT + 5):
            self._drop(root, "new", f"{1000 + i}-xxxxxxxx")

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = _dashboard(app)
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert table.row_count == DEFAULT_ROW_LIMIT + 1
            assert str(table.get_row_at(DEFAULT_ROW_LIMIT)[0]) == "… 5 more"

            # Parkable like any other row: the 1Hz tick clears and rebuilds the
            # table, so a key the cursor restore cannot find leaves an operator
            # sitting on the last row yanked back to the top a second later.
            table.move_cursor(row=DEFAULT_ROW_LIMIT)
            await pilot.pause()
            assert screen._datatable_cursor_key("#jobs-queue") == "__more__"
            screen._refresh()
            await pilot.pause()
            assert screen._datatable_cursor_key("#jobs-queue") == "__more__"

    @pytest.mark.asyncio
    async def test_empty_queue_shows_placeholder_row(self, tmp_path, monkeypatch):
        from textual.widgets import DataTable

        self._isolate(tmp_path, monkeypatch)
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert table.row_count == 1
            assert "queue empty" in str(table.get_row_at(0)[0])

    @pytest.mark.asyncio
    async def test_nothing_the_table_composes_gets_clipped(self, tmp_path, monkeypatch):
        """The strings the table composes itself — the column headers, the two
        placeholder sentinels, the "… N more" truncation row, and a data row's
        state cell — have to fit the width the widget declares: a fixed-width
        column clips rather than wraps, so an over-long placeholder reads on
        screen as a bug in the queue. A data row's prompt and age are left
        unmeasured on purpose: both have always been allowed to overflow, so
        measuring them would encode a rule that is not the contract."""
        from rich.cells import cell_len
        from textual.widgets import DataTable

        from claude_on_the_fly.jobs.file_queue import DEFAULT_ROW_LIMIT

        def fits(table, row_index, phase):
            # Column.width, not get_render_width(): the latter adds the two
            # cells of padding, which would leave "queue empty" (11) passing
            # against the 10-wide column that clips it.
            for column, cell in zip(
                table.ordered_columns, table.get_row_at(row_index), strict=True
            ):
                text = str(cell)
                assert cell_len(text) <= column.width, (
                    f"{phase}: {column.label} cell {text!r} is {cell_len(text)} "
                    f"cells, column is {column.width}"
                )

        root = self._isolate(tmp_path, monkeypatch)
        app = ClaudeTuiApp()
        # Pinned, because the budget below is a claim about an 80-column
        # terminal — run_test()'s own default is not this test's to assume.
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = _dashboard(app)
            table = app.screen.query_one("#jobs-queue", DataTable)

            # A header too wide for its own column clips the same way.
            for column in table.ordered_columns:
                assert cell_len(str(column.label)) <= column.width, column.label

            # Widening one column at another's expense is the edit this test
            # exists to catch, and every cell can fit while the table itself no
            # longer does — so budget the row against the space it is given.
            assert (
                sum(column.get_render_width(table) for column in table.ordered_columns)
                <= table.container_size.width
            )

            assert table.row_count == 1
            fits(table, 0, "empty queue")

            self._drop(root, "cur", "0999-aaaaaaaa")
            for i in range(DEFAULT_ROW_LIMIT + 5):
                self._drop(root, "new", f"{1000 + i}-xxxxxxxx")
            screen._refresh()
            await pilot.pause()
            assert table.row_count == DEFAULT_ROW_LIMIT + 1
            # Pin the subject by key, not by copy: this has to measure the
            # truncation row, never a data row that shifted into its place.
            assert table.ordered_rows[DEFAULT_ROW_LIMIT].key.value == "__more__"
            fits(table, DEFAULT_ROW_LIMIT, "capped queue")

            # "running" / "queued" are the table's own literals too, and a data
            # row's state cell is the only place they land. Both are collected
            # by value rather than by row position — an index into a row that
            # turned out to be a placeholder would measure an empty cell and
            # prove nothing.
            states = {str(table.get_row_at(i)[1]) for i in range(DEFAULT_ROW_LIMIT)}
            assert states == {"running", "queued"}
            assert max(cell_len(s) for s in states) <= table.ordered_columns[1].width

            # A broker-backed queue can't be read from here, and says so in the
            # longest string this table can print.
            monkeypatch.setenv("JOBS_QUEUE_KIND", "redis")
            screen._refresh()
            await pilot.pause()
            assert "unavailable" in str(table.get_row_at(0)[0])
            fits(table, 0, "queue unavailable")

    @pytest.mark.asyncio
    async def test_rendering_the_tab_never_creates_the_maildir(
        self, tmp_path, monkeypatch
    ):
        """The hard constraint, as a test: a live worker owns that directory and
        the TUI is only allowed to look."""
        root = self._isolate(tmp_path, monkeypatch)
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            _dashboard(app)._refresh()  # force another full tick
            await pilot.pause()
        assert not root.exists()

    @pytest.mark.asyncio
    async def test_arrows_and_watch_pane_no_op_on_the_jobs_tab(
        self, tmp_path, monkeypatch
    ):
        """No frontend strip to move through, and no per-job watch yet
        (the worker doesn't publish a session uuid) — so the daemon log keeps
        the full width."""
        from textual.containers import Vertical

        self._isolate(tmp_path, monkeypatch)
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = _dashboard(app)
            before = screen._chat_selected_idx
            await pilot.press("right")
            await pilot.press("left")
            await pilot.pause()
            assert screen._chat_selected_idx == before
            assert screen._active_daemon() == "jobs"
            assert app.screen.query_one("#log-watch-col", Vertical).display is False

    @pytest.mark.asyncio
    async def test_log_pane_follows_the_worker_log(self, tmp_path, monkeypatch):
        """The shared log row follows the active tab, so the jobs tab tails the
        worker's own per-day log — which jobs/cli.py's _setup_logging writes."""
        from textual.widgets import Static

        from claude_on_the_fly import logs
        from claude_on_the_fly.tui.screens import dashboard as dash

        self._isolate(tmp_path, monkeypatch)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_name = logs.log_name("jobs")
        (log_dir / log_name).write_text("worker line\n", encoding="utf-8")
        monkeypatch.setattr(dash, "LOG_DIR", log_dir)

        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            header = str(app.screen.query_one("#log-header", Static).render())
            assert log_name in header
            assert "missing" not in header

    @pytest.mark.asyncio
    async def test_lifecycle_keys_target_the_jobs_worker(self, tmp_path, monkeypatch):
        """k/r resolve through _active_daemon, so the single elif is the whole
        of the supervisor wiring — assert it reaches supervisor.stop."""
        from claude_on_the_fly.tui.screens import dashboard as dash

        self._isolate(tmp_path, monkeypatch)
        app = ClaudeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            stopped: list[str] = []
            monkeypatch.setattr(
                dash.supervisor, "stop", lambda name: stopped.append(name) or 4321
            )
            await pilot.press("k")
            await pilot.pause()
            assert stopped == ["jobs"]


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


@pytest.mark.asyncio
async def test_doctor_re_reads_env_when_reopened(tmp_path, monkeypatch):
    """Fix a value in .env, re-open doctor, and it must report the fixed value.

    The screen is registered in App.SCREENS, so Textual builds one instance and
    re-pushes it — `on_mount` fires only the first time. Refreshing on mount alone
    left the previous verdict on screen, so fixing what doctor complained about and
    looking again showed the same complaint, which reads as "my fix did nothing".
    """
    from claude_on_the_fly.tui import supervisor
    from claude_on_the_fly.tui.screens import doctor as doctor_mod

    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_BACKEND=pty\n", encoding="utf-8")
    monkeypatch.setattr(supervisor, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.delenv("AGENT_BACKEND", raising=False)

    reads: list[str | None] = []
    real_refresh = doctor_mod.DoctorScreen._refresh

    def _spy(self):
        env = supervisor._load_env(supervisor.DEFAULT_ENV_FILE)
        reads.append(env.get("AGENT_BACKEND"))
        return real_refresh(self)

    monkeypatch.setattr(doctor_mod.DoctorScreen, "_refresh", _spy)

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert reads == ["pty"]

        # `pty` is a CLAUDE_MODE, not a backend — the mistake this reproduces.
        env_file.write_text("AGENT_BACKEND=claude\n", encoding="utf-8")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

    assert reads == ["pty", "claude"], (
        "re-opening doctor must re-read .env, not show the previous render"
    )


async def test_an_empty_event_log_produces_no_notifications(tmp_path, monkeypatch):
    """First run, before anything has been dispatched."""
    from claude_on_the_fly.events import EventLog
    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp

    app = ClaudeTuiApp()
    app._event_log = EventLog(tmp_path / "events.jsonl")
    notices: list[str] = []
    app.notify = lambda msg, **kw: notices.append(msg)  # type: ignore[method-assign]
    app._poll_worker_events()
    assert notices == []


def test_run_app_starts_the_application(monkeypatch):
    """The `claude-tui` entry point with no subcommand lands here, so a wrong class
    or a missing run() is a dead CLI."""
    from claude_on_the_fly.tui import tui_app as tui_app_mod

    started: list[str] = []
    monkeypatch.setattr(
        tui_app_mod.ClaudeTuiApp, "run", lambda self: started.append("ran")
    )
    tui_app_mod.run_app()
    assert started == ["ran"]
