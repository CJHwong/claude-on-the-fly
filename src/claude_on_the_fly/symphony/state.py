"""In-memory orchestrator state. Only the orchestrator mutates; workers report via release().

Entries are keyed by the composite `<source>:<id>` (see `Issue.key` / `make_key`)
so the same raw id from two trackers can't collide.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from .tracker.issue import Issue, make_key


@dataclass
class RunningEntry:
    issue_id: str  # raw tracker-internal id (Jira: "10042")
    issue_identifier: str  # human key (Jira: "PROJ-1133"; GitHub: "owner/repo#123")
    issue_state: str
    started_at: float  # monotonic seconds
    source: str = "jira"  # tracker kind that minted issue_id
    task: asyncio.Task[None] | None = None  # set after task creation
    workspace: Path | None = None  # set when worker creates it
    last_turn_end_at: float | None = (
        None  # monotonic seconds; None until first turn ends
    )
    failure_attempt: int = 0  # consecutive failures before this dispatch

    @property
    def key(self) -> str:
        return make_key(self.source, self.issue_id)


class OrchestratorState:
    def __init__(self) -> None:
        self._running: dict[str, RunningEntry] = {}

    def is_claimed(self, key: str) -> bool:
        """Caller passes the composite `<source>:<id>` key."""
        return key in self._running

    def claim(self, issue: Issue) -> RunningEntry:
        """Reserve the issue for dispatch. The task and workspace fields are filled later."""
        if issue.key in self._running:
            raise RuntimeError(f"issue {issue.identifier} already claimed")
        entry = RunningEntry(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            issue_state=issue.state,
            started_at=time.monotonic(),
            source=issue.source,
        )
        self._running[issue.key] = entry
        return entry

    def release(self, key: str) -> None:
        self._running.pop(key, None)

    def update_running_state(self, key: str, new_state: str) -> None:
        entry = self._running.get(key)
        if entry is not None:
            entry.issue_state = new_state

    def mark_turn_end(self, key: str) -> None:
        entry = self._running.get(key)
        if entry is not None:
            entry.last_turn_end_at = time.monotonic()

    def running_count(self) -> int:
        return len(self._running)

    def running_by_state(self, state: str) -> int:
        return sum(1 for e in self._running.values() if e.issue_state == state)

    def all_running(self) -> list[RunningEntry]:
        return list(self._running.values())

    def get_running(self, key: str) -> RunningEntry | None:
        return self._running.get(key)
