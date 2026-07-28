"""Background jobs that survive a chat turn.

A worker-neutral use-case (`claim → run → complete → notify`) over three ports —
`JobQueue`, `AgentRunner`, `Notifier` — with default adapters that ship with cof:
a file-backed queue, an `agent.run` wrapper, and a Slack-thread notifier.
"""

from claude_on_the_fly.jobs.core import (
    AgentRunner,
    Job,
    JobQueue,
    Notifier,
    Result,
)

__all__ = [
    "AgentRunner",
    "Job",
    "JobQueue",
    "Notifier",
    "Result",
]
