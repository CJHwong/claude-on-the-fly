"""Per-ticket cursor storage + claimability comparisons."""

from __future__ import annotations

from pathlib import Path


from claude_on_the_fly.symphony.cursor import (
    CursorStore,
    TicketCursor,
    _safe_filename,
    is_claimable,
)


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------


def test_safe_filename_jira_passthrough() -> None:
    assert _safe_filename("FIS-1234") == "FIS-1234"


def test_safe_filename_github_slash_and_hash() -> None:
    assert _safe_filename("owner/repo#42") == "owner__repo__42"


def test_safe_filename_strips_dangerous_chars() -> None:
    assert _safe_filename("a/b\\c?d") == "a__b__c__d"


# ---------------------------------------------------------------------------
# CursorStore I/O
# ---------------------------------------------------------------------------


def test_load_missing_returns_fresh_cursor(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, "jira")
    cursor = store.load("FIS-1")
    assert cursor.identifier == "FIS-1"
    assert cursor.last_job_done_time is None
    assert cursor.attempts == 0


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, "jira")
    cursor = TicketCursor(
        identifier="FIS-1",
        last_job_done_time="2026-05-27T10:00:00+00:00",
        last_run_outcome="terminal",
        attempts=2,
    )
    store.save(cursor)

    # New store instance bypasses the cache.
    fresh = CursorStore(tmp_path, "jira").load("FIS-1")
    assert fresh.last_job_done_time == "2026-05-27T10:00:00+00:00"
    assert fresh.last_run_outcome == "terminal"
    assert fresh.attempts == 2


def test_save_uses_atomic_replace(tmp_path: Path) -> None:
    """A failed write must not leave the previous file truncated."""
    store = CursorStore(tmp_path, "jira")
    cursor = TicketCursor(
        identifier="FIS-1",
        last_job_done_time="2026-05-27T10:00:00+00:00",
    )
    store.save(cursor)
    target = store._path("FIS-1")
    assert target.is_file()
    # No leftover .tmp file post-save.
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_record_run_end_bumps_and_persists(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, "jira")
    store.record_run_end("FIS-1", outcome="terminal")
    cursor = store.load("FIS-1")
    assert cursor.last_job_done_time is not None
    assert cursor.last_run_outcome == "terminal"
    assert cursor.attempts == 1

    store.record_run_end("FIS-1", outcome="yield")
    cursor = store.load("FIS-1")
    assert cursor.last_run_outcome == "yield"
    assert cursor.attempts == 2


def test_load_invalid_json_falls_back_to_fresh(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, "jira")
    store._dir.mkdir(parents=True, exist_ok=True)
    (store._dir / "FIS-1.json").write_text("not valid json")
    cursor = store.load("FIS-1")
    assert cursor.identifier == "FIS-1"
    assert cursor.last_job_done_time is None


def test_github_identifier_writes_safe_filename(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, "github")
    store.record_run_end("hardcoretech/fms#4521", outcome="review_submitted")
    safe = "hardcoretech__fms__4521.json"
    assert (store._dir / safe).is_file()


# ---------------------------------------------------------------------------
# is_claimable
# ---------------------------------------------------------------------------


def test_is_claimable_when_no_prior_run() -> None:
    cursor = TicketCursor(identifier="FIS-1", last_job_done_time=None)
    assert (
        is_claimable(ticket_updated="2026-05-27T10:00:00+00:00", cursor=cursor) is True
    )


def test_is_claimable_when_ticket_newer() -> None:
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    assert (
        is_claimable(ticket_updated="2026-05-27T10:05:00+00:00", cursor=cursor) is True
    )


def test_not_claimable_when_ticket_unchanged() -> None:
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    assert (
        is_claimable(ticket_updated="2026-05-27T10:00:00+00:00", cursor=cursor) is False
    )


def test_not_claimable_when_ticket_older() -> None:
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    assert (
        is_claimable(ticket_updated="2026-05-27T09:00:00+00:00", cursor=cursor) is False
    )


def test_jira_offset_format_compatible() -> None:
    """Jira emits `2026-05-22T15:31:51.189+0800` (no colon in offset).
    The parser must accept it."""
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-22T15:31:51.000+00:00"
    )
    assert (
        is_claimable(ticket_updated="2026-05-22T15:31:51.189+0800", cursor=cursor)
        is False  # +0800 == 07:31:51 UTC < 15:31:51 UTC
    )


def test_z_suffix_compatible() -> None:
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    assert is_claimable(ticket_updated="2026-05-27T11:00:00Z", cursor=cursor) is True


def test_is_claimable_when_ticket_updated_missing() -> None:
    """If the tracker didn't return `updated`, we err on the side of claiming
    so the user's ticket doesn't get silently dropped."""
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    assert is_claimable(ticket_updated=None, cursor=cursor) is True
    assert is_claimable(ticket_updated="", cursor=cursor) is True


def test_is_claimable_naive_timestamp_does_not_raise() -> None:
    """A tz-naive `updated` (degenerate payload) must not blow up comparing
    against the aware-UTC cursor — it's treated as UTC, not a TypeError."""
    cursor = TicketCursor(
        identifier="FIS-1", last_job_done_time="2026-05-27T10:00:00+00:00"
    )
    # No offset → naive → assumed UTC. 11:00 > 10:00 → claimable.
    assert is_claimable(ticket_updated="2026-05-27T11:00:00", cursor=cursor) is True


def test_record_run_end_stamps_max_of_now_and_ticket_updated(tmp_path: Path) -> None:
    """If the ticket's own `updated` is ahead of wall-clock now (clock skew /
    late-indexed agent write), the cursor stamps the ticket time so the next
    tick doesn't re-claim a just-finished ticket."""
    store = CursorStore(tmp_path, "jira")
    future = "2099-01-01T00:00:00+00:00"
    cursor = store.record_run_end("FIS-1", outcome="yield", ticket_updated=future)
    assert cursor.last_job_done_time == future
    # And that ticket, unchanged, is now NOT claimable.
    assert is_claimable(ticket_updated=future, cursor=cursor) is False
