"""jobs.core: frozen data types, opaque origin, ports, and the stdlib-only gate."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import claude_on_the_fly.jobs.core as core
from claude_on_the_fly.jobs.core import (
    AgentRunner,
    Job,
    JobQueue,
    Notifier,
    Result,
)

# Roots the clean core is allowed to import. Anything else — an I/O SDK, `agent`,
# Slack — is a clean-arch leak and fails the gate below.
_ALLOWED_CORE_ROOTS = {"__future__", "dataclasses", "typing"}


def _imported_modules(module_path: Path) -> set[str]:
    """Absolute module names imported by a source file, via AST (no execution)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def test_core_imports_only_stdlib() -> None:
    """The core must not reach for any I/O SDK, `agent`, or Slack — the
    dependency surface is a machine-checkable invariant, not a comment."""
    mods = _imported_modules(Path(core.__file__))
    roots = {m.split(".")[0] for m in mods}
    assert roots <= _ALLOWED_CORE_ROOTS, f"core imports outside stdlib: {roots}"


def test_job_is_frozen() -> None:
    job = Job(id="j1", prompt="do it", origin={"channel": "C1"})
    with pytest.raises(Exception):
        job.id = "j2"  # type: ignore[misc]


def test_result_is_frozen() -> None:
    result = Result(ok=True, text="done")
    with pytest.raises(Exception):
        result.ok = False  # type: ignore[misc]


def test_origin_is_opaque_passthrough() -> None:
    """Core stores `origin` verbatim; it never reads or reshapes it."""
    origin = {"channel": "C1", "thread_ts": "1699.5", "sender_id": "U9", "n": 3}
    job = Job(id="j1", prompt="p", origin=origin)
    assert job.origin == origin


class _FakeQueue:
    def enqueue(self, job: Job) -> None: ...
    def claim(self) -> Job | None:
        return None

    def complete(self, job: Job, result: Result) -> None: ...
    def recover_stale(self, ttl_s: float | None) -> int:
        return 0


class _FakeRunner:
    async def run(self, prompt: str) -> Result:
        return Result(ok=True, text=prompt)


class _FakeNotifier:
    async def notify(self, origin: dict, result: Result) -> None: ...


def test_ports_are_runtime_checkable() -> None:
    assert isinstance(_FakeQueue(), JobQueue)
    assert isinstance(_FakeRunner(), AgentRunner)
    assert isinstance(_FakeNotifier(), Notifier)


def test_recover_stale_is_on_the_queue_port() -> None:
    """Finding 1: recover_stale is a first-class port method, so a queue missing
    it does not satisfy the Protocol (the worker depends only on the port)."""

    class _NoRecover:
        def enqueue(self, job: Job) -> None: ...
        def claim(self) -> Job | None:
            return None

        def complete(self, job: Job, result: Result) -> None: ...

    assert not isinstance(_NoRecover(), JobQueue)
