"""EventLog: append + tail roundtrip, ordering, malformed lines, edge cases."""

from __future__ import annotations

import json
from pathlib import Path

from claude_on_the_fly.events import (
    EVENT_CANCELLED,
    EVENT_DISPATCHED,
    EVENT_RETRY_SCHEDULED,
    EventLog,
)


def test_append_creates_file_and_dir(tmp_path: Path) -> None:
    log_path = tmp_path / "state" / "events.jsonl"
    log = EventLog(log_path)
    log.append(EVENT_DISPATCHED, source="jira", identifier="PROJ-1")
    assert log_path.is_file()


def test_append_writes_one_line_per_event(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(EVENT_DISPATCHED, source="jira", identifier="PROJ-1")
    log.append(EVENT_DISPATCHED, source="jira", identifier="PROJ-2")
    log.append(EVENT_CANCELLED, source="jira", identifier="PROJ-1", reason="terminal")
    raw = (tmp_path / "events.jsonl").read_text()
    assert raw.count("\n") == 3


def test_tail_returns_records_oldest_first(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(EVENT_DISPATCHED, source="jira", identifier="A")
    log.append(EVENT_DISPATCHED, source="jira", identifier="B")
    log.append(EVENT_DISPATCHED, source="jira", identifier="C")
    out = log.tail(10)
    assert [r["identifier"] for r in out] == ["A", "B", "C"]


def test_tail_caps_to_n(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(20):
        log.append(EVENT_DISPATCHED, source="jira", identifier=f"PROJ-{i}")
    out = log.tail(5)
    assert len(out) == 5
    assert [r["identifier"] for r in out] == [f"PROJ-{i}" for i in range(15, 20)]


def test_tail_empty_file_returns_empty(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    assert log.tail(10) == []


def test_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "subdir" / "events.jsonl")
    # __init__ creates the parent dir but not the file itself.
    assert log.tail(10) == []


def test_tail_zero_n_returns_empty(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(EVENT_DISPATCHED, source="jira", identifier="A")
    assert log.tail(0) == []


def test_tail_skips_malformed_lines(tmp_path: Path) -> None:
    """A crashed mid-write or manual edit shouldn't break readers."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(EVENT_DISPATCHED, source="jira", identifier="A")
    # Inject a torn line between two valid records.
    with path.open("a") as f:
        f.write('{"type": "dispatched", "broken\n')  # incomplete json
        f.write("not-even-json\n")
    log.append(EVENT_DISPATCHED, source="jira", identifier="B")
    out = log.tail(10)
    assert [r["identifier"] for r in out] == ["A", "B"]


def test_tail_handles_large_file(tmp_path: Path) -> None:
    """Reverse-seek should only read tail blocks, not the whole file."""
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(5000):
        log.append(
            EVENT_DISPATCHED,
            source="jira",
            identifier=f"PROJ-{i}",
            extra_padding="x" * 200,  # bloat so file > 1 MB
        )
    out = log.tail(3)
    assert [r["identifier"] for r in out] == ["PROJ-4997", "PROJ-4998", "PROJ-4999"]


def test_event_record_shape(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        EVENT_DISPATCHED,
        source="github",
        identifier="owner/repo#42",
        workspace=Path("/tmp/ws"),
        session_uuid="abc",
        state="open",
    )
    record = log.tail(1)[0]
    assert record["type"] == "dispatched"
    assert record["source"] == "github"
    assert record["identifier"] == "owner/repo#42"
    assert record["workspace"] == "/tmp/ws"
    assert record["session_uuid"] == "abc"
    assert record["state"] == "open"
    assert "ts" in record  # ISO-8601 UTC


def test_append_drops_none_values(tmp_path: Path) -> None:
    """Optional fields passed as None should be omitted, not stored as null."""
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        EVENT_DISPATCHED,
        source="jira",
        identifier="PROJ-1",
        workspace=None,
        session_uuid="abc",
    )
    record = log.tail(1)[0]
    assert "workspace" not in record
    assert record["session_uuid"] == "abc"


def test_append_preserves_extra_dict_payload(tmp_path: Path) -> None:
    """Nested dicts/lists must round-trip cleanly through JSON."""
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        EVENT_RETRY_SCHEDULED,
        source="jira",
        identifier="PROJ-1",
        attempt=3,
        error="claude unavailable",
    )
    record = log.tail(1)[0]
    assert record["attempt"] == 3
    assert record["error"] == "claude unavailable"


def test_concurrent_appends_dont_interleave(tmp_path: Path) -> None:
    """O_APPEND atomicity check — every line should be a complete JSON object."""
    import threading

    log = EventLog(tmp_path / "events.jsonl")

    def hammer(prefix: str) -> None:
        for i in range(50):
            log.append(EVENT_DISPATCHED, source="jira", identifier=f"{prefix}-{i}")

    threads = [threading.Thread(target=hammer, args=(p,)) for p in ("A", "B", "C", "D")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = (tmp_path / "events.jsonl").read_text()
    lines = [line for line in raw.splitlines() if line]
    assert len(lines) == 200
    # Every line must be a valid JSON object — no interleaving.
    for line in lines:
        json.loads(line)
