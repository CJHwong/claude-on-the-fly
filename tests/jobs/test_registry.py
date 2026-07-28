"""jobs.registry: SUPPORTED_QUEUES + make_queue dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_on_the_fly.jobs.core import JobQueue
from claude_on_the_fly.jobs.file_queue import FileInboxQueue
from claude_on_the_fly.jobs.registry import SUPPORTED_QUEUES, make_queue


def test_file_kind_registered() -> None:
    assert SUPPORTED_QUEUES["file"] is FileInboxQueue


def test_make_queue_default_is_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)
    from claude_on_the_fly import agent

    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    q = make_queue()
    assert isinstance(q, FileInboxQueue)
    assert isinstance(q, JobQueue)  # satisfies the port


def test_make_queue_uses_data_dir_jobs(monkeypatch, tmp_path: Path) -> None:
    from claude_on_the_fly import agent

    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    q = make_queue()
    # Producer and worker both land in <DATA_DIR>/jobs so they share one inbox.
    from claude_on_the_fly.jobs.core import Job

    q.enqueue(Job(id="1-a", prompt="p", origin={}))
    assert (tmp_path / "jobs" / "new" / "1-a.json").exists()


def test_make_queue_root_override(tmp_path: Path) -> None:
    root = tmp_path / "custom"
    q = make_queue(root=root)
    assert isinstance(q, FileInboxQueue)
    from claude_on_the_fly.jobs.core import Job

    q.enqueue(Job(id="1-a", prompt="p", origin={}))
    assert (root / "new" / "1-a.json").exists()


def test_make_queue_unknown_kind_raises(monkeypatch) -> None:
    monkeypatch.setenv("JOBS_QUEUE_KIND", "redis")
    with pytest.raises(ValueError, match="unsupported"):
        make_queue()


def test_make_queue_error_lists_available(monkeypatch) -> None:
    monkeypatch.setenv("JOBS_QUEUE_KIND", "nope")
    with pytest.raises(ValueError, match=r"Available: \['file'\]"):
        make_queue()
