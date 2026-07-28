"""Default `AgentRunner` — one-shot agent run reusing `agent.run`.

Each job is an independent one-shot: a fresh workspace and a fresh, deterministic
session uuid per call, so no prior transcript bleeds in (hence `"jobs"` is in
`agent.NO_HANDOFF_PLATFORMS`). Execution reuses the existing spawn-run-reap path
in `agent.run` / backend `_exec` — no new subprocess or kill code — so a graceful
shutdown that cancels the in-flight task lets `_exec`'s `finally` reap the whole
process tree within the supervisor's grace.

A handled agent failure becomes a `Result(ok=False, ...)`; `CancelledError`
(a `BaseException`, not caught here) still propagates so cancel-in-flight works.

Teardown runs in a worker thread so it cannot eat that grace — see
`_discard_workspace`.

NOTE (stdin inheritance): `agent._exec` spawns the CLI without passing `stdin=`,
so the child inherits this process's stdin. That is harmless for the supervised
worker — `supervisor.spawn` starts daemons with `stdin=subprocess.DEVNULL` — but
a worker run in the foreground from a terminal hands the agent CLI that terminal's
stdin, and it may consume input meant for the shell. Run the worker detached
(`claude-tui start jobs`), or redirect its stdin, if that matters to you.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import ClaudeUnavailableError, current_backend_key
from claude_on_the_fly.jobs.core import Result

logger = logging.getLogger(__name__)


def _discard_workspace(workspace: Path) -> None:
    """Remove a finished job's workspace. Blocking; call it off the event loop."""
    shutil.rmtree(workspace, ignore_errors=True)


@dataclass
class OrchestratorAgentRunner:
    """`AgentRunner` that drives `agent.run` in a fresh per-job workspace.

    `data_dir` roots the throwaway workspaces (`<data_dir>/workspaces/jobs/<run>`).
    `user_name` / `channel_context` / `timeout` are passed straight to
    `agent.run`. The composition root (`cli.py`) fills `timeout` from env; the
    other two keep the defaults below.
    """

    data_dir: Path
    timeout: float | None = agent.DEFAULT_TIMEOUT
    user_name: str = "jobs"
    channel_context: str = "jobs"

    async def run(self, prompt: str) -> Result:
        run_id = uuid4().hex
        workspace = self.data_dir / "workspaces" / "jobs" / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            agent.ensure_persona(workspace)
            session_uuid = str(
                uuid5(NAMESPACE_URL, f"jobs/{current_backend_key()}/{run_id}")
            )
            try:
                response = await agent.run(
                    workspace=workspace,
                    session_uuid=session_uuid,
                    prompt=prompt,
                    platform="jobs",
                    user_name=self.user_name,
                    channel_context=self.channel_context,
                    timeout=self.timeout,
                )
            except ClaudeUnavailableError as exc:
                logger.warning("jobs: agent unavailable: %s", exc)
                return Result(ok=False, text=f"Claude is unavailable: {exc}")
            except Exception as exc:  # NOT BaseException — CancelledError propagates
                logger.exception("jobs: agent run failed")
                return Result(ok=False, text=f"Job failed: {exc}")
            return Result(ok=True, text=response.body)
        finally:
            # Each job gets a throwaway workspace; a long-lived worker would grow
            # data_dir/workspaces/jobs without bound otherwise. `finally` cleans up
            # on success, on handled failure, and while CancelledError unwinds —
            # the await neither catches nor masks that propagation.
            #
            # Off the event loop, because this is on the shutdown path: an agent
            # that cloned a repo leaves a tree whose rmtree takes seconds, and
            # blocking the loop here spends the supervisor's 5s grace on file
            # deletion — the same window the in-flight cancel needs to reap the
            # agent's process tree. Blowing it means a SIGKILLed worker and an
            # orphaned agent CLI. It also keeps the heartbeat coroutine fed, so
            # the dashboard does not flip to `broken` mid-cleanup.
            await asyncio.to_thread(_discard_workspace, workspace)
