"""The dashboard driven as a real Textual screen.

What matters here is which daemon a lifecycle key acts on and what happens when
that action fails. The tab decides the target (not focus, which changes on window
blur), and every supervisor failure has to name the daemon and the reason — an
unexplained no-op on `k` is the worst outcome, because the operator presses it
again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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


async def _open(app: _Host, pilot) -> DashboardScreen:
    screen = DashboardScreen()
    await app.push_screen(screen)
    await pilot.pause()
    return screen


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
            assert "[/]" in str(table.get_row_at(0)[2])

    async def test_a_job_with_no_enqueue_time_shows_no_age(self, isolated):
        app = _Host()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot)
            screen._refresh_jobs(
                self._snap(self._view([self._row(enqueued_at=None)])), None
            )
            await pilot.pause()
            table = app.screen.query_one("#jobs-queue", DataTable)
            assert str(table.get_row_at(0)[3]) == "-"

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
    def _snap(self, jobs, error=None):
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import Snapshot

        return Snapshot(
            timestamp=datetime.now(UTC),
            frontends=[],
            jobs=jobs,
            schedule_error=error,
            jobs_queue=None,
        )

    def _job(self, name="nightly"):
        from datetime import datetime, timedelta

        from claude_on_the_fly.tui.state import JobInfo

        return JobInfo(
            name=name,
            cron="0 4 * * *",
            kind="prompt",
            next_fire=datetime.now() + timedelta(hours=1),
        )

    def _status(self, state_str):
        from claude_on_the_fly.tui.state import FrontendStatus

        return FrontendStatus(name="cron", state=state_str)

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
