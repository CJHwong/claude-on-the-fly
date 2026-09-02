"""The dashboard driven as a real Textual screen.

What matters here is which daemon a lifecycle key acts on and what happens when
that action fails. The tab decides the target (not focus, which changes on window
blur), and every supervisor failure has to name the daemon and the reason — an
unexplained no-op on `k` is the worst outcome, because the operator presses it
again.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from rich.markup import render as render_markup
from textual.app import App
from textual.widgets import DataTable, RichLog, Static, TabbedContent

import claude_on_the_fly.tui.screens.dashboard as dash
from claude_on_the_fly.checks import CheckResult
from claude_on_the_fly.tui import supervisor
from claude_on_the_fly.tui.screens.dashboard import DashboardScreen


class _Host(App):
    CSS = """
    #log-row { height: 1fr; min-height: 8; }
    #log-pane, #watch-pane { height: 1fr; min-height: 8; }
    """
    SCREENS = {"doctor": DashboardScreen}  # replaced per-test where it matters


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg = tmp_path / "cron.yaml"
    cfg.write_text("entries: []\n")
    monkeypatch.setattr(dash, "CRON_CONFIG", cfg)
    monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dash, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "DEFAULT_ENV_FILE", tmp_path / ".env")
    return tmp_path


def _freeze_refresh_ticks(screen: DashboardScreen) -> None:
    """Keep the screen's repeating `_refresh` / `_refresh_log` timers from ever
    being scheduled.

    `_refresh` resets `_job_sessions`, `_chat_workspaces` and `_job_workspaces`
    and rebuilds the cron table from the live snapshot. The tests below inject
    that state by hand and then `await pilot.pause()`, which yields the event
    loop — so a tick landing inside that yield wipes the injection and moves the
    cron cursor back to row 0, and the assertion reads the daemon's empty state.
    Locally a test finishes well inside the 1s period and never sees it; on a
    loaded CI runner it does, tripping whichever test got starved (a different
    one per run, which is what made it look like nondeterminism rather than a
    race against a timer).

    `on_mount` still calls `_refresh()` once directly, and every test drives the
    refreshes it cares about itself, so nothing here depends on the ticks.
    """
    real_set_interval = screen.set_interval

    def skip_refresh_timers(*args, **kwargs):
        callback = args[1] if len(args) > 1 else kwargs.get("callback")
        if callback in (screen._refresh, screen._refresh_log):
            return None
        return real_set_interval(*args, **kwargs)

    screen.set_interval = skip_refresh_timers  # type: ignore[method-assign]


async def _open(app: _Host, pilot) -> DashboardScreen:
    screen = DashboardScreen()
    _freeze_refresh_ticks(screen)
    await app.push_screen(screen)
    await pilot.pause()
    return screen


def _raise_oserror(*_args, **_kwargs):
    raise OSError("read-only file system")


def _result(name, status, detail="", hint=""):
    return CheckResult(name=name, status=status, detail=detail, fix_hint=hint)


@pytest.fixture
def no_suspend(monkeypatch):
    """`App.suspend` needs a real terminal to hand back, which a headless run has
    not got. The editor call itself is what these tests are about."""
    import contextlib

    monkeypatch.setattr(
        App, "suspend", lambda _self: contextlib.nullcontext(), raising=False
    )


def _capture(screen: DashboardScreen) -> list[tuple[str, str]]:
    notices: list[tuple[str, str]] = []
    screen._notify = lambda msg, severity: notices.append((msg, severity))  # type: ignore[method-assign]
    return notices


class TestActiveDaemonFollowsTheTab:
    """Focus changes when the window loses focus; the active tab does not. Keying
    off focus made `k` act on whatever was last clicked."""

    async def test_the_cron_tab_targets_cron(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            assert screen._active_daemon() == "cron"

    async def test_the_jobs_tab_targets_the_worker(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-jobs")
            await pilot.pause()
            assert screen._active_daemon() == "jobs"

    async def test_the_chat_tab_targets_the_selected_frontend(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-chat")
            await pilot.pause()
            assert screen._active_daemon() in dash.CHAT_FRONTENDS

    async def test_before_mount_it_falls_back_to_the_remembered_daemon(self):
        """`_update_action_cue` can fire from a focus event before the tabs exist."""
        screen = DashboardScreen()
        screen._last_active_daemon = "cron"
        assert screen._active_daemon() == "cron"

    async def test_an_empty_request_table_still_has_a_chat_target(self, isolated):
        """So an idle frontend is still stop/restartable."""
        screen = DashboardScreen()
        screen._chat_frontend_names = []
        assert screen._chat_supervisor_target() == dash.CHAT_FRONTENDS[0]

    async def test_the_selection_index_is_clamped(self, isolated):
        """The strip can shrink between a refresh and a keypress."""
        screen = DashboardScreen()
        screen._chat_frontend_names = ["slack"]
        screen._chat_selected_idx = 5
        assert screen._chat_supervisor_target() == "slack"


class TestStripSelection:
    async def test_arrows_cycle_the_chat_frontend(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-chat")
            await pilot.pause()
            screen._chat_frontend_names = ["slack", "telegram"]
            screen.action_strip_select(1)
            await pilot.pause()
            assert screen._chat_selected_idx == 1
            screen.action_strip_select(1)
            await pilot.pause()
            assert screen._chat_selected_idx == 0, "must wrap"

    async def test_a_single_frontend_has_nothing_to_cycle(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-chat")
            await pilot.pause()
            screen._chat_frontend_names = ["slack"]
            screen.action_strip_select(1)
            await pilot.pause()
            assert screen._chat_selected_idx == 0

    async def test_arrows_do_nothing_on_the_other_tabs(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            before = screen._chat_selected_idx
            screen.action_strip_select(1)
            await pilot.pause()
            assert screen._chat_selected_idx == before

    async def test_before_mount_it_is_a_no_op(self):
        DashboardScreen().action_strip_select(1)


class TestActionCue:
    async def test_it_names_the_daemon_the_keys_act_on(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._update_action_cue()
            cue = str(app.screen.query_one("#action-cue", Static).content)
        assert "cron" in cue

    async def test_no_target_says_to_tab_to_a_panel(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: None)
            screen._update_action_cue()
            cue = str(app.screen.query_one("#action-cue", Static).content)
        assert "Tab to a panel" in cue

    async def test_a_focus_event_before_the_cue_mounts_is_ignored(self):
        DashboardScreen()._update_action_cue()


class TestSupervisorActionFailures:
    """Each of these is a real state an operator hits, and each has to be named:
    the alternative is pressing `k` again on a daemon that was never running."""

    async def _press(self, isolated, monkeypatch, verb, error):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            notices = _capture(screen)
            monkeypatch.setattr(
                supervisor,
                verb,
                lambda _name: (_ for _ in ()).throw(error),
            )
            if verb == "stop":
                await screen.action_stop()
            else:
                await screen.action_restart()
            await pilot.pause()
        return notices

    async def test_stopping_something_not_running(self, isolated, monkeypatch):
        notices = await self._press(
            isolated, monkeypatch, "stop", supervisor.NotRunning("nope")
        )
        assert any("not running" in msg for msg, _s in notices)

    async def test_restarting_something_already_running(self, isolated, monkeypatch):
        notices = await self._press(
            isolated, monkeypatch, "restart", supervisor.AlreadyRunning("cron", 42)
        )
        assert any("already running (pid 42)" in msg for msg, _s in notices)

    async def test_a_spawn_timeout_points_at_the_log(self, isolated, monkeypatch):
        notices = await self._press(
            isolated,
            monkeypatch,
            "restart",
            supervisor.SpawnTimeout(
                frontend="cron", pid=1, log_path=Path("/logs/cron.stdout")
            ),
        )
        assert any("/logs/cron.stdout" in msg for msg, _s in notices)

    async def test_an_unexpected_error_is_reported_verbatim(
        self, isolated, monkeypatch
    ):
        notices = await self._press(
            isolated, monkeypatch, "stop", RuntimeError("disk full")
        )
        assert any("disk full" in msg for msg, _s in notices)

    async def test_a_preflight_failure_opens_the_doctor(self, isolated, monkeypatch):
        """Naming the failed check is not enough: the fix hint lives in the doctor, so
        the screen takes them there."""
        pushed: list[str] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            notices = _capture(screen)
            monkeypatch.setattr(
                supervisor,
                "restart",
                lambda _n: (_ for _ in ()).throw(
                    supervisor.PreflightFailed(
                        "cron", [_result("CRON_CONFIG", "missing", "no file")]
                    )
                ),
            )
            app.push_screen = lambda name, *a, **kw: pushed.append(name)  # type: ignore[method-assign]
            await screen.action_restart()
            await pilot.pause()
        assert pushed == ["doctor"]
        assert any("CRON_CONFIG" in msg for msg, _s in notices)

    async def test_a_preflight_failure_with_no_blocking_check_still_reports(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            notices = _capture(screen)
            monkeypatch.setattr(
                supervisor,
                "restart",
                lambda _n: (_ for _ in ()).throw(
                    supervisor.PreflightFailed("cron", [])
                ),
            )
            app.push_screen = lambda *a, **kw: None  # type: ignore[method-assign]
            await screen.action_restart()
            await pilot.pause()
        assert any("checks failed" in msg for msg, _s in notices)

    async def test_a_successful_stop_reports_the_pid(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            notices = _capture(screen)
            monkeypatch.setattr(supervisor, "stop", lambda _n: 4242)
            await screen.action_stop()
            await pilot.pause()
        assert any("stopped cron (pid 4242)" in msg for msg, _s in notices)

    async def test_no_daemon_selected_says_to_tab_first(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(screen, "_active_daemon", lambda: None)
            await screen.action_stop()
            await pilot.pause()
        assert any("no daemon selected" in msg for msg, _s in notices)

    async def test_a_second_press_while_busy_is_ignored(self, isolated, monkeypatch):
        """The spinner is showing; queueing a second stop behind it would act on a
        daemon whose state has already changed."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            calls: list[str] = []
            monkeypatch.setattr(supervisor, "stop", lambda n: (calls.append(n), 1)[1])
            screen._set_busy("stopping cron")
            await screen.action_stop()
            await pilot.pause()
        assert calls == []


class TestStopAllAndResume:
    async def test_stopping_nothing_says_so(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(supervisor, "stop_all", lambda: [])
            await screen.action_stop_all()
            await pilot.pause()
        assert any("nothing running" in msg for msg, _s in notices)

    async def test_stopping_several_names_them_and_points_at_resume(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(
                supervisor, "stop_all", lambda: [("slack", 1), ("cron", 2)]
            )
            await screen.action_stop_all()
            await pilot.pause()
        assert any("slack, cron" in msg and "press u" in msg for msg, _s in notices)

    async def test_stop_all_while_busy_is_ignored(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            calls: list[int] = []
            monkeypatch.setattr(
                supervisor, "stop_all", lambda: (calls.append(1), [])[1]
            )
            screen._set_busy("busy")
            await screen.action_stop_all()
            await pilot.pause()
        assert calls == []

    async def test_resuming_with_no_record_says_so(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(supervisor, "read_last_running", lambda: [])
            await screen.action_resume()
            await pilot.pause()
        assert any("nothing to resume" in msg for msg, _s in notices)

    async def test_a_partial_resume_reports_both_halves(self, isolated, monkeypatch):
        """Reporting only the successes would hide the daemon that stayed down."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(
                supervisor, "read_last_running", lambda: ["slack", "cron"]
            )
            monkeypatch.setattr(
                supervisor,
                "resume",
                lambda: [("slack", 1, None), ("cron", None, RuntimeError("no token"))],
            )
            await screen.action_resume()
            await pilot.pause()
        assert any("started 1: slack" in msg for msg, _s in notices)
        assert any("no token" in msg and sev == "error" for msg, sev in notices)

    async def test_resume_while_busy_is_ignored(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            calls: list[int] = []
            monkeypatch.setattr(
                supervisor, "read_last_running", lambda: (calls.append(1), [])[1]
            )
            screen._set_busy("busy")
            await screen.action_resume()
            await pilot.pause()
        assert calls == []


class TestBusySpinner:
    async def test_it_advances_while_set(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._set_busy("stopping cron")
            first = str(app.screen.query_one("#status-line", Static).content)
            screen._tick_busy()
            second = str(app.screen.query_one("#status-line", Static).content)
        assert "stopping cron" in first
        assert first != second, "the spinner did not turn"

    async def test_ticking_when_idle_does_nothing(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._clear_busy()
            screen._tick_busy()
            screen._render_busy_line()
            await pilot.pause()


class TestCopyLog:
    async def test_no_log_selected_says_so(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            screen._log_path = None
            screen._watch_path = None
            screen.action_copy_log()
            await pilot.pause()
        assert any("no log selected" in msg for msg, _s in notices)

    async def test_the_watch_pane_wins_when_both_are_open(self, isolated):
        """It is the more specific of the two: a per-job session beats the whole
        daemon's log."""
        daemon_log = isolated / "daemon.log"
        daemon_log.write_text("daemon line\n")
        watch_log = isolated / "watch.log"
        watch_log.write_text("watch line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._log_path = daemon_log
            screen._watch_path = watch_log
            copied: list[str] = []
            app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
            screen.action_copy_log()
            await pilot.pause()
        assert copied == ["watch line"]

    async def test_only_the_tail_is_copied(self, isolated):
        log = isolated / "big.log"
        log.write_text("".join(f"line {i}\n" for i in range(900)))
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_path = None
            screen._log_path = log
            copied: list[str] = []
            app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
            notices = _capture(screen)
            screen.action_copy_log()
            await pilot.pause()
        assert len(copied[0].splitlines()) == 500
        assert copied[0].startswith("line 400")
        assert any("copied last 500 line(s)" in msg for msg, _s in notices)

    async def test_a_log_that_cannot_be_read_is_reported(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_path = None
            screen._log_path = isolated / "gone.log"
            notices = _capture(screen)
            screen.action_copy_log()
            await pilot.pause()
        assert any("copy failed" in msg for msg, _s in notices)

    async def test_a_clipboard_that_will_not_write_is_reported(self, isolated):
        """Over SSH without a clipboard bridge this is the normal case."""
        log = isolated / "small.log"
        log.write_text("x\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_path = None
            screen._log_path = log
            app.copy_to_clipboard = lambda _t: (_ for _ in ()).throw(  # type: ignore[method-assign]
                RuntimeError("no clipboard")
            )
            notices = _capture(screen)
            screen.action_copy_log()
            await pilot.pause()
        assert any("clipboard write failed" in msg for msg, _s in notices)


class TestHelpScreen:
    async def test_every_described_binding_is_listed(self, isolated):
        """Built from BINDINGS so the keymap cannot drift from what the keys do."""
        pushed: list[object] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            app.push_screen = lambda s, *a, **kw: pushed.append(s)  # type: ignore[method-assign]
            screen.action_help()
            await pilot.pause()
        assert pushed
        rows = pushed[0]._rows if hasattr(pushed[0], "_rows") else None
        assert rows is None or len(rows) > 5


class TestConfigEditing:
    async def test_cancelling_the_picker_edits_nothing(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            edits: list[str] = []
            monkeypatch.setattr(
                screen, "_edit_cron_config", lambda: edits.append("cron")
            )
            monkeypatch.setattr(screen, "_edit_env", lambda: edits.append("env"))
            screen._open_config_target(None)
            await pilot.pause()
        assert edits == []

    @pytest.mark.parametrize(("choice", "expected"), [("cron", "cron"), ("env", "env")])
    async def test_each_choice_routes_to_its_editor(
        self, isolated, monkeypatch, choice, expected
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            edits: list[str] = []
            monkeypatch.setattr(
                screen, "_edit_cron_config", lambda: edits.append("cron")
            )
            monkeypatch.setattr(screen, "_edit_env", lambda: edits.append("env"))
            monkeypatch.setattr(
                screen, "_edit_sandbox_config", lambda: edits.append("sandbox")
            )
            screen._open_config_target(choice)
            await pilot.pause()
        assert edits == [expected]

    async def test_editing_the_cron_config_seeds_an_example(
        self, isolated, monkeypatch, no_suspend
    ):
        """An empty file in $EDITOR gives the operator nothing to work from."""
        seeds: list[object] = []
        monkeypatch.setattr(
            dash.env_editor,
            "open_in_editor",
            lambda path, seed=None: seeds.append(seed),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            _capture(screen)
            screen._edit_cron_config()
            await pilot.pause()
        assert seeds and "entries" in str(seeds[0])

    async def test_an_env_edit_with_no_changes_says_so(
        self, isolated, monkeypatch, no_suspend
    ):
        from claude_on_the_fly.tui.env_editor import EnvDiff

        monkeypatch.setattr(dash.env_editor, "edit_and_diff", lambda _path: EnvDiff())
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            screen._edit_env()
            await pilot.pause()
        assert any("no env changes" in msg for msg, _s in notices)

    async def test_a_real_env_change_opens_the_diff_modal(
        self, isolated, monkeypatch, no_suspend
    ):
        from claude_on_the_fly.tui.env_editor import EnvDiff

        monkeypatch.setattr(
            dash.env_editor,
            "edit_and_diff",
            lambda _path: EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}),
        )
        pushed: list[object] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            app.push_screen = lambda s, *a, **kw: pushed.append(s)  # type: ignore[method-assign]
            screen._edit_env()
            await pilot.pause()
        assert pushed and isinstance(pushed[0], dash.EnvDiffScreen)

    async def test_the_open_config_key_shows_the_picker(self, isolated):
        pushed: list[object] = []
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            app.push_screen = lambda s, *a, **kw: pushed.append(s)  # type: ignore[method-assign]
            screen.action_open_config()
            await pilot.pause()
        assert pushed and isinstance(pushed[0], dash.ConfigPickerScreen)


class TestCursorHelpers:
    async def test_a_missing_table_has_no_cursor_key(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert screen._datatable_cursor_key("#no-such-table") is None

    async def test_an_empty_table_has_no_cursor_key(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            table.clear()
            await pilot.pause()
            assert screen._datatable_cursor_key("#cron-entries") is None

    async def test_a_coordinate_failure_has_no_cursor_key(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            table.add_row("a", "b", "c", "d", key="a")
            await pilot.pause()
            monkeypatch.setattr(
                table,
                "coordinate_to_cell_key",
                lambda _c: (_ for _ in ()).throw(RuntimeError("gone")),
            )
            assert screen._datatable_cursor_key("#cron-entries") is None

    async def test_restoring_a_cursor_that_is_already_there_does_not_move_it(
        self, isolated
    ):
        """move_cursor emits RowHighlighted even for a no-op, and the 1Hz refresh
        would then rewrite the log pane every tick."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            table.clear()
            table.add_row("a", "", "", "", key="a")
            table.add_row("b", "", "", "", key="b")
            await pilot.pause()
            moves: list[int] = []
            table.move_cursor = lambda **kw: moves.append(kw.get("row", -1))  # type: ignore[method-assign]
            screen._restore_cursor(table, ["a", "b"], "a")
        assert moves == []

    async def test_restoring_a_cursor_elsewhere_moves_it(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            table.clear()
            table.add_row("a", "", "", "", key="a")
            table.add_row("b", "", "", "", key="b")
            await pilot.pause()
            moves: list[int] = []
            table.move_cursor = lambda **kw: moves.append(kw.get("row", -1))  # type: ignore[method-assign]
            screen._restore_cursor(table, ["a", "b"], "b")
        assert moves == [1]

    async def test_restoring_nothing_is_a_no_op(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            moves: list[int] = []
            table.move_cursor = lambda **kw: moves.append(kw.get("row", -1))  # type: ignore[method-assign]
            screen._restore_cursor(table, ["a"], None)
        assert moves == []


def _session_event(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write_session(path: Path, *texts: str) -> Path:
    path.write_text("".join(json.dumps(_session_event(t)) + "\n" for t in texts))
    return path


def _pane_text(app: _Host, selector: str) -> str:
    pane = app.screen.query_one(selector, RichLog)
    return "\n".join(seg.text for line in pane.lines for seg in line._segments)


class TestWatchPaneVisibility:
    async def test_it_hides_when_nothing_is_drilled_into(self, isolated):
        """Hidden gives the daemon log full width, which is what an operator wants
        when there is no specific job to follow."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-jobs")
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            assert app.screen.query_one("#log-watch-col").display is False
        assert screen._watch_target is None

    async def test_a_highlighted_cron_entry_opens_it(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            assert app.screen.query_one("#log-watch-col").display is True
        assert screen._watch_target == "cron:nightly"

    async def test_the_placeholder_row_is_not_a_target(self, isolated, monkeypatch):
        """`__empty__` is the "no jobs (g)" row, not a job."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "__empty__")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
        assert screen._watch_target is None

    async def test_switching_target_forces_a_reload(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "one")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "two")
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()
        assert screen._watch_target == "cron:two"


class TestWatchCron:
    async def test_a_job_that_has_never_fired_says_so(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "hasn't fired since startup" in rendered

    async def test_a_job_log_is_tailed_as_plain_text(self, isolated, monkeypatch):
        """Markup is bypassed on purpose: a literal [INFO] in a log line must survive
        even though the pane has markup on for the session watch."""
        from claude_on_the_fly import logs as logs_mod

        log = isolated / logs_mod.log_name("cron-nightly")
        log.write_text("[INFO] started\n[WARN] slow\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
            header = str(app.screen.query_one("#watch-header", Static).content)
        assert "[INFO] started" in rendered
        assert "[WARN] slow" in rendered
        assert "nightly" in header

    async def test_an_empty_job_log_says_it_is_empty(self, isolated, monkeypatch):
        from claude_on_the_fly import logs as logs_mod

        (isolated / logs_mod.log_name("cron-nightly")).write_text("")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "is empty" in rendered

    async def test_a_quiet_job_log_is_not_re_read(self, isolated, monkeypatch):
        from claude_on_the_fly import logs as logs_mod

        (isolated / logs_mod.log_name("cron-nightly")).write_text("line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            reads = {"n": 0}
            real_tail = dash.render.tail_lines
            monkeypatch.setattr(
                dash.render,
                "tail_lines",
                lambda p, n: (reads.__setitem__("n", reads["n"] + 1), real_tail(p, n))[
                    1
                ],
            )
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()
        assert reads["n"] == 0

    async def test_a_log_that_vanishes_mid_tick_stops_quietly(
        self, isolated, monkeypatch
    ):
        """It exists when the branch is chosen and is gone by the time the mtime is
        read, which is what retention pruning under a live dashboard looks like."""

        class VanishingPath:
            name = "cron-nightly-host-2026-07-30.log"

            def is_file(self):
                return True

            def stat(self):
                raise OSError("stale handle")

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            monkeypatch.setattr(
                dash.logs, "find_log", lambda _role, directory=None: VanishingPath()
            )
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
        # No crash, and nothing claimed as rendered.
        assert screen._watch_path is None

    async def test_a_reader_scrolled_up_keeps_their_place(self, isolated, monkeypatch):
        import os

        from claude_on_the_fly import logs as logs_mod

        log = isolated / logs_mod.log_name("cron-nightly")
        log.write_text("".join(f"line {i}\n" for i in range(50)))
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            restores: list[int] = []
            monkeypatch.setattr(dash.render, "capture_scroll", lambda _p: (False, 9))
            monkeypatch.setattr(
                dash.render,
                "restore_scroll",
                lambda _p, *, prev_y: restores.append(prev_y),
            )
            log.write_text("".join(f"line {i}\n" for i in range(60)))
            os.utime(log, (9999, 9999))
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()
        assert restores == [9]


class TestWatchSession:
    def _wire_chat(self, screen: DashboardScreen, monkeypatch, *, uuid="s-1"):
        screen.action_show_tab("tab-chat")
        monkeypatch.setattr(screen, "_datatable_cursor_key", lambda _sel: "telegram:42")
        screen._chat_workspaces = {"telegram:42": "telegram/hoss"}
        screen._job_sessions = {"telegram:42": uuid} if uuid else {}
        monkeypatch.setattr(screen, "_active_daemon", lambda: "telegram")

    async def test_a_row_with_no_session_uuid_says_pending(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch, uuid="")
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
            header = str(app.screen.query_one("#watch-header", Static).content)
        assert "no session uuid" in rendered
        assert "pending" in header

    async def test_no_session_log_yet_says_the_agent_has_not_run(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: None)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "hasn't run a turn" in rendered

    async def test_a_session_log_is_rendered(self, isolated, monkeypatch):
        log = _write_session(isolated / "session.jsonl", "hello from the agent")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
            header = str(app.screen.query_one("#watch-header", Static).content)
        assert "hello from the agent" in rendered
        assert "telegram/hoss" in header

    async def test_a_log_with_nothing_displayable_says_so(self, isolated, monkeypatch):
        log = isolated / "session.jsonl"
        log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "no displayable events yet" in rendered

    async def test_blank_and_malformed_lines_are_skipped(self, isolated, monkeypatch):
        log = isolated / "session.jsonl"
        log.write_text("\nnot json\n" + json.dumps(_session_event("real")) + "\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "real" in rendered

    async def test_a_quiet_session_is_not_re_read(self, isolated, monkeypatch):
        log = _write_session(isolated / "session.jsonl", "x")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            reads = {"n": 0}
            real_tail = dash.render.tail_lines
            monkeypatch.setattr(
                dash.render,
                "tail_lines",
                lambda p, n: (reads.__setitem__("n", reads["n"] + 1), real_tail(p, n))[
                    1
                ],
            )
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()
        assert reads["n"] == 0

    async def test_a_session_log_that_vanishes_stops_quietly(
        self, isolated, monkeypatch
    ):
        log = _write_session(isolated / "session.jsonl", "x")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            log.unlink()
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()

    async def test_a_reader_scrolled_up_keeps_their_place(self, isolated, monkeypatch):
        import os

        log = _write_session(
            isolated / "session.jsonl", *[f"line {i}" for i in range(30)]
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            restores: list[int] = []
            monkeypatch.setattr(dash.render, "capture_scroll", lambda _p: (False, 4))
            monkeypatch.setattr(
                dash.render,
                "restore_scroll",
                lambda _p, *, prev_y: restores.append(prev_y),
            )
            _write_session(log, *[f"line {i}" for i in range(31)])
            os.utime(log, (9999, 9999))
            screen._refresh_watch_pane(force_reload=False)
            await pilot.pause()
        assert restores == [4]

    async def test_a_row_key_without_a_colon_is_not_a_target(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-chat")
            monkeypatch.setattr(screen, "_active_daemon", lambda: "telegram")
            monkeypatch.setattr(screen, "_datatable_cursor_key", lambda _s: "nocolon")
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
        assert screen._watch_target is None


class TestJobIdAndPromptCells:
    def test_a_long_job_id_keeps_its_readable_tail(self):
        """The ns timestamp prefix is noise; the suffix is what the operator
        recognises."""
        assert dash._short_job_id("1755000000000000000-abcdef12") == "abcdef12"

    def test_an_id_with_no_separator_is_used_whole(self):
        assert dash._short_job_id("plainid") == "plainid"

    def test_an_unreadable_prompt_is_said_plainly(self):
        """Claimed mid-read, or hand-mangled. An empty cell reads as an empty prompt,
        which is a different problem."""
        assert dash._prompt_preview(None) == "(unreadable)"

    def test_a_multi_line_prompt_is_flattened(self):
        """A DataTable cell cannot render a newline."""
        assert dash._prompt_preview("first\nsecond") == "first second"

    def test_a_huge_prompt_is_clipped(self):
        assert len(dash._prompt_preview("x" * 500)) == 80


class TestJobsQueueTable:
    def _view(self, rows, *, hidden=0):
        from claude_on_the_fly.jobs.file_queue import QueueDepth
        from claude_on_the_fly.tui.state import JobsQueueView

        return JobsQueueView(
            depth=QueueDepth(new=len(rows), running=0, done=0, failed=0),
            rows=rows,
            hidden=hidden,
        )

    def _row(self, job_id="1755000000000000000-abcdef12", **fields):
        from datetime import UTC, datetime

        from claude_on_the_fly.jobs.file_queue import QueueRow

        base = {
            "id": job_id,
            "prompt": "do the thing",
            "origin": {"channel": "C1"},
            "in_flight": False,
            "enqueued_at": datetime.now(UTC),
        }
        base.update(fields)
        return QueueRow(**base)

    def _snap(self, view):
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import Snapshot

        return Snapshot(
            timestamp=datetime.now(UTC),
            frontends=[],
            jobs=[],
            schedule_error=None,
            jobs_queue=view,
        )

    async def test_a_queued_job_is_listed(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(self._snap(self._view([self._row()])), None)
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
        assert "abcdef12" in row
        assert "queued" in row
        assert "do the thing" in row

    async def test_a_running_job_says_running(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(self._view([self._row(in_flight=True)])), None
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert "running" in [str(c) for c in table.get_row_at(0)]

    async def test_markup_in_a_prompt_cannot_kill_the_dashboard(self, isolated):
        """The prompt is the one cell carrying third-party text. `[/]` raises
        MarkupError, which Textual turns into an app exit."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(self._view([self._row(prompt="check [/] and [pytest]")])),
                None,
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert "[/]" in str(table.get_row_at(0)[3])

    async def test_a_job_with_no_enqueue_time_shows_no_age(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(self._view([self._row(enqueued_at=None)])), None
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[4]) == "-"

    async def test_a_cron_job_is_named_by_its_entry(self, isolated):
        """The entry is what an operator matches against cron.yaml and against
        the entry's own log; the bare word "cron" would say nothing."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(
                    self._view(
                        [self._row(origin={"kind": "cron", "entry": "jira-poll"})]
                    )
                ),
                None,
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[1]) == "jira-poll"

    async def test_a_chat_job_is_named_by_its_producer(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(
                    self._view([self._row(origin={"kind": "slack", "channel": "C1"})])
                ),
                None,
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[1]) == "slack"

    async def test_an_origin_without_a_kind_claims_no_producer(self, isolated):
        """A record written before the field existed, or one that could not be
        read at all."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(self._snap(self._view([self._row(origin={})])), None)
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[1]) == "-"

    async def test_a_cron_origin_missing_its_entry_still_names_the_producer(
        self, isolated
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(self._view([self._row(origin={"kind": "cron"})])), None
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[1]) == "cron"

    async def test_the_prompt_column_takes_the_leftover_width(self, isolated):
        """Five columns no longer fit an 80-column terminal at fixed widths, so
        the prompt takes what the other four leave and the table fits."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-jobs")
            await pilot.pause()
            screen._refresh_jobs(
                self._snap(self._view([self._row(prompt="x" * 200)])), None
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            total = sum(c.get_render_width(table) for c in table.columns.values())
            # Inside the run_test block: the widget's size resets to 0 when the
            # app shuts down, so the assertion must not outlive it.
            assert total == table.size.width

    async def test_an_empty_queue_keeps_a_focusable_row(self, isolated):
        """So the table stays reachable by Tab and the lifecycle keys still work."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(self._snap(self._view([])), None)
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
        assert table.row_count == 1
        assert "queue empty" in str(table.get_row_at(0)[0])

    async def test_an_unavailable_queue_says_so_rather_than_empty(self, isolated):
        """ "Empty" and "cannot read the maildir" are different problems."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(self._snap(None), None)
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
        assert "queue unavailable" in str(table.get_row_at(0)[0])

    async def test_a_capped_listing_says_what_it_cut(self, isolated):
        """Otherwise the operator has to subtract it out of the header's own count."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(self._snap(self._view([self._row()], hidden=7)), None)
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            last = str(table.get_row_at(table.row_count - 1)[0])
        assert "7 more" in last


class TestCronTable:
    def _snap(self, jobs, error=None, queue=None):
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import Snapshot

        return Snapshot(
            timestamp=datetime.now(UTC),
            frontends=[],
            jobs=jobs,
            schedule_error=error,
            jobs_queue=queue,
        )

    def _queue(self, *entries):
        """A queue view holding one in-flight cron job per name given."""
        from datetime import UTC, datetime

        from claude_on_the_fly.jobs.file_queue import QueueDepth, QueueRow
        from claude_on_the_fly.tui.state import JobsQueueView

        rows = [
            QueueRow(
                id=f"{i}-aaaaaaaa",
                prompt="p",
                origin={"kind": "cron", "entry": name},
                enqueued_at=datetime.now(UTC),
                in_flight=True,
            )
            for i, name in enumerate(entries)
        ]
        return JobsQueueView(
            depth=QueueDepth(new=0, running=len(rows), done=0, failed=0), rows=rows
        )

    def _job(self, name="nightly", detail=""):
        from datetime import datetime, timedelta

        from claude_on_the_fly.tui.state import JobInfo

        return JobInfo(
            name=name,
            cron="0 4 * * *",
            kind="prompt",
            next_fire=datetime.now() + timedelta(hours=1),
            detail=detail,
        )

    @staticmethod
    def _prompt_col(table):
        return next(
            c for c in table.columns.values() if c.label.plain == dash.PROMPT_COLUMN
        )

    def _status(self, state_str):
        from claude_on_the_fly.tui.state import FrontendStatus

        return FrontendStatus(name="cron", state=state_str)

    async def test_an_entry_whose_work_is_in_flight_says_running(self, isolated):
        """The entry an operator is watching is the one whose countdown matters
        least: its work is already running."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("nightly")], queue=self._queue("nightly")),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert str(table.get_row_at(0)[3]) == "running"

    async def test_a_producer_entry_counts_its_in_flight_items(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap(
                    [self._job("jira")], queue=self._queue("jira", "jira", "other")
                ),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert str(table.get_row_at(0)[3]) == "running (2)"

    async def test_an_idle_entry_keeps_its_countdown(self, isolated):
        """Another entry's job running must not blank out this one's next fire."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("nightly")], queue=self._queue("something-else")),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert "in 1.0h" in str(table.get_row_at(0)[3])

    async def test_a_queued_job_is_not_yet_running(self, isolated):
        """Only a claimed job is running; a queued one leaves the countdown,
        which is still the honest answer to when this entry next fires."""
        from datetime import UTC, datetime

        from claude_on_the_fly.jobs.file_queue import QueueDepth, QueueRow
        from claude_on_the_fly.tui.state import JobsQueueView

        view = JobsQueueView(
            depth=QueueDepth(new=1, running=0, done=0, failed=0),
            rows=[
                QueueRow(
                    id="1-aaaaaaaa",
                    prompt="p",
                    origin={"kind": "cron", "entry": "nightly"},
                    enqueued_at=datetime.now(UTC),
                    in_flight=False,
                )
            ],
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("nightly")], queue=view), self._status("running")
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert "in 1.0h" in str(table.get_row_at(0)[3])

    async def test_a_chat_job_never_marks_a_cron_entry_running(self, isolated):
        """A Slack job's origin carries no entry, and an entry name that happens
        to match a channel must not be read as one."""
        from datetime import UTC, datetime

        from claude_on_the_fly.jobs.file_queue import QueueDepth, QueueRow
        from claude_on_the_fly.tui.state import JobsQueueView

        view = JobsQueueView(
            depth=QueueDepth(new=0, running=1, done=0, failed=0),
            rows=[
                QueueRow(
                    id="1-aaaaaaaa",
                    prompt="p",
                    origin={"kind": "slack", "entry": "nightly"},
                    enqueued_at=datetime.now(UTC),
                    in_flight=True,
                )
            ],
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("nightly")], queue=view), self._status("running")
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert "in 1.0h" in str(table.get_row_at(0)[3])

    async def test_a_cron_origin_without_an_entry_marks_nothing(self, isolated):
        from datetime import UTC, datetime

        from claude_on_the_fly.jobs.file_queue import QueueDepth, QueueRow
        from claude_on_the_fly.tui.state import JobsQueueView

        view = JobsQueueView(
            depth=QueueDepth(new=0, running=1, done=0, failed=0),
            rows=[
                QueueRow(
                    id="1-aaaaaaaa",
                    prompt="p",
                    origin={"kind": "cron"},
                    enqueued_at=datetime.now(UTC),
                    in_flight=True,
                )
            ],
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("nightly")], queue=view), self._status("running")
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert "in 1.0h" in str(table.get_row_at(0)[3])

    async def test_a_running_scheduler_shows_the_next_fire(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(self._snap([self._job()]), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
        assert "nightly" in row
        assert any("in " in cell for cell in row), row

    async def test_a_stopped_scheduler_shows_no_fire_time(self, isolated):
        """A next-fire time for a daemon that is not running is a lie."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(self._snap([self._job()]), self._status("stopped"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            assert str(table.get_row_at(0)[3]) == "-"

    async def test_an_empty_schedule_points_at_the_config_key(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(self._snap([]), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
        assert "no jobs (g)" in str(table.get_row_at(0)[0])

    async def test_the_cursor_survives_a_refresh(self, isolated, monkeypatch):
        """The table is rebuilt every second; losing the cursor would make the watch
        pane unusable."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            jobs = [self._job("a"), self._job("b")]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
            assert screen._selected_job() == "b"
            # No pause between: the restore is synchronous, and the screen's own
            # 1Hz refresh would otherwise rebuild the table from the real (empty)
            # snapshot and drop the cursor for reasons unrelated to this.
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            assert screen._selected_job() == "b"

    async def test_the_detail_cell_reaches_the_table(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job(detail="summarise my inbox")]),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
        assert "summarise my inbox" in row

    async def test_the_prompt_column_takes_the_leftover_width(self, isolated):
        """The name column auto-sizes to its content; the prompt column gets the
        rest, so the table fits the terminal instead of overflowing."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            # The cron table only gets a size once its tab is laid out; the
            # 1Hz refresh refits the column from that size.
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("a-long-name", detail="x" * 200)]),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            total = sum(c.get_render_width(table) for c in table.columns.values())
            # Inside the run_test block: the widget's size resets to 0 when
            # the app shuts down, so the assertion must not outlive it.
            assert total == table.size.width

    async def test_resize_is_a_no_op_before_layout(self, isolated, monkeypatch):
        """The first _refresh runs before layout, when the table's size is still
        0 — the column keeps its mount-time width rather than going negative."""
        from textual.geometry import Size

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            table = app.screen.query_one("#cron-entries", DataTable)
            monkeypatch.setattr(DataTable, "size", property(lambda self: Size(0, 0)))
            screen._resize_flex_column(table, dash.PROMPT_COLUMN, auto_label="name")
            prompt = self._prompt_col(table)
        assert prompt.width == 14

    async def test_resize_without_the_prompt_column_is_a_no_op(self, isolated):
        """Defensive: a table that never had the column (the chat strip) is left
        untouched — including its missing `name` column, which the cron table's
        auto-sizing measures."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-chat")
            await pilot.pause()
            table = app.screen.query_one("#chat-strip", DataTable)
            before = {c.label.plain: c.width for c in table.columns.values()}
            screen._resize_flex_column(table, dash.PROMPT_COLUMN, auto_label="name")
            after = {c.label.plain: c.width for c in table.columns.values()}
        assert before == after

    async def test_on_resize_refits_the_prompt_column(self, isolated):
        """A terminal resize re-fits the column (the second resize event carries
        the table's fresh size)."""
        from textual import events
        from textual.geometry import Size

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("a-long-name")]), self._status("running")
            )
            await pilot.pause()
            screen.on_resize(
                events.Resize(size=Size(80, 24), virtual_size=Size(80, 24))
            )
            table = app.screen.query_one("#cron-entries", DataTable)
            total = sum(c.get_render_width(table) for c in table.columns.values())
            # Inside the run_test block: the widget's size resets to 0 when
            # the app shuts down, so the assertion must not outlive it.
            assert total == table.size.width

    async def test_the_table_cell_collapses_the_detail(self, isolated):
        """A DataTable cell cannot render a newline; the full text lives in
        the detail block."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("digest", detail="summarise\nmy inbox")]),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
        assert "summarise my inbox" in row
        assert "\n" not in row[4]

    async def test_the_detail_block_shows_the_highlighted_row(self, isolated):
        """The block below the table shows the full prompt as written, not the
        collapsed one-liner the cell clips."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("digest", detail="summarise\nmy inbox")]),
                self._status("running"),
            )
            await pilot.pause()
            header = app.screen.query_one("#cron-detail-header", Static)
            rendered = _pane_text(app, "#cron-detail")
            # Inside the run_test block: display reverts to the CSS default
            # (none) when the app shuts down.
            assert header.display
            assert "digest" in str(header.content)
            assert "summarise\nmy inbox" in rendered

    async def test_the_detail_block_follows_the_cursor(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            jobs = [
                self._job("a", detail="prompt a"),
                self._job("b", detail="prompt b"),
            ]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
            rendered = _pane_text(app, "#cron-detail")
        assert "prompt b" in rendered
        assert "prompt a" not in rendered

    async def test_the_detail_block_hides_when_nothing_is_highlighted(self, isolated):
        """An empty schedule leaves the block hidden so the log row keeps the
        space."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("digest", detail="summarise")]),
                self._status("running"),
            )
            await pilot.pause()
            screen._refresh_cron(self._snap([]), self._status("running"))
            await pilot.pause()
            header = app.screen.query_one("#cron-detail-header", Static)
            pane = app.screen.query_one("#cron-detail", RichLog)
        assert not header.display
        assert not pane.display

    async def test_the_detail_block_is_not_rewritten_when_unchanged(self, isolated):
        """The 1Hz refresh must not repaint the block every tick; a rewrite
        would clear the pane, so a clear that raises proves the skip."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("digest", detail="summarise")]),
                self._status("running"),
            )
            await pilot.pause()
            pane = app.screen.query_one("#cron-detail", RichLog)
            pane.clear = lambda: (_ for _ in ()).throw(AssertionError("rewritten"))  # type: ignore[method-assign]
            screen._refresh_cron_detail()

    async def test_the_detail_block_hides_for_a_row_without_detail(self, isolated):
        """Defensive: a highlighted row the snapshot knows nothing about (it
        can only appear by hand-editing the table) hides the block, even when
        it was showing another job's prompt."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            screen._refresh_cron(
                self._snap([self._job("digest", detail="summarise")]),
                self._status("running"),
            )
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            table.clear()
            table.add_row("orphan-a", "", "", "", key="orphan-a")
            table.add_row("orphan-b", "", "", "", key="orphan-b")
            await pilot.pause()
            table.move_cursor(row=1)
            await pilot.pause()
            header = app.screen.query_one("#cron-detail-header", Static)
            pane = app.screen.query_one("#cron-detail", RichLog)
        assert not header.display
        assert not pane.display

    async def test_the_header_shows_the_job_count(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_cron(
                self._snap([self._job("a"), self._job("b")]),
                self._status("running"),
            )
            await pilot.pause()
            header = str(app.screen.query_one("#cron-header", Static).content)
        assert "2 jobs" in header

    async def test_the_header_names_the_sort_mode(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._cron_sort = "name"
            screen._refresh_cron(self._snap([self._job()]), self._status("running"))
            await pilot.pause()
            header = str(app.screen.query_one("#cron-header", Static).content)
        assert "sort: name" in header

    async def test_shift_s_toggles_the_sort_mode(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            assert screen._cron_sort == "next"
            await pilot.press("S")
            await pilot.pause()
            assert screen._cron_sort == "name"
            await pilot.press("S")
            await pilot.pause()
            assert screen._cron_sort == "next"

    async def test_lowercase_s_does_not_sort(self, isolated):
        """`S`, not `s` — the lowercase key is free for the DataTable."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert screen._cron_sort == "next"

    async def test_the_cron_table_leaves_room_for_the_detail_block(self, isolated):
        """A long entry list must not push the prompt preview off the panel.

        The table is the panel's flexible child, so it scrolls instead of
        growing; the detail block below it stays on screen.
        """
        app = _Host()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            jobs = [
                self._job(name=f"entry-{i:02d}", detail=f"do the thing {i} " * 6)
                for i in range(30)
            ]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            await pilot.pause()
            panel = app.screen.query_one("#cron-panel")
            table = app.screen.query_one("#cron-entries", DataTable)
            detail = app.screen.query_one("#cron-detail", RichLog)
            assert detail.display
            assert detail.region.height > 0
            assert detail.region.bottom <= panel.region.bottom
            assert table.region.bottom <= detail.region.y

    async def test_a_scrolled_table_stays_put_across_a_refresh(self, isolated):
        """`clear()` drops the scroll offset and the cursor restore scrolls
        back to it, so a scrolled table snapped to the top and back on every
        1Hz tick — a flash the operator sees once a second."""
        app = _Host()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            jobs = [self._job(name=f"entry-{i:02d}") for i in range(30)]
            snap, status = self._snap(jobs), self._status("running")
            screen._refresh_cron(snap, status)
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            table.move_cursor(row=25)
            await pilot.pause()
            scrolled = table.scroll_offset.y
            assert scrolled > 0
            screen._refresh_cron(snap, status)
            # Read before the next pause: the flash was the intermediate frame.
            assert table.scroll_offset.y == scrolled
            await pilot.pause()
            assert table.scroll_offset.y == scrolled
            assert table.cursor_row == 25

    async def test_a_short_entry_list_keeps_the_table_at_its_content_height(
        self, isolated
    ):
        """The flexible table is capped at the rows it has, so two entries
        don't paint a half-screen of empty zebra stripes."""
        app = _Host()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            jobs = [self._job(name=f"entry-{i}", detail="short") for i in range(2)]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
            # 2 rows + the column header.
            assert table.region.height == 3

    async def test_name_sort_orders_the_table(self, isolated):
        """The snapshot arrives sorted by next fire; the name order is a
        display-only re-sort on top of it."""
        from datetime import datetime, timedelta

        from claude_on_the_fly.tui.state import JobInfo

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._cron_sort = "name"
            jobs = [
                JobInfo(
                    name="zeta",
                    cron="0 4 * * *",
                    kind="prompt",
                    next_fire=datetime.now() + timedelta(minutes=1),
                ),
                JobInfo(
                    name="alpha",
                    cron="0 4 * * *",
                    kind="prompt",
                    next_fire=datetime.now() + timedelta(hours=2),
                ),
            ]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            await pilot.pause()
            table = app.screen.query_one("#cron-entries", DataTable)
        assert str(table.get_row_at(0)[0]) == "alpha"
        assert str(table.get_row_at(1)[0]) == "zeta"

    async def test_a_large_schedule_is_capped_and_scrolls(self, isolated):
        """The table must not grow unboundedly and squeeze the log row off
        the screen: with more entries than fit, it caps and scrolls."""
        app = _Host()
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            jobs = [self._job(name=f"job-{i:02d}") for i in range(30)]
            screen._refresh_cron(self._snap(jobs), self._status("running"))
            table = app.screen.query_one("#cron-entries", DataTable)
            # The cap is a layout property, which settles a frame or two after
            # the rows land; under a loaded suite that can take a few frames.
            for _ in range(20):
                await pilot.pause()
                if table.size.height < 30:
                    break
        assert table.row_count == 30
        assert table.size.height < 30, "table must cap and scroll, not grow"


class TestTabBadges:
    def _status(self, name, state_str):
        from claude_on_the_fly.tui.state import FrontendStatus

        return FrontendStatus(name=name, state=state_str)

    @pytest.mark.parametrize(
        ("states", "expected"),
        [
            ({"slack": "running", "telegram": "stopped"}, "running"),
            ({"slack": "broken", "telegram": "running"}, "broken"),
            ({"slack": "stopped", "telegram": "stopped"}, "stopped"),
            ({}, "stopped"),
        ],
    )
    def test_the_worst_chat_state_wins(self, states, expected):
        """The chat tab fronts three daemons, so the badge has to draw the eye when
        any one of them is broken."""
        by_name = {name: self._status(name, st) for name, st in states.items()}
        assert DashboardScreen._chat_aggregate_state(by_name) == expected

    async def test_the_badges_reach_the_tabs(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_tab_badges(
                {
                    "cron": self._status("cron", "running"),
                    "jobs": self._status("jobs", "broken"),
                    "slack": self._status("slack", "running"),
                }
            )
            await pilot.pause()
            tabs = app.screen.query_one("#daemon-tabs", TabbedContent)
            cron_label = str(tabs.get_tab("tab-cron").label)
        assert "cron" in cron_label

    async def test_a_pre_mount_call_is_swallowed(self):
        """A refresh can land before the tabs exist."""
        DashboardScreen()._refresh_tab_badges({})


class TestChatStrip:
    def _status(self, name, state_str):
        from claude_on_the_fly.tui.state import FrontendStatus

        return FrontendStatus(name=name, state=state_str)

    async def test_a_running_job_is_listed_and_mapped_for_the_watch_pane(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._chat_selected_idx = 0
            from claude_on_the_fly.tui.state import FrontendStatus

            by_name = {
                "slack": FrontendStatus(
                    name="slack",
                    state="running",
                    extra={
                        "running_jobs": [
                            {
                                "identifier": "slack/hoss",
                                "chat_id": "C1",
                                "session_uuid": "s-1",
                                "uptime_s": 12,
                            }
                        ]
                    },
                ),
                "telegram": self._status("telegram", "stopped"),
            }
            screen._refresh_chat_strip(by_name)
            await pilot.pause()
            table = app.screen.query_one("#chat-strip", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
        assert "slack/hoss" in row
        assert screen._job_sessions
        assert screen._chat_workspaces

    async def test_an_idle_running_daemon_says_idle(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_chat_strip(
                {name: self._status(name, "running") for name in dash.CHAT_FRONTENDS}
            )
            await pilot.pause()
            table = app.screen.query_one("#chat-strip", DataTable)
        assert "idle" in str(table.get_row_at(0)[0])

    async def test_a_stopped_daemon_says_how_to_start_it(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_chat_strip(
                {name: self._status(name, "stopped") for name in dash.CHAT_FRONTENDS}
            )
            await pilot.pause()
            table = app.screen.query_one("#chat-strip", DataTable)
        assert "press r to start" in str(table.get_row_at(0)[0])


class TestStaleBanner:
    def _snap(self, frontends):
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import Snapshot

        return Snapshot(
            timestamp=datetime.now(UTC),
            frontends=frontends,
            jobs=[],
            schedule_error=None,
            jobs_queue=None,
        )

    def _frontend(self, name, *, stale):
        from claude_on_the_fly.tui.state import FrontendStatus

        return FrontendStatus(name=name, state="running", stale=stale)

    async def test_an_out_of_date_daemon_is_named_with_the_upgrade_keys(self, isolated):
        """A daemon running old code after an upgrade is the single most confusing
        state to debug, so the banner names both the daemon and the fix."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_stale_banner(
                self._snap([self._frontend("slack", stale=True)])
            )
            await pilot.pause()
            banner = str(app.screen.query_one("#stale-banner", Static).content)
        assert "slack" in banner
        assert "Shift+K" in banner

    async def test_everything_current_leaves_the_banner_empty(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_stale_banner(
                self._snap([self._frontend("slack", stale=False)])
            )
            await pilot.pause()
            banner = str(app.screen.query_one("#stale-banner", Static).content)
        assert banner.strip() == ""


class TestDaemonLogAppendPath:
    """The pane is append-only: the 1Hz tick writes the few new bytes rather than
    re-rendering a 200-line backlog every second. That means the per-daemon offset
    and buffer carry the state, and a tab switch has to replay from them."""

    def _log(self, isolated, role: str, body: str) -> Path:
        from claude_on_the_fly import logs as logs_mod

        path = isolated / logs_mod.log_name(role)
        path.write_text(body)
        return path

    async def test_only_appended_lines_are_written(self, isolated, monkeypatch):
        import os

        log = self._log(isolated, "cron", "old line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: "cron")
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            before = _pane_text(app, "#log-pane")
            log.write_text("old line\nfresh line\n")
            os.utime(log, (9999, 9999))
            screen._refresh_daemon_log(force_reload=False)
            await pilot.pause()
            after = _pane_text(app, "#log-pane")
        # The pre-open backlog stays hidden — the full history is in [l].
        assert "old line" not in before
        assert "fresh line" in after

    async def test_a_quiet_log_writes_nothing(self, isolated, monkeypatch):
        self._log(isolated, "cron", "line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: "cron")
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            before = _pane_text(app, "#log-pane")
            screen._refresh_daemon_log(force_reload=False)
            await pilot.pause()
            after = _pane_text(app, "#log-pane")
        assert after == before

    async def test_switching_back_replays_what_was_logged_while_away(
        self, isolated, monkeypatch
    ):
        """The per-daemon resume offset is the whole point: switching back blank would
        lose everything the daemon said in the meantime."""
        import os

        cron_log = self._log(isolated, "cron", "")
        self._log(isolated, "jobs", "")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            target = {"name": "cron"}
            monkeypatch.setattr(screen, "_active_daemon", lambda: target["name"])
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            # Something in the pane before leaving, so the switch-back has a buffer
            # to replay rather than only new bytes to append.
            cron_log.write_text("seen before leaving\n")
            os.utime(cron_log, (8888, 8888))
            screen._refresh_daemon_log(force_reload=False)
            await pilot.pause()

            # Away on the jobs tab while cron keeps logging.
            target["name"] = "jobs"
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            cron_log.write_text("seen before leaving\nlogged while away\n")
            os.utime(cron_log, (9999, 9999))

            target["name"] = "cron"
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#log-pane")
        assert "logged while away" in rendered
        assert "seen before leaving" in rendered, "the buffer was not replayed"

    async def test_a_reader_scrolled_up_keeps_their_place(self, isolated, monkeypatch):
        import os

        log = self._log(isolated, "cron", "")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: "cron")
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            restores: list[int] = []
            monkeypatch.setattr(dash.render, "capture_scroll", lambda _p: (False, 3))
            monkeypatch.setattr(
                dash.render,
                "restore_scroll",
                lambda _p, *, prev_y: restores.append(prev_y),
            )
            log.write_text("appended\n")
            os.utime(log, (9999, 9999))
            screen._refresh_daemon_log(force_reload=False)
            await pilot.pause()
        assert restores == [3]

    async def test_a_log_that_vanishes_between_the_check_and_the_read(
        self, isolated, monkeypatch
    ):
        class VanishingPath:
            name = "cron-host-2026-07-30.log"

            def is_file(self):
                return True

            def stat(self):
                raise OSError("stale handle")

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: "cron")
            monkeypatch.setattr(
                dash.logs, "find_log", lambda _role, directory=None: VanishingPath()
            )
            before = screen._log_path
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
        # The read failed before the path was adopted, so the pane keeps showing
        # whatever it had rather than claiming the vanished file.
        assert screen._log_path == before

    async def test_a_long_background_catch_up_is_bounded(self, isolated, monkeypatch):
        """A daemon that logged for an hour while the tab was elsewhere must not write
        its whole hour into the pane on switch-back."""
        import os

        log = self._log(isolated, "cron", "")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_active_daemon", lambda: "cron")
            screen._refresh_daemon_log(force_reload=True)
            await pilot.pause()
            log.write_text("".join(f"line {i}\n" for i in range(dash.TAIL_LINES * 3)))
            os.utime(log, (9999, 9999))
            screen._refresh_daemon_log(force_reload=False)
            await pilot.pause()
            written = _pane_text(app, "#log-pane").splitlines()
        assert len(written) <= dash.TAIL_LINES + 2


class TestRefreshNowKey:
    async def test_it_refreshes_both_the_tables_and_the_log(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            calls: list[str] = []
            monkeypatch.setattr(screen, "_refresh", lambda: calls.append("tables"))
            monkeypatch.setattr(
                screen,
                "_refresh_log",
                lambda *, force_reload=False: calls.append(f"log:{force_reload}"),
            )
            screen.action_refresh_now()
            await pilot.pause()
        assert calls == ["tables", "log:True"]

    async def test_editing_the_sandbox_config_seeds_the_commented_template(
        self, isolated, monkeypatch, no_suspend
    ):
        """The first thing an operator opens should be the file explaining every
        field, not an empty buffer whose schema they have to go and find."""
        from claude_on_the_fly import settings

        opened: list[tuple[object, object]] = []
        monkeypatch.setattr(
            dash.env_editor,
            "open_in_editor",
            lambda path, seed=None: opened.append((path, seed)),
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            _capture(screen)
            screen._edit_sandbox_config()
            await pilot.pause()

        target, seed = opened[0]
        assert target == settings.operator_settings()
        assert target.name == "config.yaml"
        # The bundled template, comments and all -- that is the point of seeding it.
        assert "egress:" in str(seed) and "permissions:" in str(seed)
        assert str(seed).count("#") > 40

    async def test_a_sandbox_edit_names_the_file_it_touched(
        self, isolated, monkeypatch, no_suspend
    ):
        """No diff afterwards, unlike .env: nothing in here is a secret to redact, and
        the loaders re-read the file per call so an edit needs no restart."""
        monkeypatch.setattr(dash.env_editor, "open_in_editor", lambda *a, **k: None)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            screen._edit_sandbox_config()
            await pilot.pause()
        assert any("config.yaml" in str(note) for note in notices)


class TestChatTakeover:
    """`t` on the chat tab copies the highlighted job's resume command.

    Same contract as the History screen's `t`: the row's own
    (workspace, session_uuid) pair, so the command resumes that exact session
    rather than whatever the current env points at.
    """

    def _wire_chat(self, screen: DashboardScreen, monkeypatch, *, uuid="s-1"):
        screen.action_show_tab("tab-chat")
        monkeypatch.setattr(screen, "_datatable_cursor_key", lambda _sel: "telegram:42")
        screen._chat_workspaces = {"telegram:42": "telegram/hoss"}
        screen._job_sessions = {"telegram:42": uuid} if uuid else {}
        monkeypatch.setattr(screen, "_active_daemon", lambda: "telegram")

    async def test_an_empty_table_says_no_row(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("no row selected" in msg for msg, _sev in notices)

    async def test_a_row_with_no_session_says_no_takeover(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch, uuid="")
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("no takeover for this row" in msg for msg, _sev in notices)

    async def test_a_resolvable_row_copies_cd_and_resume(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            backend = type(
                "B", (), {"takeover_command": lambda _s, _w, _u: "claude --resume x"}
            )()
            monkeypatch.setattr(dash, "get_backend", lambda: backend)
            copied: list[str] = []
            app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        workspace = dash.DATA_DIR / "workspaces" / "telegram/hoss"
        assert copied == [f"cd -- {shlex.quote(str(workspace))} && claude --resume x"]
        assert any(
            "copied takeover cmd for telegram/hoss" in msg for msg, _sev in notices
        )

    async def test_a_backend_with_no_session_yet_says_so(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            backend = type("B", (), {"takeover_command": lambda _s, _w, _u: None})()
            monkeypatch.setattr(dash, "get_backend", lambda: backend)
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("agent hasn't run a turn" in msg for msg, _sev in notices)

    async def test_a_backend_that_raises_is_reported(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)

            def boom(_self, _w, _u):
                raise RuntimeError("store unreadable")

            backend = type("B", (), {"takeover_command": boom})()
            monkeypatch.setattr(dash, "get_backend", lambda: backend)
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("takeover failed" in msg for msg, _sev in notices)

    async def test_a_clipboard_that_will_not_write_is_reported(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_chat(screen, monkeypatch)
            backend = type("B", (), {"takeover_command": lambda _s, _w, _u: "claude"})()
            monkeypatch.setattr(dash, "get_backend", lambda: backend)
            app.copy_to_clipboard = lambda _t: (_ for _ in ()).throw(  # type: ignore[method-assign]
                RuntimeError("no clipboard")
            )
            notices = _capture(screen)
            await pilot.press("t")
            await pilot.pause()
        assert any("clipboard write failed" in msg for msg, _sev in notices)


class TestRunNow:
    """`n` fires the highlighted cron entry via the daemon's trigger file. The
    trigger lands under the redirected DATA_DIR, so the tests assert on the file
    itself rather than on a mocked writer."""

    def _wire_cron(self, screen: DashboardScreen, monkeypatch, *, job="nightly"):
        screen.action_show_tab("tab-cron")
        monkeypatch.setattr(screen, "_selected_job", lambda: job)

    async def test_run_now_writes_a_trigger_for_the_selected_entry(
        self, isolated, monkeypatch
    ):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_cron(screen, monkeypatch)
            monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
            notices = _capture(screen)
            await pilot.press("n")
            await pilot.pause()
        assert (isolated / "state" / "cron.trigger").is_file()
        assert any("run-now requested for nightly" in msg for msg, _sev in notices)

    def test_a_tab_scoped_key_is_hidden_before_the_tabs_mount(self):
        """The Footer asks for bindings early; no tab is in front yet, so a
        tab-scoped key stays off it rather than crashing the paint."""
        screen = DashboardScreen()
        assert screen.check_action("run_now", ()) is False
        assert screen.check_action("stop", ()) is True

    async def test_run_now_is_inert_off_the_cron_tab(self, isolated, monkeypatch):
        """`n` belongs to the cron tab: check_action hides it elsewhere, and a
        hidden binding does not fire."""
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(screen, "_selected_job", lambda: "nightly")
            monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
            screen.action_show_tab("tab-chat")
            await pilot.pause()
            assert screen.check_action("run_now", ()) is False
            await pilot.press("n")
            await pilot.pause()
        assert not (isolated / "state" / "cron.trigger").exists()

    async def test_run_now_refuses_when_the_daemon_is_stopped(
        self, isolated, monkeypatch
    ):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_cron(screen, monkeypatch)
            monkeypatch.setattr(supervisor, "is_running", lambda _n: False)
            notices = _capture(screen)
            await pilot.press("n")
            await pilot.pause()
        assert not (isolated / "state" / "cron.trigger").exists()
        assert any("cron daemon not running" in msg for msg, _sev in notices)

    async def test_run_now_with_no_selection_says_so(self, isolated, monkeypatch):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen.action_show_tab("tab-cron")
            await pilot.pause()
            monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
            notices = _capture(screen)
            await pilot.press("n")
            await pilot.pause()
        assert not (isolated / "state" / "cron.trigger").exists()
        assert any("no cron entry selected" in msg for msg, _sev in notices)

    async def test_run_now_is_gated_by_the_busy_spinner(self, isolated, monkeypatch):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_cron(screen, monkeypatch)
            screen._busy_msg = "stopping cron"
            await pilot.press("n")
            await pilot.pause()
        assert not (isolated / "state" / "cron.trigger").exists()

    async def test_a_failing_trigger_write_is_reported(self, isolated, monkeypatch):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", isolated)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_cron(screen, monkeypatch)
            monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
            monkeypatch.setattr(
                dash,
                "request_run_now",
                lambda _name: (_ for _ in ()).throw(
                    RuntimeError("state dir unwritable")
                ),
            )
            notices = _capture(screen)
            await pilot.press("n")
            await pilot.pause()
        assert not (isolated / "state" / "cron.trigger").exists()
        assert any("run-now failed" in msg for msg, _sev in notices)


class TestWatchJobs:
    """The jobs tab's watch pane: the worker publishes the running job's
    session uuid in its heartbeat, so the highlighted job's live agent
    conversation is tailed like a chat job's."""

    def _wire_jobs(self, screen: DashboardScreen, monkeypatch, isolated, *, uuid="s-1"):
        screen.action_show_tab("tab-jobs")
        monkeypatch.setattr(screen, "_datatable_cursor_key", lambda _sel: "t1-abc")
        screen._chat_workspaces = {"jobs:t1-abc": "t1-abc"}
        screen._job_workspaces = {"jobs:t1-abc": isolated}
        screen._job_sessions = {"jobs:t1-abc": uuid} if uuid else {}
        monkeypatch.setattr(screen, "_active_daemon", lambda: "jobs")

    async def test_a_running_job_with_a_session_is_watched(self, isolated, monkeypatch):
        log = _write_session(isolated / "session.jsonl", "hello from the job agent")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_jobs(screen, monkeypatch, isolated)
            monkeypatch.setattr(dash, "resolve_session_log", lambda _w, _u: log)
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "hello from the job agent" in rendered

    async def test_a_running_job_without_a_session_says_pending(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            self._wire_jobs(screen, monkeypatch, isolated, uuid="")
            await pilot.pause()
            screen._refresh_watch_pane(force_reload=True)
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
        assert "no session uuid" in rendered

    async def test_refresh_jobs_builds_the_watch_maps_from_the_heartbeat(
        self, isolated
    ):
        """The worker's heartbeat `running_jobs` is what resolves a row to
        its live session."""
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import FrontendStatus, Snapshot

        status = FrontendStatus(
            name="jobs",
            state="running",
            extra={
                "running_jobs": [
                    {
                        "job_id": "t1-abc",
                        "key": "k1",
                        "workspace": str(isolated / "workspaces" / "jobs" / "abc"),
                        "uptime_s": 3,
                        "session_uuid": "s-1",
                    },
                    # A row without a job id cannot be keyed; it is skipped.
                    {"key": "k2", "workspace": "/tmp/x", "session_uuid": "s-2"},
                ]
            },
        )
        snap = Snapshot(
            timestamp=datetime.now(UTC),
            frontends=[status],
            jobs=[],
            schedule_error=None,
            jobs_queue=None,
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(snap, status)
            await pilot.pause()
        assert screen._job_sessions == {"jobs:t1-abc": "s-1"}
        assert screen._job_workspaces == {
            "jobs:t1-abc": isolated / "workspaces" / "jobs" / "abc"
        }
        # The display label is the short id, the same one the table shows.
        assert screen._chat_workspaces == {"jobs:t1-abc": "abc"}


class TestUpgradeAction:
    """[U] is the destructive one: it stops every daemon at once. So the modal
    has to come first, and only a confirmed run may touch anything."""

    def _plan(self):
        from claude_on_the_fly.upgrade import Plan

        return Plan(command="git pull && uv sync", source="test")

    async def test_it_asks_before_it_stops_anything(self, isolated, monkeypatch):
        from claude_on_the_fly.tui.screens.upgrade import UpgradeScreen

        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            monkeypatch.setattr(dash.upgrade, "resolve", self._plan)
            monkeypatch.setattr(supervisor, "all_pending_work", lambda: [])
            stopped: list[int] = []
            monkeypatch.setattr(
                supervisor, "stop_all", lambda: (stopped.append(1), [])[1]
            )

            await screen.action_upgrade()
            await pilot.pause()

            assert isinstance(app.screen, UpgradeScreen)
            assert stopped == []

    async def test_an_unknown_install_says_so_and_stops_nothing(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)

            def _refuse():
                raise dash.upgrade.UnknownInstall("set upgrade.command")

            monkeypatch.setattr(dash.upgrade, "resolve", _refuse)
            await screen.action_upgrade()
            await pilot.pause()

        assert any("set upgrade.command" in msg for msg, _s in notices)

    async def test_upgrading_while_busy_is_ignored(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            calls: list[int] = []
            monkeypatch.setattr(
                dash.upgrade, "resolve", lambda: (calls.append(1), self._plan())[1]
            )
            screen._set_busy("busy")
            await screen.action_upgrade()
            await pilot.pause()
        assert calls == []

    async def test_declining_the_modal_runs_nothing(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            ran: list[int] = []
            monkeypatch.setattr(
                dash.upgrade,
                "run_captured",
                lambda _plan: (ran.append(1), (0, ""))[1],
            )
            screen._on_upgrade_confirmed(self._plan(), False)
            await pilot.pause()
        assert ran == []

    async def test_confirming_the_modal_starts_the_upgrade(self, isolated, monkeypatch):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            ran: list[str] = []

            async def _fake_upgrade(plan):
                ran.append(plan.command)

            monkeypatch.setattr(screen, "_upgrade", _fake_upgrade)
            screen._on_upgrade_confirmed(self._plan(), True)
            await pilot.pause()

        assert ran == ["git pull && uv sync"]

    async def test_a_successful_upgrade_resumes_then_hands_over(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            order: list[str] = []
            monkeypatch.setattr(
                supervisor,
                "stop_all",
                lambda: (order.append("stop"), [("slack", 1)])[1],
            )
            monkeypatch.setattr(
                dash.upgrade,
                "run_captured",
                lambda _plan: (order.append("run"), (0, "Updated 1 file"))[1],
            )
            monkeypatch.setattr(
                supervisor, "resume", lambda: (order.append("resume"), [])[1]
            )

            await screen._upgrade(self._plan())
            await pilot.pause()

            assert order == ["stop", "run", "resume"]
            assert app.relaunch_on_exit is True
        log = isolated / dash.logs.log_name("upgrade")
        assert "Updated 1 file" in log.read_text()

    async def test_a_failed_upgrade_restarts_the_daemons_and_does_not_hand_over(
        self, isolated, monkeypatch
    ):
        """Old code running beats nothing running, and there is no new code to
        show, so the TUI stays where it is."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(supervisor, "stop_all", lambda: [])
            monkeypatch.setattr(
                dash.upgrade, "run_captured", lambda _plan: (2, "fatal: no upstream")
            )
            resumed: list[int] = []
            monkeypatch.setattr(
                supervisor, "resume", lambda: (resumed.append(1), [])[1]
            )

            await screen._upgrade(self._plan())
            await pilot.pause()

        assert resumed == [1]
        # Never set, so the host app never grew the attribute at all.
        assert getattr(app, "relaunch_on_exit", False) is False
        assert any("exit 2" in msg and severity == "error" for msg, severity in notices)

    async def test_a_daemon_that_will_not_come_back_is_named(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(supervisor, "stop_all", lambda: [])
            monkeypatch.setattr(dash.upgrade, "run_captured", lambda _plan: (0, ""))
            monkeypatch.setattr(
                supervisor,
                "resume",
                lambda: [("slack", None, RuntimeError("no token"))],
            )

            await screen._upgrade(self._plan())
            await pilot.pause()

        assert any("no token" in msg for msg, _s in notices)

    async def test_an_unwritable_log_does_not_lose_the_upgrade(
        self, isolated, monkeypatch
    ):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            notices = _capture(screen)
            monkeypatch.setattr(dash, "LOG_DIR", isolated / "nope" / "deeper")
            monkeypatch.setattr(Path, "mkdir", _raise_oserror)

            screen._write_upgrade_log(self._plan(), "output")

        assert any("could not write" in msg for msg, _s in notices)


class TestTheWatchPaneRendersRemoteTextAsData:
    """The label is a workspace name, and on the trusted-bot path that carries a
    Slack `username` the poster chooses. Both widgets have markup enabled, so an
    unescaped `[/something]` is a closing tag: it either eats the rest of the
    line or raises MarkupError and takes the TUI down with it. Every log and tail
    path already goes through `session_format._safe`; this one did not.
    """

    class _Recorder:
        """Stands in for the header Static and the watch RichLog."""

        def __init__(self) -> None:
            self.written: list[str] = []

        def update(self, value) -> None:
            self.written.append(str(value))

        def clear(self) -> None:
            self.written.clear()

        def write(self, value) -> None:
            self.written.append(str(value))

    def _drive(self, screen: DashboardScreen, label: str) -> str:
        screen._chat_workspaces = {"telegram:7": label}
        screen._job_sessions = {}
        screen._job_workspaces = {}
        header, pane = self._Recorder(), self._Recorder()
        screen._refresh_watch_session("telegram", "7", header, pane, True)
        return "\n".join(header.written + pane.written)

    async def test_markup_in_a_workspace_name_stays_literal(self, isolated):
        label = "telegram/[/bold]evil[blink]"
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            rendered = self._drive(screen, label)

        # Parsed the way the widget parses it: the name has to survive whole.
        assert label in render_markup(rendered).plain

    async def test_a_bracketed_path_does_not_raise(self, isolated):
        """The shape that already took the TUI down once, from a PostCompact
        notice: `[/Users/…/thing]` is not a tag Rich knows."""
        label = "telegram/[/Users/somebody/thing]"
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            rendered = self._drive(screen, label)

        assert label in render_markup(rendered).plain


class TestWatchGrid:
    """The watch pane prefers the agent's own terminal when the run is hosted."""

    @staticmethod
    def _call(screen, workspace):
        from textual.widgets import RichLog, Static

        return screen._refresh_watch_grid(
            "telegram",
            "12345",
            workspace,
            "telegram/H",
            screen.query_one("#watch-header", Static),
            screen.query_one("#watch-pane", RichLog),
        )

    async def test_a_hosted_run_renders_its_pane_instead_of_the_transcript(
        self, isolated, monkeypatch
    ):
        from claude_on_the_fly import tmux as tmux_mod

        pane = tmux_mod.Pane(session="cotf-pty-telegram-12345-abcd")
        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: True)
        monkeypatch.setattr(tmux_mod, "pane_named", lambda *_prefixes: pane)
        monkeypatch.setattr(
            tmux_mod, "capture", lambda *_a, **_k: "\x1b[32mrunning tests\x1b[0m"
        )
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._watch_path = isolated / "stale.jsonl"
            handled = self._call(screen, isolated / "workspaces" / "telegram" / "H")
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")
            header = str(app.screen.query_one("#watch-header", Static).content)

        assert handled is True
        assert "running tests" in rendered
        assert "live pane" in header
        # Cleared, or a later switch back to the transcript would refuse to reload.
        assert screen._watch_path is None

    async def test_the_watch_pane_itself_prefers_a_live_pane(
        self, isolated, monkeypatch
    ):
        """Through the real refresh path, not the helper: the tail must not also
        run and overwrite the grid."""
        from claude_on_the_fly import tmux as tmux_mod

        pane = tmux_mod.Pane(session="cotf-pty-telegram-777-abcd")
        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: True)
        monkeypatch.setattr(tmux_mod, "pane_named", lambda *_prefixes: pane)
        monkeypatch.setattr(tmux_mod, "capture", lambda *_a, **_k: "live grid text")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            from textual.widgets import RichLog, Static

            screen._refresh_watch_session(
                "telegram",
                "777",
                screen.query_one("#watch-header", Static),
                screen.query_one("#watch-pane", RichLog),
                True,
            )
            await pilot.pause()
            rendered = _pane_text(app, "#watch-pane")

        assert "live grid text" in rendered

    async def test_an_unhosted_run_falls_back_to_the_transcript_tail(
        self, isolated, monkeypatch
    ):
        from claude_on_the_fly import tmux as tmux_mod

        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: True)
        monkeypatch.setattr(tmux_mod, "pane_named", lambda *_prefixes: None)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert self._call(screen, isolated / "ws") is False

    async def test_hosting_unavailable_never_probes_for_a_pane(
        self, isolated, monkeypatch
    ):
        from claude_on_the_fly import tmux as tmux_mod

        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: False)

        def fail(*_prefixes):
            raise AssertionError("probed for a pane with hosting unavailable")

        monkeypatch.setattr(tmux_mod, "pane_named", fail)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert self._call(screen, isolated / "ws") is False

    async def test_a_pane_that_ends_mid_capture_falls_back(self, isolated, monkeypatch):
        from claude_on_the_fly import tmux as tmux_mod

        pane = tmux_mod.Pane(session="cotf-job-run1")
        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: True)
        monkeypatch.setattr(tmux_mod, "pane_named", lambda *_prefixes: pane)
        monkeypatch.setattr(tmux_mod, "capture", lambda *_a, **_k: None)
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            assert self._call(screen, isolated / "ws") is False


class TestWatchGridBlank:
    async def test_a_pane_that_has_not_drawn_yet_falls_back_to_the_transcript(
        self, isolated, monkeypatch
    ):
        """A live pane trims to "" until the agent draws. Rendering that would
        blank the watch pane for the opening stretch of every turn."""
        from textual.widgets import RichLog, Static

        from claude_on_the_fly import tmux as tmux_mod

        pane = tmux_mod.Pane(session="cotf-job-run1")
        monkeypatch.setattr(tmux_mod, "hosting_available", lambda: True)
        monkeypatch.setattr(tmux_mod, "pane_named", lambda *_prefixes: pane)
        monkeypatch.setattr(tmux_mod, "capture", lambda *_a, **_k: "   \n  \n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            handled = screen._refresh_watch_grid(
                "telegram",
                "12345",
                isolated / "ws",
                "telegram/H",
                screen.query_one("#watch-header", Static),
                screen.query_one("#watch-pane", RichLog),
            )

        assert handled is False
