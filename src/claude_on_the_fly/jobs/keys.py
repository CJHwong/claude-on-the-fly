"""Turning a job key into something safe to put in a path.

A job key is whatever a producer chose to identify one unit of work: `jira/ACE-1234`,
`owner/repo#42`, an entry name. Two places then use it as a single filename
component — the workspace directory a keyed job resumes in, and the queue's job
id — so it has to survive both without changing meaning.

The charset deliberately excludes `.`, which the queue's archive naming makes
load-bearing: `done/` distinguishes `<id>.json` from `<id>.result.json` and
`<id>.delivered.json` by suffix, so a key allowed to contain dots could produce
an id whose job file is indistinguishable from another id's result file.

Sanitizing is many-to-one (`a/b` and `a?b` both become `a_b`), so two different
keys can collide into one segment. That is deliberate: a collision costs a shared
dedup slot, while the alternative — encoding to stay injective — costs
readability in every log line and directory listing for a case that does not
arise when keys come from tracker identifiers.

Runs of unsafe characters collapse to a *single* `_`, which is load-bearing
rather than cosmetic: it means a sanitized segment can never contain `__`, so the
queue can join an entry name and an item key with `__` and split them back apart
unambiguously. Without the collapse, an entry called `jira` could not be told
from one called `jira_extra` when counting what each has outstanding.
"""

from __future__ import annotations

import hashlib
import re

# `_` counts as unsafe so that a run mixing it with other unsafe characters
# (`a/_b`) collapses to one `_` rather than into two adjacent replacements.
_UNSAFE = re.compile(r"[^A-Za-z0-9-]+")

# Room for the `<time_ns>-` prefix (20), a `__` separator, an entry name, and the
# longest archive suffix (`.delivered.json`, 15) inside the 255-byte filename
# limit every filesystem here honors.
MAX_SEGMENT = 96
# Long enough that a truncation collision needs ~4 billion keys sharing a prefix.
_DIGEST_CHARS = 8


def safe_segment(value: str, max_len: int = MAX_SEGMENT) -> str:
    """`value` reduced to one path/filename component.

    Over-long input is truncated and given a digest of the *original* so the
    result stays deterministic and distinct: two keys sharing a 96-character
    prefix must not land on the same segment just because they were both cut.
    """
    cleaned = _UNSAFE.sub("_", value)
    if len(cleaned) <= max_len:
        return cleaned
    digest = hashlib.blake2b(value.encode(), digest_size=8).hexdigest()[:_DIGEST_CHARS]
    return f"{cleaned[: max_len - _DIGEST_CHARS - 1]}-{digest}"


def split_key(key: str) -> tuple[str, str]:
    """A job key as its `(entry, item)` halves.

    Producers name work `<entry>/<item>` — the cron entry that found it, then the
    thing itself. A key with no `/` is all entry and no item, which is what a
    plain scheduled prompt looks like: the entry IS the unit of work.
    """
    entry, _, item = key.partition("/")
    return entry, item


def queue_filename(job_id: str, key: str | None) -> str:
    """The `new/`/`cur/` filename for a job.

    An unkeyed job keeps the bare `<id>.json` every job used before keys existed.
    A keyed one appends its entry and item, so answering "is this item already
    queued?" and "how many of this entry's items are outstanding?" is a glob over
    two directories with no file reads at all — which matters because the producer
    asks both on every fire.

    `__` separates the three parts and cannot occur inside any of them: sanitizing
    collapses unsafe runs to a single `_`, and the id is `<digits>-<hex>`. So
    `split("__")` recovers them exactly, however many `-` or single `_` the entry
    and item contain.
    """
    if key is None:
        return f"{job_id}.json"
    entry, item = split_key(key)
    return f"{job_id}__{safe_segment(entry)}__{safe_segment(item)}.json"


def job_id_from_filename(name: str) -> str:
    """The job id out of a `new/`/`cur/` filename, keyed or not.

    The inverse of `queue_filename`'s first component. Callers that want the id
    must go through this rather than reaching for `Path.stem`, which on a keyed
    file is the id *plus* its entry and item.
    """
    stem = name[: -len(".json")] if name.endswith(".json") else name
    return stem.split("__", 1)[0]


def filename_glob(entry: str, item: str | None = None) -> str:
    """Glob matching one entry's queued files, or one specific item's.

    Used for the two producer-side questions: dedup (`item` given) and the
    `max_concurrent` cap (`item` omitted).
    """
    if item is None:
        return f"*__{safe_segment(entry)}__*.json"
    return f"*__{safe_segment(entry)}__{safe_segment(item)}.json"
