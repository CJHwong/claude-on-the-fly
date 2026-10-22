"""What an operator is told when a daemon refuses to start.

`supervisor.spawn` already captures the cause, the log path, and the last 25
lines. The question these cover is whether any of that survives the trip to a
human: a toast that says only "spawn timed out" sends someone to a log file by
hand, and a CLI that catches nothing ends in a traceback that says less than the
exception it swallowed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App
from textual.widgets import RichLog

from claude_on_the_fly.tui import app as cli_app
from claude_on_the_fly.tui import supervisor
from claude_on_the_fly.tui.screens import logs as logs_screen
from claude_on_the_fly.tui.screens.logs import LogsScreen


def _timeout(tmp_path: Path, *, exited: bool, rc: int | None = None) -> Exception:
    capture = tmp_path / "jobs-host-2026-09-03.stdout"
    capture.write_text("Traceback (most recent call last):\nValueError: boom\n")
    return supervisor.SpawnTimeout(
        "jobs",
        4242,
        capture,
        log_tail="ValueError: boom",
        exited=exited,
        returncode=rc,
    )


class TestCause:
    """A crash and a hang send an operator to different places, so the wording
    has to tell them apart before anything else does."""

    def test_a_crash_names_its_return_code(self, tmp_path: Path) -> None:
        exc = _timeout(tmp_path, exited=True, rc=1)
        assert exc.cause == "exited (rc=1) before heartbeat"

    def test_a_hang_says_it_never_heartbeat(self, tmp_path: Path) -> None:
        exc = _timeout(tmp_path, exited=False)
        assert exc.cause == "did not heartbeat within timeout"

    def test_the_message_still_carries_the_path_and_the_tail(
        self, tmp_path: Path
    ) -> None:
        """`str(exc)` is the whole record. A caller with room for it, like the
        CLI, should not have to reassemble it from the fields."""
        exc = _timeout(tmp_path, exited=True, rc=1)
        text = str(exc)
        assert "exited (rc=1) before heartbeat" in text
        assert "pid 4242" in text
        assert ".stdout" in text
        assert "ValueError: boom" in text


class TestTheCliPrintsItInsteadOfTracebacking:
    def test_start_prints_the_record_and_exits_two(
        self, tmp_path: Path, capsys
    ) -> None:
        exc = _timeout(tmp_path, exited=True, rc=1)
        with patch.object(cli_app.supervisor, "spawn", side_effect=exc):
            assert cli_app.cmd_start("jobs", None) == 2
        err = capsys.readouterr().err
        assert "exited (rc=1) before heartbeat" in err
        assert "ValueError: boom" in err

    def test_restart_does_the_same(self, tmp_path: Path, capsys) -> None:
        """Two entry points, one exception. A restart that tracebacks while a
        start prints cleanly is the inconsistency worth removing."""
        exc = _timeout(tmp_path, exited=False)
        with (
            patch.object(cli_app, "_report_one"),
            patch.object(cli_app.supervisor, "restart", side_effect=exc),
        ):
            assert cli_app.cmd_restart("jobs", None, force=False) == 2
        assert "did not heartbeat within timeout" in capsys.readouterr().err


class _Host(App):
    CSS = """
    #overlay-box { height: 1fr; }
    #logs-sidebar, #logs-main { height: 1fr; }
    #logs-list, #logs-content { height: 1fr; min-height: 4; }
    """


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(logs_screen, "LOG_DIR", directory)
    return directory


class TestTheLogsScreenOpensOnTheNamedFile:
    async def test_a_named_file_wins_over_the_newest(self, log_dir) -> None:
        newest = log_dir / "slack-host-2026-09-03.log"
        newest.write_text("unrelated slack chatter\n")
        wanted = log_dir / "jobs-host-2026-09-02.log"
        wanted.write_text("the traceback that matters\n")
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(LogsScreen(preselect=wanted))
            await pilot.pause()
            content = app.screen.query_one("#logs-content", RichLog)
            rendered = "\n".join(
                seg.text for line in content.lines for seg in line._segments
            )
        assert "the traceback that matters" in rendered

    async def test_a_stdout_capture_is_shown_though_the_listing_excludes_it(
        self, log_dir
    ) -> None:
        """The one file worth opening after a refused start is the `.stdout`
        capture, and `_available_logs` filters those out on purpose. A preselect
        that silently fell back to the newest `.log` would land the operator on
        an unrelated daemon's file and look like it worked."""
        (log_dir / "slack-host-2026-09-03.log").write_text("unrelated\n")
        capture = log_dir / "jobs-host-2026-09-03.stdout"
        capture.write_text("ValueError: boom\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen(preselect=capture)
            await app.push_screen(screen)
            await pilot.pause()
            content = app.screen.query_one("#logs-content", RichLog)
            rendered = "\n".join(
                seg.text for line in content.lines for seg in line._segments
            )
        assert screen._selected == capture
        assert "ValueError: boom" in rendered

    async def test_a_preselect_that_no_longer_exists_falls_back(self, log_dir) -> None:
        """Log retention can remove it between the failure and the keypress.
        Showing the newest beats showing an empty pane."""
        (log_dir / "slack-host-2026-09-03.log").write_text("still here\n")
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen(preselect=log_dir / "swept-away.stdout")
            await app.push_screen(screen)
            await pilot.pause()
        assert screen._selected is not None
        assert screen._selected.name == "slack-host-2026-09-03.log"

    async def test_a_bare_open_still_takes_the_newest(self, log_dir) -> None:
        older = log_dir / "a-host-2026-09-01.log"
        older.write_text("old\n")
        newer = log_dir / "b-host-2026-09-03.log"
        newer.write_text("new\n")
        import os

        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        app = _Host()
        async with app.run_test() as pilot:
            screen = LogsScreen()
            await app.push_screen(screen)
            await pilot.pause()
        assert screen._selected == newer
