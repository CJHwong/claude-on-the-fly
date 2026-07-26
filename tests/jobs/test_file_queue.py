"""FileInboxQueue: FIFO claim, no double-claim, restart survival, recovery,
poison isolation, and opaque origin round-trip — plus the read-only observers
(`read_queue_depth` / `read_queue_rows`) the TUI's jobs tab reads through."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from claude_on_the_fly.jobs.core import Job, Result
from claude_on_the_fly.jobs.file_queue import (
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
    assert row.enqueued_at == datetime.fromtimestamp(
        minted_ns / 1_000_000_000, tz=timezone.utc
    )


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
    real_read_prompt = fq._read_prompt

    def _spy(path: Path) -> str | None:
        opened.append(path)
        return real_read_prompt(path)

    monkeypatch.setattr(fq, "_read_prompt", _spy)

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
