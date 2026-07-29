"""Queue registry — the single factory both producer and worker call.

`make_queue()` builds the `JobQueue` named by `JOBS_QUEUE_KIND` (default
`file`). The producer (`slack.py`) and the worker (`cli.py`) both go through it,
so they always agree on which queue they are talking to. Mirrors the
`SUPPORTED_TRACKERS` seam.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from claude_on_the_fly import agent
from claude_on_the_fly.jobs.core import JobQueue
from claude_on_the_fly.jobs.file_queue import FileInboxQueue

# kind name -> factory building a JobQueue over a root directory. Each adapter
# satisfies the JobQueue Protocol structurally.
SUPPORTED_QUEUES: dict[str, Callable[[Path], JobQueue]] = {
    "file": FileInboxQueue,
}

# ATTACH POINT (not built in v1): a Python entry-points group
# `claude_on_the_fly.job_queues` would be loaded into SUPPORTED_QUEUES here so a
# third-party adapter (e.g. a Desk-backed queue) can register its kind without
# editing this file — then selected via JOBS_QUEUE_KIND. This is the upstream
# plugin-registration seam.


def make_queue(root: Path | None = None) -> JobQueue:
    """Construct the queue named by `JOBS_QUEUE_KIND` (default `file`).

    Raises `ValueError` on an unknown kind. `root` overrides the default
    `<DATA_DIR>/jobs` location (used by tests and the enqueue CLI).
    """
    kind = os.environ.get("JOBS_QUEUE_KIND", "file").lower()
    factory = SUPPORTED_QUEUES.get(kind)
    if factory is None:
        raise ValueError(
            f"JOBS_QUEUE_KIND={kind!r} unsupported. "
            f"Available: {sorted(SUPPORTED_QUEUES)}"
        )
    return factory(root or (agent.DATA_DIR / "jobs"))
