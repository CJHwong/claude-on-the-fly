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

Filenames in `new/` and `cur/` are `<id>.json` for an unkeyed job and
`<id>__<entry>__<item>.json` for a keyed one (`keys.queue_filename`). The key is
in the *name* so that `count_unfinished` can answer a producer's dedup and
concurrency questions with two globs and zero file reads — it is asked on every
poll, and reading every queued job to answer it would put the cost of the whole
queue on every fire.

The id is unchanged either way, and stays the first component, so `sorted()`
remains oldest-first and `_enqueued_at` still reads the enqueue time straight out
of the name. Anything wanting the id from a filename goes through
`keys.job_id_from_filename`; `Path.stem` is the id *plus* its key on a keyed file.

`done/` deliberately does not follow the scheme: `complete()` archives to a bare
`<id>.json` so `undelivered()` can pair a job with its `<id>.result.json` by id
alone. Keys matter while work is outstanding, which by then it is not.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_on_the_fly.jobs.core import Delivery, Job, QueueRow, Result
from claude_on_the_fly.jobs.keys import (
    filename_glob,
    job_id_from_filename,
    queue_filename,
)

logger = logging.getLogger(__name__)

# Cap on how many queued jobs an observer parses per read. A deep queue must not
# turn a 1Hz dashboard tick into hundreds of file reads.
DEFAULT_ROW_LIMIT = 20
# How long a completed job's record and reply stay in `done/`. Shorter than the
# log and workspace windows (30 days) on purpose: this is one JSON record per
# finished job, scanned in full on every completion, so the cost of widening it
# is paid by the hot path rather than by the disk.
DONE_RETENTION_S = 7 * 24 * 60 * 60
# How long an undelivered result stays worth re-posting. A permanently
# undeliverable one — archived channel, revoked token — must not be retried on
# every start until the archive prunes it, and a day-old reply is stale anyway.
DELIVERY_RETRY_WINDOW_S = 24 * 60 * 60
# Longest prompt an observer keeps in memory. The dashboard renders a short
# preview, so retaining a multi-megabyte prompt for the life of the process buys
# nothing; the cap is far above any prompt a preview could show.
PROMPT_PREVIEW_LIMIT = 500


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional(value: object, *types: type) -> bool:
    """Whether `value` is None or one of `types`. `bool` never counts as a
    number, so a `timeout` of `true` is poison rather than a one-second limit."""
    if value is None:
        return True
    if isinstance(value, bool) and bool not in types:
        return False
    return isinstance(value, types)


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
            "key": job.key,
            "session_key": job.session_key,
            "timeout": job.timeout,
            "platform": job.platform,
            "profile": job.profile,
        }
        name = queue_filename(job.id, job.key)
        staging = self._tmp / name
        staging.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(staging, self._new / name)

    def count_unfinished(self, entry: str, item: str | None = None) -> int:
        """How many of `entry`'s jobs are queued or in flight; one item's if given.

        The producer's two admission questions in one call: `item` given answers
        "is this one already here?" (skip it) and `item` omitted answers "how many
        of mine are outstanding?" (respect the cap).

        Answered by globbing the two directories, so the queue's own contents are
        the only source of truth — no side file to drift out of step when a worker
        is SIGKILLed mid-job, which is exactly when a producer must not conclude
        that an item is still running forever.

        Deliberately not memoized, unlike the observer reads below: this one gates
        a write, and serving it a stale answer double-enqueues.
        """
        pattern = filename_glob(entry, item)
        return _count(self._new, pattern) + _count(self._cur, pattern)

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
            os.replace(
                self._cur / queue_filename(job.id, job.key),
                self._done / f"{job.id}.json",
            )
        except FileNotFoundError:
            logger.warning("jobs: complete: %s not in cur/ (already moved?)", job.id)
        self._prune_archive()

    def _prune_archive(self) -> None:
        """Drop archived jobs older than the retention window.

        `done/` holds each job's prompt and the agent's full reply verbatim, and
        nothing else ever removes them, so an unpruned archive grows without
        bound in both disk and the cost of the observers that count it. Age, not
        count: a burst of small jobs should not evict the one from this morning
        somebody still wants to read. The 7-day window matches the log retention
        in `jobs/cli._setup_logging`, so the archive and the logs covering it
        expire together.

        Runs on completion, not on a timer — once per job is rare enough for an
        O(archive) scan, and it means a worker that stops never leaves a growing
        directory behind.
        """
        cutoff = time.time() - DONE_RETENTION_S
        for path in self._done.glob("*.json"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
            except OSError as exc:  # racing reader, or a permissions problem
                logger.debug("jobs: could not prune %s: %s", path.name, exc)

    def mark_delivered(self, job_id: str) -> None:
        """Drop a marker beside the archived result.

        A sibling file rather than a flag inside the result: the result is
        written once and never touched again, so a marker cannot corrupt it,
        and its absence is what makes an interrupted delivery visible. The
        `.json` suffix keeps it inside `_prune_archive`'s sweep, and it does not
        match the `*.result.json` the depth count uses.
        """
        self._ensure_tree()
        try:
            (self._done / f"{job_id}.delivered.json").write_text(
                json.dumps({"delivered_at": _utcnow_iso()}), encoding="utf-8"
            )
        except OSError as exc:
            # Worst case the reply is delivered twice after a restart, which
            # beats failing a job whose work is already done.
            logger.warning("jobs: could not mark %s delivered: %s", job_id, exc)

    def undelivered(self) -> list[Delivery]:
        """Completed results with no delivery marker, oldest first.

        Bounded by `DELIVERY_RETRY_WINDOW_S`: a permanently undeliverable
        result — the channel was archived, the token revoked — would otherwise
        be retried on every start until the archive prunes it, and a reply old
        enough is not worth posting anyway.
        """
        self._ensure_tree()
        cutoff = time.time() - DELIVERY_RETRY_WINDOW_S
        pending: list[Delivery] = []
        for result_path in sorted(self._done.glob("*.result.json")):
            job_id = result_path.name[: -len(".result.json")]
            if (self._done / f"{job_id}.delivered.json").exists():
                continue
            try:
                if result_path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            delivery = self._load_delivery(job_id, result_path)
            if delivery is not None:
                pending.append(delivery)
        return pending

    def _load_delivery(self, job_id: str, result_path: Path) -> Delivery | None:
        """Pair an archived result with the origin from its job file, or None if
        either cannot be read — there is nowhere to deliver a result whose
        origin is gone."""
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            job_data = json.loads(
                (self._done / f"{job_id}.json").read_text(encoding="utf-8")
            )
            origin = job_data["origin"]
            result = Result(ok=bool(result_data["ok"]), text=str(result_data["text"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("jobs: cannot redeliver %s: %s", job_id, exc)
            return None
        if not isinstance(origin, dict):
            return None
        return Delivery(job_id=job_id, origin=origin, result=result)

    def list_unfinished(self, limit: int = DEFAULT_ROW_LIMIT) -> list[QueueRow]:
        """The unfinished jobs, newest-claimed first. Delegates to the
        module-level reader below, so this stays a pure read: unlike every
        write method here it does not `_ensure_tree()`, and a missing maildir
        reads as an empty queue rather than being built on the spot."""
        return read_queue_rows(self._root, limit)

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
            # The five dispatch fields are read with `.get` defaults rather than
            # required: a job enqueued before they existed, or by a producer that
            # does not care about any of them, is an ordinary unkeyed one-shot
            # and not poison. The same tolerance runs the other way, and it is
            # worth knowing about: a worker older than the field ignores a
            # `profile` it does not understand and runs the daemon default, so a
            # mixed-version rollout downgrades the model rather than failing.
            job = Job(
                id=data["id"],
                prompt=data["prompt"],
                origin=data["origin"],
                key=data.get("key"),
                session_key=data.get("session_key"),
                timeout=data.get("timeout"),
                platform=data.get("platform") or "jobs",
                profile=data.get("profile"),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("jobs: poison job file %s → failed/ (%s)", path.name, exc)
            self._quarantine(path)
            return None
        if (
            not isinstance(job.id, str)
            or not isinstance(job.prompt, str)
            or not isinstance(job.origin, dict)
            or not isinstance(job.platform, str)
            or not _optional(job.key, str)
            or not _optional(job.session_key, str)
            or not _optional(job.timeout, int, float)
            or not _optional(job.profile, str)
        ):
            logger.warning(
                "jobs: malformed job %s → failed/ (bad field types)", path.name
            )
            self._quarantine(path)
            return None
        if job.id != job_id_from_filename(path.name):
            logger.warning(
                "jobs: job id %r != filename %r → failed/", job.id, path.name
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


# QueueRow is defined in `core` (it is the JobQueue port's return type) and
# re-exported here, where every reader of this maildir already looks. In this
# adapter `enqueued_at` comes from the id rather than the file — see
# `_enqueued_at`.


# Memoize archive counts by (dir, pattern) -> (mtime_ns, count) so the 1Hz
# dashboard refresh doesn't re-walk done/ on every tick. Exact for this queue,
# which only ever adds/removes whole files — never for in-place edits, which
# never happen here — and only to the resolution the filesystem stamps mtimes
# at: where that is a whole second rather than a nanosecond, a second change
# inside the same second reads stale until the next one.
#
# Bounded, because these are process-global and nothing else evicts them: a
# caller that varies `root` grows them for the life of the process. Two
# directories are counted per read, so the cap only has to sit above that.
_MAX_COUNT_MEMOS = 8
_archive_count_cache: dict[tuple[Path, str], tuple[int, int]] = {}

# Memoize the unfinished-row read by (root, limit) -> (cur mtime_ns, new
# mtime_ns, rows). Same argument, applied to the reader that actually opens
# files rather than merely counting them — and this one pins the rows, prompts
# included, so it is capped tighter.
_MAX_ROW_MEMOS = 2
_rows_cache: dict[tuple[Path, int], tuple[int, int, list[QueueRow]]] = {}


def _store_memo(cache: dict, key, value, limit: int) -> None:
    """Record `value` under `key`, clearing the cache first if it is full.

    Clear-and-refill rather than LRU eviction: these memos exist to spare a 1Hz
    reader some directory walks, and the only cost of dropping one is the walk
    it would have skipped. A real eviction policy would be more machinery than
    the thing it protects.
    """
    if len(cache) >= limit and key not in cache:
        cache.clear()
    cache[key] = value


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
    _store_memo(_archive_count_cache, key, (mtime_ns, count), _MAX_COUNT_MEMOS)
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
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _read_row_fields(path: Path) -> tuple[str | None, dict]:
    """The job's prompt head and its origin, from one read of the file.

    ``(None, {})`` if the file vanished (claimed mid-read) or does not parse —
    each field degrades on its own, so a hand-mangled record costs one cell
    rather than the whole listing.

    The prompt is truncated here rather than at render time: the row it lands in
    is memoized for as long as the queue holds still, so returning the whole
    string pins every queued prompt in the observing process — and a prompt has
    no size limit, being whatever somebody typed or pasted into a chat message.
    The origin is small, flat, and the only thing a caller can filter a listing
    by, so it is carried whole.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    prompt = data.get("prompt")
    origin = data.get("origin")
    return (
        prompt[:PROMPT_PREVIEW_LIMIT] if isinstance(prompt, str) else None,
        origin if isinstance(origin, dict) else {},
    )


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
    _store_memo(_rows_cache, key, (cur_ns, new_ns, rows), _MAX_ROW_MEMOS)
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
            job_id = job_id_from_filename(path.name)
            if job_id in seen:
                continue
            seen.add(job_id)
            prompt, origin = _read_row_fields(path)
            rows.append(
                QueueRow(
                    id=job_id,
                    prompt=prompt,
                    origin=origin,
                    enqueued_at=_enqueued_at(job_id),
                    in_flight=in_flight,
                )
            )
    return rows
