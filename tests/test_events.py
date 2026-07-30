"""EventLog: an append-only audit trail that must never take a daemon down.

Every failure path here logs and returns. The log is a record of what happened,
not part of doing it, so a full disk or a permissions problem costs the record and
nothing else.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_on_the_fly.events import EventLog


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


class TestAppend:
    def test_a_record_round_trips(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("job_queued", source="slack", identifier="C1", prompt="do it")
        record = json.loads((tmp_path / "events.jsonl").read_text().strip())
        assert record["type"] == "job_queued"
        assert record["source"] == "slack"
        assert record["identifier"] == "C1"
        assert record["prompt"] == "do it"
        assert record["ts"]

    def test_none_valued_extras_are_dropped(self, tmp_path: Path) -> None:
        """A null in the record reads as "we know it is nothing", which is different
        from "we did not record it"."""
        log = _log(tmp_path)
        log.append("job_queued", source="cron", identifier="nightly", thread_ts=None)
        record = json.loads((tmp_path / "events.jsonl").read_text().strip())
        assert "thread_ts" not in record

    def test_paths_are_stringified(self, tmp_path: Path) -> None:
        """So the JSON stays portable and diff-friendly rather than carrying a repr."""
        log = _log(tmp_path)
        log.append("run", source="cron", identifier="a", workspace=Path("/tmp/ws"))
        record = json.loads((tmp_path / "events.jsonl").read_text().strip())
        assert record["workspace"] == "/tmp/ws"

    def test_appends_accumulate(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("one", source="s", identifier="a")
        log.append("two", source="s", identifier="b")
        assert len((tmp_path / "events.jsonl").read_text().strip().splitlines()) == 2

    def test_a_path_that_cannot_be_opened_is_logged_not_raised(
        self, tmp_path: Path, caplog
    ) -> None:
        """The constructor creates the parent, so this is the later case: the
        directory became unwritable under a running daemon."""
        log = _log(tmp_path)
        tmp_path.chmod(0o500)
        try:
            with caplog.at_level("ERROR", logger="claude_on_the_fly.events"):
                log.append("job_queued", source="slack", identifier="C1")
        finally:
            tmp_path.chmod(0o755)
        assert "for append failed" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_write_that_fails_is_logged_and_the_fd_still_closed(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """The fd close is in a finally for exactly this: a daemon leaking one per
        event would run out of them in a day."""
        log = _log(tmp_path)
        closed: list[int] = []
        real_close = os.close

        def write_fails(_fd, _data):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "write", write_fails)
        monkeypatch.setattr(
            os, "close", lambda fd: (closed.append(fd), real_close(fd))[0]
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.events"):
            log.append("job_queued", source="slack", identifier="C1")
        assert "append to" in "\n".join(r.getMessage() for r in caplog.records)
        assert closed, "the fd was leaked"


class TestTail:
    def test_no_file_yet_reads_as_empty(self, tmp_path: Path) -> None:
        assert _log(tmp_path).tail(10) == []

    def test_a_non_positive_count_reads_as_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("one", source="s", identifier="a")
        assert log.tail(0) == []
        assert log.tail(-1) == []

    def test_the_last_n_come_back_oldest_first(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for index in range(5):
            log.append("run", source="s", identifier=str(index))
        rows = log.tail(3)
        assert [r["identifier"] for r in rows] == ["2", "3", "4"]

    def test_a_file_longer_than_one_block_is_still_read_backwards(
        self, tmp_path: Path
    ) -> None:
        """The reverse seek is what stops months of history being loaded to render 50
        rows, so it has to work across the 8KB block boundary."""
        log = _log(tmp_path)
        for index in range(400):
            log.append("run", source="s", identifier=str(index), pad="x" * 100)
        assert (tmp_path / "events.jsonl").stat().st_size > 8192
        rows = log.tail(2)
        assert [r["identifier"] for r in rows] == ["398", "399"]

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        """They typically come from a crashed write, and the next clean line is what
        the reader wanted anyway."""
        path = tmp_path / "events.jsonl"
        path.write_text(
            '{"type": "one", "identifier": "a"}\n'
            "not json\n"
            "\n"
            '["a list"]\n'
            '{"type": "two", "identifier": "b"}\n'
        )
        rows = EventLog(path).tail(10)
        assert [r["identifier"] for r in rows] == ["a", "b"]

    def test_a_file_that_cannot_be_read_reads_as_empty(
        self, tmp_path: Path, caplog
    ) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text('{"type": "one"}\n')
        path.chmod(0o000)
        try:
            with caplog.at_level("ERROR", logger="claude_on_the_fly.events"):
                assert EventLog(path).tail(10) == []
        finally:
            path.chmod(0o644)
        assert "read failed" in "\n".join(r.getMessage() for r in caplog.records)
