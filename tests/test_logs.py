"""Log naming, rollover, console auto-detect, and retention."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from claude_on_the_fly import agent, logs


@pytest.fixture
def log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `logs.log_dir()` at a tmp tree and pin the host tag."""
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.setenv("COTF_HOST_TAG", "testbox")
    return tmp_path / "logs"


@pytest.fixture
def restore_root_handlers():
    """`configure` replaces the root handlers wholesale; put them back."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield root
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    for handler in before:
        root.addHandler(handler)
    root.setLevel(level)


class TestHostTag:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "laptop")
        assert logs.host_tag() == "laptop"

    def test_dashes_collapse_to_underscore(self, monkeypatch):
        # A dash in the host would make `<role>-<host>-<date>` ambiguous to
        # parse from the right, which is what `parse_log_name` relies on.
        monkeypatch.setenv("COTF_HOST_TAG", "Hoss-MacBook-Pro")
        assert logs.host_tag() == "Hoss_MacBook_Pro"

    def test_falls_back_when_unresolvable(self, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "...")
        assert logs.host_tag() == "unknown"


class TestNaming:
    def test_name_carries_role_host_and_day(self, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        name = logs.log_name("slack", day=date(2026, 7, 28))
        assert name == "slack-testbox-2026-07-28.log"

    def test_two_roles_never_share_a_path(self, log_root):
        assert logs.log_file("slack") != logs.log_file("telegram")

    def test_two_hosts_never_share_a_path(self, log_root, monkeypatch):
        mine = logs.log_file("slack")
        monkeypatch.setenv("COTF_HOST_TAG", "otherbox")
        assert logs.log_file("slack") != mine

    def test_parse_round_trips(self, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        path = Path("/x") / logs.log_name("slack", day=date(2026, 7, 28))
        assert logs.parse_log_name(path) == ("slack", "testbox", "2026-07-28")

    def test_parse_handles_a_role_containing_dashes(self, monkeypatch):
        """A scheduled job's role is `schedule-<job>`, and the job name may have
        dashes of its own."""
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        path = Path("/x") / logs.log_name("schedule-fms-shadow", day=date(2026, 7, 28))
        assert logs.parse_log_name(path) == (
            "schedule-fms-shadow",
            "testbox",
            "2026-07-28",
        )

    def test_parse_rejects_a_legacy_flat_name(self):
        """Retention must leave a pre-rename `slack.log` alone rather than
        deleting a file it cannot date."""
        assert logs.parse_log_name(Path("/x/slack.log")) is None
        assert logs.parse_log_name(Path("/x/slack.log.2026-07-24")) is None

    def test_stdout_captures_are_recognised(self, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        name = logs.log_name("slack", day=date(2026, 7, 28), suffix=".stdout")
        assert logs.parse_log_name(Path("/x") / name) == (
            "slack",
            "testbox",
            "2026-07-28",
        )


class TestConfigure:
    def test_no_console_handler_when_stderr_is_not_a_tty(
        self, log_root, restore_root_handlers, monkeypatch
    ):
        """The duplicate-write bug: `supervisor.spawn` redirects a daemon's
        stderr to a file, so a console handler wrote the whole log a second time
        into `<role>.stdout`."""

        class _NotATty:
            def isatty(self) -> bool:
                return False

        monkeypatch.setattr("sys.stderr", _NotATty())
        logs.configure("slack")

        streams = [
            h
            for h in restore_root_handlers.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert streams == []
        assert any(
            isinstance(h, logs.DailyRoleFileHandler)
            for h in restore_root_handlers.handlers
        )

    def test_console_handler_when_stderr_is_a_tty(
        self, log_root, restore_root_handlers, monkeypatch
    ):
        class _Tty:
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr("sys.stderr", _Tty())
        logs.configure("slack")

        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in restore_root_handlers.handlers
        )

    def test_console_false_wins_over_a_tty(
        self, log_root, restore_root_handlers, monkeypatch
    ):
        class _Tty:
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr("sys.stderr", _Tty())
        logs.configure("slack", console=False)

        assert not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in restore_root_handlers.handlers
        )

    def test_replaces_handlers_so_a_prior_basicconfig_cannot_double_up(
        self, log_root, restore_root_handlers
    ):
        logging.basicConfig()
        logs.configure("slack", console=False)
        assert len(restore_root_handlers.handlers) == 1

    def test_writes_to_the_role_host_day_file(self, log_root, restore_root_handlers):
        logs.configure("slack", console=False)
        logging.getLogger("t").warning("hello")
        assert (log_root / logs.log_name("slack")).read_text().endswith("hello\n")

    def test_a_process_that_logs_nothing_leaves_no_file(
        self, log_root, restore_root_handlers
    ):
        logs.configure("slack", console=False)
        assert not (log_root / logs.log_name("slack")).exists()


class TestRollover:
    def test_a_new_day_opens_the_next_file_and_never_renames(
        self, log_root, restore_root_handlers, monkeypatch
    ):
        log_root.mkdir(parents=True)
        handler = logs.DailyRoleFileHandler("slack")
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "day one", (), None)
        handler.emit(record)
        first = log_root / logs.log_name("slack")
        assert first.is_file()

        tomorrow = date.today() + timedelta(days=1)
        monkeypatch.setattr(logs, "date", _FrozenDate(tomorrow))
        handler.emit(
            logging.LogRecord("t", logging.INFO, __file__, 1, "day two", (), None)
        )
        handler.close()

        second = log_root / logs.log_name("slack", day=tomorrow)
        # Yesterday's file keeps its name and its content: a rollover is the next
        # open(), so a syncer never sees whole files move.
        assert first.read_text().strip() == "day one"
        assert second.read_text().strip() == "day two"


class _FrozenDate(date):
    """Stand-in for `date` whose `today()` is pinned."""

    _pinned: date

    def __new__(cls, pinned: date):
        obj = super().__new__(cls, pinned.year, pinned.month, pinned.day)
        obj._pinned = pinned
        return obj

    def today(self):  # type: ignore[override]
        return self._pinned


class TestFindLog:
    def test_prefers_this_host(self, log_root, monkeypatch):
        log_root.mkdir(parents=True)
        mine = log_root / logs.log_name("slack")
        mine.write_text("mine")
        monkeypatch.setenv("COTF_HOST_TAG", "otherbox")
        (log_root / logs.log_name("slack")).write_text("theirs")
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        assert logs.find_log("slack") == mine

    def test_falls_back_to_another_host(self, log_root, monkeypatch):
        log_root.mkdir(parents=True)
        monkeypatch.setenv("COTF_HOST_TAG", "otherbox")
        theirs = log_root / logs.log_name("slack")
        theirs.write_text("theirs")
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        assert logs.find_log("slack") == theirs

    def test_picks_the_newest_day(self, log_root):
        log_root.mkdir(parents=True)
        old = log_root / logs.log_name("slack", day=date(2026, 7, 1))
        new = log_root / logs.log_name("slack", day=date(2026, 7, 20))
        old.write_text("old")
        new.write_text("new")
        assert logs.find_log("slack") == new

    def test_returns_todays_path_when_nothing_written_yet(self, log_root):
        """The reader still needs a filename to show while it waits, so this
        never returns None."""
        assert logs.find_log("slack") == log_root / logs.log_name("slack")

    def test_ignores_other_roles_and_stdout_captures(self, log_root):
        log_root.mkdir(parents=True)
        (log_root / logs.log_name("telegram")).write_text("x")
        (log_root / logs.log_name("slack", suffix=".stdout")).write_text("x")
        assert logs.find_log("slack") == log_root / logs.log_name("slack")


class TestPrune:
    def _write(self, root: Path, role: str, day: date, suffix: str = ".log") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / logs.log_name(role, day=day, suffix=suffix)
        path.write_text("x")
        return path

    def test_removes_files_past_the_window(self, log_root):
        stale = self._write(log_root, "slack", date.today() - timedelta(days=30))
        fresh = self._write(log_root, "slack", date.today())
        removed = logs.prune()
        assert removed == [stale]
        assert not stale.exists()
        assert fresh.exists()

    def test_prunes_every_host_not_just_this_one(self, log_root, monkeypatch):
        monkeypatch.setenv("COTF_HOST_TAG", "otherbox")
        theirs = self._write(log_root, "slack", date.today() - timedelta(days=30))
        monkeypatch.setenv("COTF_HOST_TAG", "testbox")
        assert logs.prune() == [theirs]

    def test_leaves_unrecognised_files_alone(self, log_root):
        log_root.mkdir(parents=True)
        legacy = log_root / "slack.log"
        legacy.write_text("x")
        notes = log_root / "my-notes.txt"
        notes.write_text("x")
        assert logs.prune() == []
        assert legacy.exists()
        assert notes.exists()

    def test_keeps_the_newest_stdout_capture_however_old(self, log_root):
        """A capture is opened once at spawn and backs the daemon's stderr for
        its whole life; deleting it reclaims nothing and loses a fatal
        traceback."""
        held = self._write(
            log_root, "slack", date.today() - timedelta(days=90), suffix=".stdout"
        )
        superseded = self._write(
            log_root, "slack", date.today() - timedelta(days=120), suffix=".stdout"
        )
        removed = logs.prune()
        assert removed == [superseded]
        assert held.exists()

    def test_zero_keep_days_disables_pruning(self, log_root, monkeypatch):
        stale = self._write(log_root, "slack", date.today() - timedelta(days=99))
        monkeypatch.setenv("COTF_LOG_KEEP_DAYS", "0")
        assert logs.prune() == []
        assert stale.exists()

    def test_unparseable_keep_days_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("COTF_LOG_KEEP_DAYS", "not-a-number")
        assert logs.keep_days() == logs.DEFAULT_KEEP_DAYS
