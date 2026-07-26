"""Default `JobQueue` — a maildir-style file inbox under one directory.

Layout (all under `root`):

    tmp/     staging for a partial write before it is atomically published
    new/     unclaimed jobs, FIFO by time-sortable id
    cur/     in-flight (claimed, not yet completed)
    done/    completed — the job file plus its `<id>.result.json`, kept for audit
    failed/  poison (unparseable / id-mismatch) — quarantined, never re-looped

The claim is a POSIX `rename(2)` from `new/` to `cur/`: atomic within one
filesystem, so two workers racing for the same file resolve to exactly one
winner (the loser's rename raises `FileNotFoundError` and is skipped) with no
lock files. A crashed worker leaves its job in `cur/`; `recover_stale` moves it
back to `new/` on the next start — at-least-once execution, so jobs must be safe
to re-run.

Single-filesystem assumption: `root` and its subdirs must live on one
filesystem for the rename to stay atomic. The directory tree is created
lazily on first use, so merely constructing the queue (e.g. inside a Slack
frontend's `__init__`) has no filesystem side effect.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_on_the_fly.jobs.core import Job, Result

logger = logging.getLogger(__name__)

# Cap on how many queued jobs an observer parses per read. A deep queue must not
# turn a 1Hz dashboard tick into hundreds of file reads.
DEFAULT_ROW_LIMIT = 20


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FileInboxQueue:
    """File-backed `JobQueue`. Pass the directory that holds the maildir tree."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._tmp = root / "tmp"
        self._new = root / "new"
        self._cur = root / "cur"
        self._done = root / "done"
        self._failed = root / "failed"

    def _ensure_tree(self) -> None:
        """Create the maildir subdirs. Idempotent; called before every op so a
        `rename` never fails on a missing target dir."""
        for directory in (self._tmp, self._new, self._cur, self._done, self._failed):
            directory.mkdir(parents=True, exist_ok=True)

    # -- producer -----------------------------------------------------------

    def enqueue(self, job: Job) -> None:
        """Publish a job. Written to `tmp/` first, then atomically moved into
        `new/`, so `new/*.json` never sees a partial write."""
        self._ensure_tree()
        payload = {
            "id": job.id,
            "prompt": job.prompt,
            "origin": job.origin,
            "enqueued_at": _utcnow_iso(),
        }
        staging = self._tmp / f"{job.id}.json"
        staging.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(staging, self._new / f"{job.id}.json")

    # -- worker -------------------------------------------------------------

    def claim(self) -> Job | None:
        """Take the oldest runnable job, or None. Ids are time-sortable, so
        `sorted(new/)` is FIFO. The atomic `new→cur` rename is the claim; a lost
        race (`FileNotFoundError`) or a poison file is skipped."""
        self._ensure_tree()
        for src in sorted(self._new.glob("*.json")):
            dest = self._cur / src.name
            try:
                os.rename(src, dest)
            except FileNotFoundError:
                continue  # another worker claimed it first
            job = self._load(dest)
            if job is not None:
                return job
            # poison: _load moved it to failed/; keep scanning for a real job
        return None

    def complete(self, job: Job, result: Result) -> None:
        """Record the result then archive the job file. Result is written first
        (tmp+replace) so a crash between the two leaves a durable result in
        `done/`; the job file move follows."""
        self._ensure_tree()
        result_payload = {
            "ok": result.ok,
            "text": result.text,
            "completed_at": _utcnow_iso(),
        }
        result_path = self._done / f"{job.id}.result.json"
        tmp = result_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result_payload), encoding="utf-8")
        os.replace(tmp, result_path)
        try:
            os.replace(self._cur / f"{job.id}.json", self._done / f"{job.id}.json")
        except FileNotFoundError:
            logger.warning("jobs: complete: %s not in cur/ (already moved?)", job.id)

    def recover_stale(self, ttl_s: float | None) -> int:
        """Requeue in-flight jobs from `cur/` back to `new/`. `ttl_s=None`
        requeues all (single-worker default); a positive TTL requeues only those
        whose file mtime is older than it. Returns the count requeued. Poison
        files live in `failed/`, never `cur/`, so they are never re-looped."""
        self._ensure_tree()
        now = time.time()
        count = 0
        for src in sorted(self._cur.glob("*.json")):
            if ttl_s is not None:
                try:
                    age = now - src.stat().st_mtime
                except OSError:
                    continue
                if age < ttl_s:
                    continue
            try:
                os.replace(src, self._new / src.name)
            except OSError as exc:
                logger.warning("jobs: recover_stale: %s: %s", src.name, exc)
                continue
            count += 1
        return count

    # -- internals ----------------------------------------------------------

    def _load(self, path: Path) -> Job | None:
        """Parse a claimed file into a Job, or quarantine it and return None.

        A file is poison when it is unparseable, missing/typed-wrong fields, or
        its embedded id disagrees with the filename — the last matters because
        `complete` re-derives the path from `job.id`, so a mismatch is an
        otherwise-unrepresentable inconsistency waiting to lose the file.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = Job(id=data["id"], prompt=data["prompt"], origin=data["origin"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("jobs: poison job file %s → failed/ (%s)", path.name, exc)
            self._quarantine(path)
            return None
        if (
            not isinstance(job.id, str)
            or not isinstance(job.prompt, str)
            or not isinstance(job.origin, dict)
        ):
            logger.warning(
                "jobs: malformed job %s → failed/ (bad field types)", path.name
            )
            self._quarantine(path)
            return None
        if job.id != path.stem:
            logger.warning(
                "jobs: job id %r != filename %r → failed/", job.id, path.stem
            )
            self._quarantine(path)
            return None
        return job

    def _quarantine(self, path: Path) -> None:
        try:
            os.replace(path, self._failed / path.name)
        except OSError as exc:
            logger.warning("jobs: could not quarantine %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Read-only observers
#
# Deliberately module-level functions over a `root`, not `FileInboxQueue`
# methods: all four queue operations above (enqueue, claim, complete,
# recover_stale) open with `_ensure_tree()`, so a reader added as a sibling
# method would inherit five `mkdir`s by symmetry. As free functions there is
# nothing to inherit — an observer (the TUI's jobs tab) cannot create, move, or
# write anything, and a missing tree reads as an empty queue rather than being
# built on the spot.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueDepth:
    """How many jobs sit in each maildir stage, as of one read."""

    new: int
    running: int
    done: int
    failed: int


@dataclass(frozen=True)
class QueueRow:
    """One not-yet-finished job: in `cur/` (in_flight) or waiting in `new/`.

    `enqueued_at` comes from the id, not the file — see `_enqueued_at`. Both it
    and `prompt` are None when they could not be derived, so a half-written or
    hand-mangled file degrades one cell instead of failing the whole read.
    """

    id: str
    prompt: str | None
    enqueued_at: datetime | None
    in_flight: bool


# Memoize archive counts by (dir, pattern) -> (mtime_ns, count) so the 1Hz
# dashboard refresh doesn't re-walk an unbounded done/ on every tick. Exact for
# this queue, which only ever adds/removes whole files — never for in-place
# edits, which never happen here — and only to the resolution the filesystem
# stamps mtimes at: where that is a whole second rather than a nanosecond, a
# second change inside the same second reads stale until the next one.
_archive_count_cache: dict[tuple[Path, str], tuple[int, int]] = {}

# Memoize the unfinished-row read by (root, limit) -> (cur mtime_ns, new
# mtime_ns, rows). Same argument, applied to the reader that actually opens
# files rather than merely counting them.
_rows_cache: dict[tuple[Path, int], tuple[int, int, list[QueueRow]]] = {}


def _mtime_ns(directory: Path) -> int:
    """The directory's own mtime, or -1 when it is missing or unreadable — a
    value no real mtime takes, so a directory appearing later busts the memo."""
    try:
        return directory.stat().st_mtime_ns
    except OSError:
        return -1


def _count(directory: Path, pattern: str) -> int:
    """Number of entries matching `pattern`. A missing/unreadable directory is
    zero, never an error."""
    try:
        return sum(1 for _ in directory.glob(pattern))
    except OSError:
        return 0


def _count_memoized(directory: Path, pattern: str) -> int:
    """`_count`, recomputed only when the directory's own mtime moved."""
    key = (directory, pattern)
    try:
        mtime_ns = directory.stat().st_mtime_ns
    except OSError:
        _archive_count_cache.pop(key, None)
        return 0
    cached = _archive_count_cache.get(key)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    count = _count(directory, pattern)
    _archive_count_cache[key] = (mtime_ns, count)
    return count


def _enqueued_at(job_id: str) -> datetime | None:
    """When the id was minted, read out of the id itself.

    Producers build ids as `f"{time.time_ns()}-{uuid4().hex[:8]}"` (see
    `jobs/cli.py:_cmd_enqueue`), so the enqueue time is already in the name: no
    `stat()` syscall, and unlike a file mtime it survives a copy or a `touch`.
    An id that does not carry one yields None rather than raising.
    """
    head, _, _ = job_id.partition("-")
    try:
        ns = int(head)
    except ValueError:
        return None
    if ns <= 0:
        return None
    try:
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _read_prompt(path: Path) -> str | None:
    """The job's prompt, or None if the file vanished (claimed mid-read) or does
    not parse."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    prompt = data.get("prompt")
    return prompt if isinstance(prompt, str) else None


def read_queue_depth(root: Path) -> QueueDepth:
    """Count each stage of the maildir at `root` without touching it.

    `done` counts `*.result.json`, not `*.json`: `complete()` writes both
    `<id>.result.json` and `<id>.json` into `done/`, so counting `*.json` would
    report every finished job twice. The result file is the one written first
    and unconditionally, so it is the marker that a job finished exactly once.
    """
    return QueueDepth(
        new=_count(root / "new", "*.json"),
        running=_count(root / "cur", "*.json"),
        done=_count_memoized(root / "done", "*.result.json"),
        failed=_count_memoized(root / "failed", "*.json"),
    )


def read_queue_rows(root: Path, limit: int = DEFAULT_ROW_LIMIT) -> list[QueueRow]:
    """The unfinished jobs at `root`, in-flight first then queued, oldest first.

    Memoized on the `cur/` and `new/` mtimes, by the same argument as the
    archive counts above — and for a stronger reason: a row costs a file open
    and a `json.loads`, and the 1Hz dashboard refresh reads this whether or not
    the jobs tab is even visible. An idle tick therefore costs two `stat()`s
    instead of up to `limit` file reads. The stamps are sampled *before* the
    scan, so a file landing mid-read invalidates the memo rather than hiding
    behind it.

    Hard-capped at `limit` rows, which bounds the file *reads* — the expensive
    part, and the only unbounded one if left uncapped. (Listing and sorting the
    directories is still O(depth); the cap cannot be applied before the sort
    without losing oldest-first.) Ids are time-sortable, so sorting by filename
    is oldest-first.
    """
    if limit <= 0:
        return []
    cur, new = root / "cur", root / "new"
    key = (root, limit)
    cur_ns, new_ns = _mtime_ns(cur), _mtime_ns(new)
    cached = _rows_cache.get(key)
    if cached is not None and cached[0] == cur_ns and cached[1] == new_ns:
        return cached[2]
    rows = _scan_rows(cur, new, limit)
    _rows_cache[key] = (cur_ns, new_ns, rows)
    return rows


def _scan_rows(cur: Path, new: Path, limit: int) -> list[QueueRow]:
    """`read_queue_rows` without the memo.

    Ids are deduplicated, `cur/` winning. The two directories are listed one
    after the other, and the worker can move a file between them in between:
    `claim()` going new→cur just drops a row for one tick, but `recover_stale`
    going cur→new would otherwise return the same id twice — and a caller
    keying rows by id (the TUI's DataTable) raises on the duplicate. The
    contract is that this read never makes its caller fail.
    """
    rows: list[QueueRow] = []
    seen: set[str] = set()
    for directory, in_flight in ((cur, True), (new, False)):
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if len(rows) >= limit:
                return rows
            if path.stem in seen:
                continue
            seen.add(path.stem)
            rows.append(
                QueueRow(
                    id=path.stem,
                    prompt=_read_prompt(path),
                    enqueued_at=_enqueued_at(path.stem),
                    in_flight=in_flight,
                )
            )
    return rows
