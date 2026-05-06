"""In-memory orchestrator state. Only the orchestrator mutates; workers report via release()."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .tracker.issue import Issue
from .workspace import Workspace


@dataclass
class RunningEntry:
    issue_id: str
    issue_identifier: str
    issue_state: str
    started_at: float  # monotonic seconds
    task: asyncio.Task[None] | None = None  # set after task creation
    workspace: Workspace | None = None  # set when worker creates it
    last_turn_end_at: float | None = (
        None  # monotonic seconds; None until first turn ends
    )
    failure_attempt: int = 0  # consecutive failures before this dispatch


class OrchestratorState:
    def __init__(self) -> None:
        self._running: dict[str, RunningEntry] = {}

    def is_claimed(self, issue_id: str) -> bool:
        return issue_id in self._running

    def claim(self, issue: Issue) -> RunningEntry:
        """Reserve the issue for dispatch. The task and workspace fields are filled later."""
        if issue.id in self._running:
            raise RuntimeError(f"issue {issue.identifier} already claimed")
        entry = RunningEntry(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            issue_state=issue.state,
            started_at=time.monotonic(),
        )
        self._running[issue.id] = entry
        return entry

    def release(self, issue_id: str) -> None:
        self._running.pop(issue_id, None)

    def update_running_state(self, issue_id: str, new_state: str) -> None:
        entry = self._running.get(issue_id)
        if entry is not None:
            entry.issue_state = new_state

    def mark_turn_end(self, issue_id: str) -> None:
        entry = self._running.get(issue_id)
        if entry is not None:
            entry.last_turn_end_at = time.monotonic()

    def running_count(self) -> int:
        return len(self._running)

    def running_by_state(self, state: str) -> int:
        return sum(1 for e in self._running.values() if e.issue_state == state)

    def all_running(self) -> list[RunningEntry]:
        return list(self._running.values())

    def get_running(self, issue_id: str) -> RunningEntry | None:
        return self._running.get(issue_id)
