"""FileInboxQueue: FIFO claim, no double-claim, restart survival, recovery,
poison isolation, and opaque origin round-trip — plus the read-only observers
(`read_queue_depth` / `read_queue_rows`) the TUI's jobs tab reads through."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from claude_on_the_fly.jobs import file_queue
from claude_on_the_fly.jobs.core import Job, Result
from claude_on_the_fly.jobs.file_queue import (
    DELIVERY_RETRY_WINDOW_S,
    DONE_RETENTION_S,
    PROMPT_PREVIEW_LIMIT,
    FileInboxQueue,
    read_queue_depth,
    read_queue_rows,
)


def _job(job_id: str, prompt: str = "do it", **origin: object) -> Job:
    return Job(id=job_id, prompt=prompt, origin=dict(origin) or {"channel": "C1"})


def test_construction_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    """Lazy tree: merely constructing the queue (as a Slack frontend does in
    __init__) must not create directories in the real home."""
    root = tmp_path / "jobs"
    FileInboxQueue(root)
    assert not root.exists()


def test_enqueue_then_claim_roundtrip(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job("100-aaaa", prompt="hello"))
    claimed = q.claim()
    assert claimed is not None
    assert claimed.id == "100-aaaa"
    assert claimed.prompt == "hello"


def test_claim_returns_none_when_empty(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    assert q.claim() is None


def test_claim_is_fifo_by_id(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    for jid in ("300-c", "100-a", "200-b"):
        q.enqueue(_job(jid))
    order = [q.claim(), q.claim(), q.claim()]
    assert [j.id for j in order if j] == ["100-a", "200-b", "300-c"]


def test_no_double_claim_of_same_job(tmp_path: Path) -> None:
    """Two queue handles over one root: a single job is claimed exactly once."""
    root = tmp_path / "jobs"
    q1 = FileInboxQueue(root)
    q2 = FileInboxQueue(root)
    q1.enqueue(_job("100-a"))
    first = q1.claim()
    second = q2.claim()
    assert first is not None and first.id == "100-a"
    assert second is None


def test_concurrent_claim_never_double_claims(tmp_path: Path) -> None:
    """Threaded workers draining a shared inbox: every job claimed by exactly
    one worker (exercises the FileNotFoundError lost-race skip)."""
    root = tmp_path / "jobs"
    producer = FileInboxQueue(root)
    n = 60
    for i in range(n):
        producer.enqueue(_job(f"{i:04d}-x"))

    claimed: list[str] = []
    lock = threading.Lock()

    def drain() -> None:
        q = FileInboxQueue(root)
        while True:
            job = q.claim()
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n
    assert len(set(claimed)) == n  # no id claimed twice


def test_job_in_new_survives_restart(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    FileInboxQueue(root).enqueue(_job("100-a"))
    # Fresh instance, same root — as after a worker process restart.
    claimed = FileInboxQueue(root).claim()
    assert claimed is not None and claimed.id == "100-a"


def test_recover_stale_requeues_all_by_default(tmp_path: Path) -> None:
    """A claimed-then-crashed job (in cur/) is re-run after recover_stale(None)."""
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job("100-a"))
    claimed = q.claim()  # now in cur/
    assert claimed is not None
    assert q.claim() is None  # nothing left in new/

    n = q.recover_stale(None)
    assert n == 1
    again = q.claim()
    assert again is not None and again.id == "100-a"


def test_recover_stale_ttl_skips_fresh(tmp_path: Path) -> None:
    """A positive TTL leaves a just-claimed job in cur/ (younger than the TTL)."""
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job("100-a"))
    q.claim()
    assert q.recover_stale(ttl_s=3600.0) == 0
    assert q.claim() is None  # still in cur/, not requeued


def test_recover_stale_empty_is_zero(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    assert q.recover_stale(None) == 0


def test_complete_archives_job_and_writes_result(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))
    job = q.claim()
    assert job is not None
    q.complete(job, Result(ok=True, text="all done"))

    assert (root / "done" / "100-a.json").exists()
    result_file = root / "done" / "100-a.result.json"
    assert result_file.exists()
    saved = json.loads(result_file.read_text())
    assert saved["ok"] is True
    assert saved["text"] == "all done"
    # No longer in cur/ or new/ — not re-claimable.
    assert not (root / "cur" / "100-a.json").exists()
    assert q.claim() is None


def test_completed_job_not_requeued_by_recover(tmp_path: Path) -> None:
    q = FileInboxQueue(tmp_path / "jobs")
    q.enqueue(_job("100-a"))
    job = q.claim()
    assert job is not None
    q.complete(job, Result(ok=False, text="boom"))
    assert q.recover_stale(None) == 0


def test_poison_file_isolated_to_failed(tmp_path: Path) -> None:
    """An unparseable file in new/ is quarantined to failed/, and claim keeps
    scanning past it to a real job."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("200-good"))
    # Hand-drop a garbage file that sorts before the good one.
    (root / "new" / "100-bad.json").write_text("{ not valid json", encoding="utf-8")

    claimed = q.claim()
    assert claimed is not None and claimed.id == "200-good"
    assert (root / "failed" / "100-bad.json").exists()
    assert not (root / "new" / "100-bad.json").exists()
    assert not (root / "cur" / "100-bad.json").exists()


def test_id_mismatch_is_poison(tmp_path: Path) -> None:
    """A file whose embedded id disagrees with its name is poison — complete
    would otherwise re-derive the wrong path and lose it."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q._ensure_tree()
    (root / "new" / "100-a.json").write_text(
        json.dumps({"id": "999-z", "prompt": "p", "origin": {}}), encoding="utf-8"
    )
    assert q.claim() is None
    assert (root / "failed" / "100-a.json").exists()


def test_poison_never_requeued_by_recover(tmp_path: Path) -> None:
    """recover_stale only touches cur/, so quarantined poison can't re-loop."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q._ensure_tree()
    (root / "new" / "100-bad.json").write_text("garbage", encoding="utf-8")
    q.claim()  # quarantines it
    assert q.recover_stale(None) == 0
    assert (root / "failed" / "100-bad.json").exists()


def test_opaque_origin_round_trips(tmp_path: Path) -> None:
    """Origin is stored verbatim and read back identical — never validated or
    reshaped by the queue."""
    q = FileInboxQueue(tmp_path / "jobs")
    origin = {
        "channel": "C1",
        "thread_ts": "1699.5",
        "sender_id": "U9",
        "nested": {"a": 1},
    }
    q.enqueue(Job(id="100-a", prompt="p", origin=origin))
    claimed = q.claim()
    assert claimed is not None
    assert claimed.origin == origin


# ---------------------------------------------------------------------------
# Read-only observers: read_queue_depth / read_queue_rows
# ---------------------------------------------------------------------------


def test_read_queue_depth_on_missing_root_is_all_zeros(tmp_path: Path) -> None:
    """An observer must describe a queue that was never created — and must not
    create it on the way past."""
    root = tmp_path / "nope"
    depth = read_queue_depth(root)
    assert (depth.new, depth.running, depth.done, depth.failed) == (0, 0, 0, 0)
    assert read_queue_rows(root) == []
    assert not root.exists()


def test_read_queue_depth_never_creates_the_maildir(tmp_path: Path) -> None:
    """The read-only contract, asserted rather than described: neither reader
    may create root or any of the five subdirs."""
    root = tmp_path / "jobs"
    read_queue_depth(root)
    read_queue_rows(root)
    assert not root.exists()


def test_read_queue_depth_counts_each_stage(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))
    q.enqueue(_job("200-b"))
    q.enqueue(_job("300-c"))
    claimed = q.claim()  # 100-a → cur/
    assert claimed is not None

    depth = read_queue_depth(root)
    assert (depth.new, depth.running, depth.done, depth.failed) == (2, 1, 0, 0)


def test_completed_job_is_counted_exactly_once(tmp_path: Path) -> None:
    """complete() writes BOTH <id>.json and <id>.result.json into done/, so a
    naive *.json count would double every finished job."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))
    claimed = q.claim()
    assert claimed is not None
    q.complete(claimed, Result(ok=True, text="done"))

    # Both files really are there — this is what makes the count non-obvious.
    assert (root / "done" / "100-a.json").exists()
    assert (root / "done" / "100-a.result.json").exists()
    assert read_queue_depth(root).done == 1


def test_quarantined_job_counts_as_failed(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q._ensure_tree()
    (root / "new" / "100-bad.json").write_text("garbage", encoding="utf-8")
    assert q.claim() is None  # quarantines it
    assert read_queue_depth(root).failed == 1


def test_archive_counts_refresh_after_a_later_write(tmp_path: Path) -> None:
    """The done/failed counts are memoized on directory mtime; a second job
    finishing must still be seen (the memo is exact for add/remove, which is
    all this queue ever does)."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))
    first = q.claim()
    assert first is not None
    q.complete(first, Result(ok=True, text="1"))
    assert read_queue_depth(root).done == 1  # populates the memo

    q.enqueue(_job("200-b"))
    second = q.claim()
    assert second is not None
    q.complete(second, Result(ok=True, text="2"))
    assert read_queue_depth(root).done == 2


def test_read_queue_rows_lists_in_flight_first_then_queued(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    for jid in ("100-a", "200-b", "300-c"):
        q.enqueue(_job(jid, prompt=f"prompt for {jid}"))
    claimed = q.claim()  # 100-a → cur/
    assert claimed is not None

    rows = read_queue_rows(root)
    assert [(r.id, r.in_flight) for r in rows] == [
        ("100-a", True),
        ("200-b", False),
        ("300-c", False),
    ]
    assert rows[0].prompt == "prompt for 100-a"


def test_read_queue_rows_is_hard_capped(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    for i in range(10):
        q.enqueue(_job(f"{100 + i}-x"))
    assert len(read_queue_rows(root, limit=3)) == 3
    assert read_queue_rows(root, limit=0) == []


def test_row_enqueued_at_comes_from_the_id_not_the_file(tmp_path: Path) -> None:
    """The age is derived from the id's leading time_ns, so touching the file
    (a backup, an rsync) cannot move it."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    minted_ns = 1_700_000_000_000_000_000
    q.enqueue(_job(f"{minted_ns}-abcd1234"))
    os.utime(root / "new" / f"{minted_ns}-abcd1234.json", (0, 0))

    row = read_queue_rows(root)[0]
    assert row.enqueued_at == datetime.fromtimestamp(minted_ns / 1_000_000_000, tz=UTC)


def test_row_with_unparseable_id_has_no_enqueued_at(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("not-a-timestamp"))
    row = read_queue_rows(root)[0]
    assert row.id == "not-a-timestamp"
    assert row.enqueued_at is None


def test_row_with_unreadable_file_has_no_prompt(tmp_path: Path) -> None:
    """A half-written or hand-mangled file degrades one cell, never the read."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q._ensure_tree()
    (root / "new" / "100-a.json").write_text("{not json", encoding="utf-8")
    row = read_queue_rows(root)[0]
    assert row.id == "100-a"
    assert row.prompt is None


def test_idle_tick_does_not_walk_the_archive_dirs(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the memo: a second read with nothing changed must
    re-count new/ and cur/ (they move constantly and are small) but must NOT
    re-walk done/ or failed/, which grow without bound."""
    from claude_on_the_fly.jobs import file_queue as fq

    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))
    claimed = q.claim()
    assert claimed is not None
    q.complete(claimed, Result(ok=True, text="ok"))

    read_queue_depth(root)  # warm the memo

    walked: list[Path] = []
    real_count = fq._count

    def _spy(directory: Path, pattern: str) -> int:
        walked.append(directory)
        return real_count(directory, pattern)

    monkeypatch.setattr(fq, "_count", _spy)
    read_queue_depth(root)

    assert walked == [root / "new", root / "cur"]


def test_idle_tick_does_not_reopen_job_files(tmp_path: Path, monkeypatch) -> None:
    """Rows are the expensive read — a file open plus a json parse each, up to
    the cap, at 1Hz whether or not the jobs tab is even visible. A tick with
    nothing changed must open none of them, and a job enqueued afterwards must
    still show up on the next one."""
    from claude_on_the_fly.jobs import file_queue as fq

    fq._rows_cache.clear()  # so the assertions below prove the memo, not luck

    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q.enqueue(_job("100-a"))

    opened: list[Path] = []
    real_read_fields = fq._read_row_fields

    def _spy(path: Path) -> tuple[str | None, dict]:
        opened.append(path)
        return real_read_fields(path)

    monkeypatch.setattr(fq, "_read_row_fields", _spy)

    assert [r.id for r in read_queue_rows(root)] == ["100-a"]
    assert opened == [root / "new" / "100-a.json"]  # cold read

    opened.clear()
    assert [r.id for r in read_queue_rows(root)] == ["100-a"]
    assert opened == []  # idle tick: answered from the memo, nothing opened

    q.enqueue(_job("200-b"))
    # Push new/'s mtime past the memoized stamp by hand. The memo is only as
    # fine-grained as the filesystem's timestamps, and on a 1-second-resolution
    # mount both writes could land in the same tick — this bump is what keeps
    # the assertion about the memo rather than about the filesystem.
    stamp = fq._mtime_ns(root / "new") + 1_000_000_000
    os.utime(root / "new", ns=(stamp, stamp))

    assert [r.id for r in read_queue_rows(root)] == ["100-a", "200-b"]
    # Invalidation recomputes the whole page, not a delta — the memo's unit is
    # the read, and a rename can reorder rows that were already on it.
    assert opened == [root / "new" / "100-a.json", root / "new" / "200-b.json"]


def test_rows_never_repeat_an_id_across_cur_and_new(tmp_path: Path) -> None:
    """cur/ and new/ are listed one after the other, and the worker can move a
    file between them in between — `recover_stale` does exactly that at every
    start. A repeated id would make a caller keying rows by id (the TUI's
    DataTable) raise, so the reader dedupes with cur/ winning."""
    root = tmp_path / "jobs"
    q = FileInboxQueue(root)
    q._ensure_tree()
    # The state the race produces: the same id visible in both directories.
    for stage in ("cur", "new"):
        (root / stage / "100-a.json").write_text(
            json.dumps({"id": "100-a", "prompt": "p", "origin": {}}), encoding="utf-8"
        )

    rows = read_queue_rows(root)
    assert [r.id for r in rows] == ["100-a"]
    assert rows[0].in_flight is True  # cur/ wins


def test_archive_prunes_entries_past_the_retention_window(tmp_path: Path) -> None:
    """done/ keeps every prompt and every agent reply verbatim and nothing else
    removes them, so without a window it is unbounded disk growth."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    queue.enqueue(_job("100-old"))
    claimed = queue.claim()
    assert claimed is not None
    queue.complete(claimed, Result(ok=True, text="ancient"))

    stale = os.stat(root / "done" / "100-old.json").st_mtime - DONE_RETENTION_S - 60
    for name in ("100-old.json", "100-old.result.json"):
        os.utime(root / "done" / name, (stale, stale))

    queue.enqueue(_job("200-new"))
    fresh = queue.claim()
    assert fresh is not None
    queue.complete(fresh, Result(ok=True, text="recent"))

    archived = sorted(p.name for p in (root / "done").glob("*.json"))
    assert archived == ["200-new.json", "200-new.result.json"]


def test_archive_keeps_entries_inside_the_window(tmp_path: Path) -> None:
    """Age, not count: a burst of new jobs must not evict this morning's."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    for job_id in ("100-a", "200-b", "300-c"):
        queue.enqueue(_job(job_id))
        claimed = queue.claim()
        assert claimed is not None
        queue.complete(claimed, Result(ok=True, text="ok"))

    assert read_queue_depth(root).done == 3


def test_row_prompt_is_truncated_at_read(tmp_path: Path) -> None:
    """Rows are memoized for as long as the queue holds still, so an untruncated
    prompt is pinned in the observing process — and a prompt is whatever
    somebody pasted into a chat message, with no size limit."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    queue.enqueue(_job("100-a", prompt="x" * (PROMPT_PREVIEW_LIMIT * 4)))

    rows = read_queue_rows(root)

    assert rows[0].prompt is not None
    assert len(rows[0].prompt) == PROMPT_PREVIEW_LIMIT


def test_observer_memos_stay_bounded_across_roots(tmp_path: Path) -> None:
    """The memos are process-global with no eviction of their own: a caller that
    varies root — every test in this suite, via tmp_path — would otherwise grow
    them for the life of the process."""
    from claude_on_the_fly.jobs import file_queue as fq

    for index in range(50):
        root = tmp_path / f"queue-{index}"
        queue = FileInboxQueue(root)
        queue.enqueue(_job(f"{index + 100}-a"))
        read_queue_depth(root)
        read_queue_rows(root)

    assert len(fq._archive_count_cache) <= fq._MAX_COUNT_MEMOS
    assert len(fq._rows_cache) <= fq._MAX_ROW_MEMOS


# --- delivery tracking -------------------------------------------------------


def _complete_one(queue: FileInboxQueue, job_id: str, text: str = "the answer"):
    queue.enqueue(_job(job_id))
    claimed = queue.claim()
    assert claimed is not None
    queue.complete(claimed, Result(ok=True, text=text))
    return claimed


def test_a_completed_result_starts_undelivered(tmp_path: Path) -> None:
    queue = FileInboxQueue(tmp_path / "jobs")
    _complete_one(queue, "100-a")

    pending = queue.undelivered()

    assert [d.job_id for d in pending] == ["100-a"]
    assert pending[0].result.text == "the answer"
    assert pending[0].origin == {"channel": "C1"}


def test_marking_delivered_removes_it_from_the_pending_list(tmp_path: Path) -> None:
    queue = FileInboxQueue(tmp_path / "jobs")
    _complete_one(queue, "100-a")

    queue.mark_delivered("100-a")

    assert queue.undelivered() == []


def test_the_marker_does_not_disturb_the_result_or_the_depth(tmp_path: Path) -> None:
    """The result is written once and never touched again, and `done` counts
    *.result.json — a marker must not corrupt one or inflate the other."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    _complete_one(queue, "100-a")
    before = (root / "done" / "100-a.result.json").read_text()

    queue.mark_delivered("100-a")

    assert (root / "done" / "100-a.result.json").read_text() == before
    assert read_queue_depth(root).done == 1


def test_undelivered_skips_results_past_the_retry_window(tmp_path: Path) -> None:
    """A permanently undeliverable result — archived channel, revoked token —
    must not be retried on every start until the archive prunes it."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    _complete_one(queue, "100-a")

    stale = os.stat(root / "done" / "100-a.result.json").st_mtime
    stale -= DELIVERY_RETRY_WINDOW_S + 60
    os.utime(root / "done" / "100-a.result.json", (stale, stale))

    assert queue.undelivered() == []


def test_undelivered_skips_a_result_whose_origin_is_gone(tmp_path: Path) -> None:
    """There is nowhere to deliver a result whose job record cannot be read."""
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    _complete_one(queue, "100-a")
    (root / "done" / "100-a.json").unlink()

    assert queue.undelivered() == []


def test_delivery_markers_are_pruned_with_the_archive(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    queue = FileInboxQueue(root)
    _complete_one(queue, "100-a")
    queue.mark_delivered("100-a")

    stale = os.stat(root / "done" / "100-a.json").st_mtime - DONE_RETENTION_S - 60
    for path in (root / "done").glob("100-a*"):
        os.utime(path, (stale, stale))

    _complete_one(queue, "200-b")  # completion is what prunes

    assert sorted(p.name for p in (root / "done").glob("100-a*")) == []


# --- dispatch fields round-trip -------------------------------------------


def test_dispatch_fields_survive_enqueue_and_claim(tmp_path: Path) -> None:
    """The queue is the only thing between a producer and the runner, so a field
    it drops is a field the runner silently defaults."""
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(
        Job(
            id="1-a",
            prompt="p",
            origin={"kind": "cron"},
            key="jira/ACE-1",
            session_key="jira/ACE-1",
            timeout=90.0,
            platform="cron",
        )
    )

    claimed = queue.claim()

    assert claimed is not None
    assert claimed.key == "jira/ACE-1"
    assert claimed.session_key == "jira/ACE-1"
    assert claimed.timeout == 90.0
    assert claimed.platform == "cron"


def test_a_job_without_dispatch_fields_is_an_unkeyed_one_shot(tmp_path: Path) -> None:
    """A record written by a producer that does not care about any of them — or
    before they existed — is ordinary work, not poison."""
    queue = FileInboxQueue(tmp_path)
    (tmp_path / "new").mkdir(parents=True)
    (tmp_path / "new" / "1-a.json").write_text(
        json.dumps({"id": "1-a", "prompt": "p", "origin": {"channel": "C1"}}),
        encoding="utf-8",
    )

    claimed = queue.claim()

    assert claimed is not None
    assert claimed.key is None
    assert claimed.session_key is None
    assert claimed.timeout is None
    assert claimed.platform == "jobs"


def test_a_wrongly_typed_dispatch_field_is_poison(tmp_path: Path) -> None:
    """A non-string session_key would reach the uuid derivation and a non-numeric
    timeout would reach asyncio.wait_for, so both are caught at the boundary."""
    queue = FileInboxQueue(tmp_path)
    (tmp_path / "new").mkdir(parents=True)
    (tmp_path / "new" / "1-a.json").write_text(
        json.dumps(
            {
                "id": "1-a",
                "prompt": "p",
                "origin": {},
                "session_key": ["not", "a", "str"],
            }
        ),
        encoding="utf-8",
    )

    assert queue.claim() is None
    assert (tmp_path / "failed" / "1-a.json").is_file()


def test_a_boolean_timeout_is_poison_not_one_second(tmp_path: Path) -> None:
    """`bool` is an `int` subclass, so a plain isinstance check would accept
    `true` and quietly impose a one-second limit."""
    queue = FileInboxQueue(tmp_path)
    (tmp_path / "new").mkdir(parents=True)
    (tmp_path / "new" / "1-a.json").write_text(
        json.dumps({"id": "1-a", "prompt": "p", "origin": {}, "timeout": True}),
        encoding="utf-8",
    )

    assert queue.claim() is None
    assert (tmp_path / "failed" / "1-a.json").is_file()


# --- count_unfinished (producer admission) ---------------------------------


def _keyed(job_id: str, key: str) -> Job:
    return Job(id=job_id, prompt="p", origin={"kind": "cron"}, key=key)


def test_count_unfinished_sees_queued_and_in_flight(tmp_path: Path) -> None:
    """Both mean "already handed over", so both have to count — a claimed job
    that is still running must not be re-enqueued."""
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(_keyed("100-a", "jira/ACE-1"))
    queue.enqueue(_keyed("101-b", "jira/ACE-2"))

    assert queue.count_unfinished("jira") == 2

    queue.claim()  # ACE-1 moves new/ -> cur/

    assert queue.count_unfinished("jira") == 2
    assert queue.count_unfinished("jira", "ACE-1") == 1


def test_count_unfinished_drops_to_zero_once_complete(tmp_path: Path) -> None:
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(_keyed("100-a", "jira/ACE-1"))
    job = queue.claim()
    assert job is not None

    queue.complete(job, Result(ok=True, text="done"))

    assert queue.count_unfinished("jira") == 0
    assert queue.count_unfinished("jira", "ACE-1") == 0


def test_count_unfinished_isolates_entries(tmp_path: Path) -> None:
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(_keyed("100-a", "jira/ACE-1"))
    queue.enqueue(_keyed("101-b", "prs/owner/repo#7"))

    assert queue.count_unfinished("jira") == 1
    assert queue.count_unfinished("prs") == 1
    assert queue.count_unfinished("jira", "ACE-1") == 1
    assert queue.count_unfinished("jira", "owner/repo#7") == 0


def test_count_unfinished_ignores_unkeyed_jobs(tmp_path: Path) -> None:
    """A Slack-triggered job belongs to no entry, so it cannot consume a cron
    entry's concurrency budget."""
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(Job(id="100-a", prompt="p", origin={"channel": "C1"}))

    assert queue.count_unfinished("jira") == 0


def test_count_unfinished_on_a_missing_queue_is_zero(tmp_path: Path) -> None:
    """It gates a write on a queue that may not exist yet, and must not build the
    tree as a side effect of being asked."""
    queue = FileInboxQueue(tmp_path / "absent")

    assert queue.count_unfinished("jira") == 0
    assert not (tmp_path / "absent").exists()


def test_a_keyed_job_round_trips_through_claim_and_complete(tmp_path: Path) -> None:
    """The keyed filename must not break the id-vs-filename poison check, which
    compares the embedded id against the name."""
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(_keyed("100-a", "jira/ACE-1"))

    job = queue.claim()

    assert job is not None
    assert job.id == "100-a"
    assert job.key == "jira/ACE-1"
    queue.complete(job, Result(ok=True, text="ok"))
    assert (tmp_path / "done" / "100-a.json").is_file()
    assert (tmp_path / "done" / "100-a.result.json").is_file()
    assert not list((tmp_path / "failed").glob("*.json"))


def test_a_keyed_job_reports_its_bare_id_to_observers(tmp_path: Path) -> None:
    """The TUI keys rows by id; handing it the id-plus-key stem would show a
    different string than `claim()` logs and than `done/` is named after."""
    queue = FileInboxQueue(tmp_path)
    queue.enqueue(_keyed("100-a", "jira/ACE-1"))

    rows = read_queue_rows(tmp_path)

    assert [row.id for row in rows] == ["100-a"]
    assert rows[0].enqueued_at is not None


# ---------------------------------------------------------------------------
# A queue on a filesystem another process is also touching
# ---------------------------------------------------------------------------


class TestRacesAndPermissionProblems:
    """Every failure in this class is "log and carry on". The queue is a maildir on
    a disk that a syncer, another worker, and the OS all touch, so a single bad file
    must never stop the worker draining the rest."""

    def test_completing_a_job_whose_file_is_already_gone_is_not_fatal(
        self, tmp_path: Path, caplog
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        job = _job("1")
        queue.enqueue(job)
        claimed = queue.claim()
        assert claimed is not None
        # A previous worker's recover_stale already moved it back to new/.
        for stray in (tmp_path / "cur").glob("*.json"):
            stray.unlink()
        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.file_queue"):
            queue.complete(claimed, Result(ok=True, text="done"))
        assert "not in cur/" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_file_that_cannot_be_pruned_is_left_alone(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        queue.enqueue(_job("1"))
        claimed = queue.claim()
        assert claimed is not None
        queue.complete(claimed, Result(ok=True, text="done"))

        # Genuinely old rather than a patched stat, so the prune decision itself is
        # the real one and only the unlink fails.
        ancient = time.time() - DONE_RETENTION_S - 3600
        for archived in (tmp_path / "done").glob("*.json"):
            os.utime(archived, (ancient, ancient))

        def unlink_fails(self, *_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", unlink_fails)
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.jobs.file_queue"):
            queue._prune_archive()
        assert "could not prune" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_delivery_marker_that_cannot_be_written_is_not_fatal(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Worst case the reply is delivered twice after a restart, which beats
        failing a job whose work is already done."""
        queue = FileInboxQueue(tmp_path)

        def write_fails(self, *_args, **_kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "write_text", write_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.file_queue"):
            queue.mark_delivered("1")
        assert "could not mark 1 delivered" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_result_that_cannot_be_stated_is_skipped_by_undelivered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        queue.enqueue(_job("1"))
        claimed = queue.claim()
        assert claimed is not None
        queue.complete(claimed, Result(ok=True, text="done"))
        real_stat = Path.stat

        def stat_fails(self, *args, **kwargs):
            # Only the result file: the `.delivered.json` existence probe stats too,
            # and it runs before the guarded block.
            if self.name.endswith(".result.json"):
                raise OSError("stale handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_fails)
        assert queue.undelivered() == []

    def test_a_corrupt_result_file_is_not_redelivered(
        self, tmp_path: Path, caplog
    ) -> None:
        """Redelivery reads the archived result, and half-written JSON there must not
        take the worker down on startup."""
        done = tmp_path / "done"
        done.mkdir(parents=True)
        # undelivered() keys off *.result.json, and the job file beside it is where
        # the origin comes from.
        (done / "1.result.json").write_text(
            json.dumps({"ok": True, "text": "x"}), encoding="utf-8"
        )
        (done / "1.json").write_text("{not json", encoding="utf-8")
        queue = FileInboxQueue(tmp_path)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.file_queue"):
            assert queue.undelivered() == []
        assert "cannot redeliver 1" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_result_whose_origin_is_not_a_mapping_is_not_redelivered(
        self, tmp_path: Path
    ) -> None:
        """A non-dict origin has no channel to reply into, so the notifier would
        raise on every retry forever."""
        done = tmp_path / "done"
        done.mkdir(parents=True)
        (done / "1.result.json").write_text(
            json.dumps({"ok": True, "text": "x"}), encoding="utf-8"
        )
        (done / "1.json").write_text(json.dumps({"origin": "C1"}), encoding="utf-8")
        queue = FileInboxQueue(tmp_path)
        assert queue.undelivered() == []

    def test_recover_stale_skips_a_file_it_cannot_stat(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        queue.enqueue(_job("1"))
        assert queue.claim() is not None
        real_stat = Path.stat

        def stat_fails(self, *args, **kwargs):
            if self.parent.name == "cur":
                raise OSError("stale handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_fails)
        assert queue.recover_stale(ttl_s=60) == 0

    def test_recover_stale_logs_a_move_it_cannot_make(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        queue.enqueue(_job("1"))
        assert queue.claim() is not None

        def replace_fails(_src, _dst):
            raise OSError("cross-device link")

        monkeypatch.setattr(os, "replace", replace_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.file_queue"):
            assert queue.recover_stale(None) == 0
        assert "recover_stale" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_poison_file_that_cannot_be_quarantined_is_logged(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        queue = FileInboxQueue(tmp_path)
        new = tmp_path / "new"
        new.mkdir(parents=True, exist_ok=True)
        poison = new / "1.json"
        poison.write_text("{not json", encoding="utf-8")

        def replace_fails(_src, _dst):
            raise OSError("permission denied")

        monkeypatch.setattr(os, "replace", replace_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.file_queue"):
            queue.claim()
        assert "could not quarantine" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestReadOnlyObserversNeverRaise:
    """The TUI polls these once a second. A queue directory that is being synced,
    pruned, or has never existed must read as empty, not as a crashed dashboard."""

    def test_depth_of_a_directory_that_cannot_be_listed_is_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def glob_fails(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", glob_fails)
        depth = read_queue_depth(tmp_path)
        assert (depth.new, depth.running, depth.done, depth.failed) == (0, 0, 0, 0)

    def test_rows_from_a_directory_that_cannot_be_listed_are_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        (tmp_path / "cur").mkdir(parents=True)
        (tmp_path / "new").mkdir(parents=True)

        def glob_fails(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", glob_fails)
        assert read_queue_rows(tmp_path) == []

    def test_a_row_file_that_cannot_be_read_still_lists(self, tmp_path: Path) -> None:
        """The id comes from the filename, so a job with an unreadable body is still
        worth showing: the operator can see something is queued."""
        new = tmp_path / "new"
        new.mkdir(parents=True)
        (new / "1755000000000000000-abcdef12.json").write_text("{not json")
        rows = read_queue_rows(tmp_path)
        assert len(rows) == 1
        assert not rows[0].prompt

    def test_a_row_whose_json_is_not_a_mapping_still_lists(
        self, tmp_path: Path
    ) -> None:
        new = tmp_path / "new"
        new.mkdir(parents=True)
        (new / "1755000000000000000-abcdef12.json").write_text('["a list"]')
        rows = read_queue_rows(tmp_path)
        assert len(rows) == 1
        assert not rows[0].prompt


class TestEnqueueTimeFromTheJobId:
    """The enqueue time is already in the id, so the TUI needs no stat() syscall,
    and unlike an mtime it survives a copy or a `touch`. Anything unparseable has to
    read as "unknown" rather than raising into a dashboard refresh."""

    def test_a_real_id_yields_its_timestamp(self) -> None:
        parsed = file_queue._enqueued_at("1755000000000000000-abcdef12")
        assert parsed is not None
        assert parsed.tzinfo is UTC
        assert parsed.year == 2025

    def test_a_non_numeric_head_is_unknown(self) -> None:
        assert file_queue._enqueued_at("notanumber-abcdef12") is None

    def test_a_zero_or_negative_stamp_is_unknown(self) -> None:
        """A 0 would render as 1970, which looks like real data."""
        assert file_queue._enqueued_at("0-abcdef12") is None
        assert file_queue._enqueued_at("-5-abcdef12") is None

    def test_a_stamp_beyond_the_representable_range_is_unknown(self) -> None:
        assert file_queue._enqueued_at(f"{10**30}-abcdef12") is None


def test_list_unfinished_on_a_missing_maildir_reads_as_empty(tmp_path: Path) -> None:
    """Unlike every write method here, this is a pure read: a missing maildir must
    not be built on the spot just because someone opened the jobs tab."""
    queue = FileInboxQueue(tmp_path / "never-created")
    assert queue.list_unfinished() == []
    assert not (tmp_path / "never-created").exists()
