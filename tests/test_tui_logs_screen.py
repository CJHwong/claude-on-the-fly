"""The logs browser screen: which files it lists, and when it re-reads them.

Two behaviours carry the weight. The list is newest-first because the file an
operator wants is almost always the one being written right now, and the tail only
re-reads on an mtime change — otherwise a quiet daemon costs a full-file read every
second for the life of the screen.
"""

from __future__ import annotations

import os
import time

import pytest
from textual.app import App
from textual.widgets import ListView, RichLog

from claude_on_the_fly.tui.screens import logs as logs_screen
from claude_on_the_fly.tui.screens.logs import LogsScreen


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(logs_screen, "LOG_DIR", directory)
    return directory


class _Host(App):
    CSS = """
    #overlay-box { height: 1fr; }
    #logs-sidebar, #logs-main { height: 1fr; }
    #logs-list, #logs-content { height: 1fr; min-height: 4; }
    """


class TestAvailableLogs:
    def test_a_missing_directory_lists_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs_screen, "LOG_DIR", tmp_path / "never-created")
        assert logs_screen._available_logs() == []

    def test_only_log_files_are_listed(self, log_dir):
        """`.stdout` captures and the state dir's JSON are not logs, and listing them
        would put unreadable binary in the pane."""
        (log_dir / "slack-host-2026-07-30.log").write_text("a")
        (log_dir / "slack-host-2026-07-30.stdout").write_text("b")
        (log_dir / "notes.txt").write_text("c")
        (log_dir / "subdir").mkdir()
        assert [p.name for p in logs_screen._available_logs()] == [
            "slack-host-2026-07-30.log"
        ]

    def test_the_newest_file_comes_first(self, log_dir):
        older = log_dir / "slack-host-2026-07-29.log"
        older.write_text("old")
        newer = log_dir / "slack-host-2026-07-30.log"
        newer.write_text("new")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        assert [p.name for p in logs_screen._available_logs()] == [
            newer.name,
            older.name,
        ]


class TestScreenBehaviour:
    async def test_the_newest_log_is_selected_and_rendered_on_open(self, log_dir):
        (log_dir / "slack-host-2026-07-30.log").write_text("first line\nsecond line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            content = app.screen.query_one("#logs-content", RichLog)
            rendered = "\n".join(
                seg.text for line in content.lines for seg in line._segments
            )
        assert "second line" in rendered
        assert screen._selected is not None

    async def test_an_empty_directory_renders_nothing_and_does_not_crash(self, log_dir):
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert app.screen.query_one("#logs-list", ListView).index in (None, 0)
        assert screen._selected is None

    async def test_highlighting_another_file_switches_the_pane(self, log_dir):
        first = log_dir / "slack-host-2026-07-30.log"
        first.write_text("from slack\n")
        second = log_dir / "cron-host-2026-07-30.log"
        second.write_text("from cron\n")
        os.utime(first, (2000, 2000))
        os.utime(second, (1000, 1000))

        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            view = app.screen.query_one("#logs-list", ListView)
            view.index = 1
            await pilot.pause()
            content = app.screen.query_one("#logs-content", RichLog)
            rendered = "\n".join(
                seg.text for line in content.lines for seg in line._segments
            )
        assert screen._selected == second
        assert "from cron" in rendered

    async def test_highlighting_out_of_range_is_ignored(self, log_dir):
        """The list and `_files` are refreshed separately, so an index can briefly
        point past the end."""
        (log_dir / "slack-host-2026-07-30.log").write_text("x\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            before = screen._selected
            screen._files = []
            view = app.screen.query_one("#logs-list", ListView)
            screen.on_list_view_highlighted(ListView.Highlighted(view, None))
            await pilot.pause()
        assert screen._selected == before

    async def test_a_quiet_file_is_not_re_read(self, log_dir):
        """The whole point of the mtime check: a full-file read every second for a
        daemon that is not logging."""
        path = log_dir / "slack-host-2026-07-30.log"
        path.write_text("line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            renders = {"n": 0}
            real_render = screen._render_selected_full
            screen._render_selected_full = lambda: (  # type: ignore[method-assign]
                renders.__setitem__("n", renders["n"] + 1),
                real_render(),
            )[1]
            screen._tail_selected_if_changed()
            screen._tail_selected_if_changed()
        assert renders["n"] == 0

    async def test_an_appended_file_is_re_read(self, log_dir):
        path = log_dir / "slack-host-2026-07-30.log"
        path.write_text("line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            time.sleep(0.01)
            path.write_text("line\nappended\n")
            os.utime(path, (9999, 9999))
            screen._tail_selected_if_changed()
            await pilot.pause()
            content = app.screen.query_one("#logs-content", RichLog)
            rendered = "\n".join(
                seg.text for line in content.lines for seg in line._segments
            )
        assert "appended" in rendered

    async def test_a_file_that_vanishes_stops_tailing_quietly(self, log_dir):
        """The daily rollover deletes nothing, but retention does, and a pruned file
        must not raise into the 1Hz timer."""
        path = log_dir / "slack-host-2026-07-30.log"
        path.write_text("line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            path.unlink()
            screen._tail_selected_if_changed()
            await pilot.pause()

    async def test_rendering_a_vanished_file_leaves_no_stale_mtime(self, log_dir):
        """A stale mtime would make the next tick think the file is unchanged and
        never pick the new one up."""
        path = log_dir / "slack-host-2026-07-30.log"
        path.write_text("line\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            path.unlink()
            screen._render_selected_full()
            await pilot.pause()
        assert screen._rendered_mtime is None

    async def test_tailing_with_nothing_selected_is_a_no_op(self, log_dir):
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen._selected = None
            screen._tail_selected_if_changed()
            screen._render_selected_full()
            await pilot.pause()

    async def test_refresh_rescans_the_directory(self, log_dir):
        """A log created after the screen opened, which is the common case for a
        daemon started from the dashboard."""
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert screen._files == []
            (log_dir / "slack-host-2026-07-30.log").write_text("new daemon\n")
            await pilot.press("r")
            await pilot.pause()
        assert [p.name for p in screen._files] == ["slack-host-2026-07-30.log"]
