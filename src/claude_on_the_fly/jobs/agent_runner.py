"""Default `AgentRunner` — one agent run per job, reusing `agent.run`.

A job with no `session_key` is an independent one-shot: a fresh workspace and a
fresh session uuid per call, discarded on the way out, so no prior transcript
bleeds in (hence `"jobs"` is in `agent.NO_HANDOFF_PLATFORMS`). That is every
Slack-triggered job.

A job that carries a `session_key` is one step of something longer — turn 2 on a
ticket a poller keeps returning — so its workspace and session uuid derive from
that key instead of a random id, and neither is discarded when the run ends. The
next job with the same key resumes the same transcript rather than re-deriving
the world from nothing. Both the workspace path and the session uuid include the
key, so nothing else has to be persisted to make the resume happen.

Execution reuses the existing spawn-run-reap path in `agent.run` / backend
`_exec` — no new subprocess or kill code — so a graceful shutdown that cancels
the in-flight task lets `_exec`'s `finally` reap the whole process tree within
the supervisor's grace.

A handled agent failure becomes a `Result(ok=False, ...)`; `CancelledError`
(a `BaseException`, not caught here) still propagates so cancel-in-flight works.

Teardown of an unkeyed job removes the workspace *and* the session directory the
active backend named after it (claude keeps one outside the workspace), and runs
in a worker thread so it cannot eat the shutdown grace — see
`_discard_workspace`. A keyed job skips teardown entirely; its workspace is the
continuity.

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
from claude_on_the_fly.jobs.core import Job, Result
from claude_on_the_fly.jobs.keys import safe_segment
from claude_on_the_fly.transcript import remove_workspace_sessions

logger = logging.getLogger(__name__)


def _discard_workspace(workspace: Path) -> None:
    """Remove a finished job's workspace and the session directory the backend
    keyed to it. Blocking; call it off the event loop.

    Session dirs go first, while the workspace path still resolves — see
    `transcript.remove_workspace_sessions`, which is what stops
    `~/.claude/projects/` from gaining a dead directory per job.
    """
    remove_workspace_sessions(workspace)
    shutil.rmtree(workspace, ignore_errors=True)


@dataclass
class OrchestratorAgentRunner:
    """`AgentRunner` that drives `agent.run` in a fresh per-job workspace.

    `data_dir` roots the throwaway workspaces (`<data_dir>/workspaces/jobs/<run>`).
    `user_name` / `channel_context` / `timeout` are passed straight to
    `agent.run`. The composition root (`cli.py`) fills `timeout` from `jobs.timeout`;
    the
    other two keep the defaults below.
    """

    data_dir: Path
    timeout: float | None = agent.DEFAULT_TIMEOUT
    user_name: str = "jobs"
    channel_context: str = "jobs"

    async def run(self, job: Job) -> Result:
        # A keyed job's run id IS its session key, which is what makes the
        # workspace and the session uuid below stable across runs. An unkeyed one
        # gets a random id it will never see again.
        keyed = job.session_key is not None
        run_id = safe_segment(job.session_key) if job.session_key else uuid4().hex
        workspace = self.data_dir / "workspaces" / job.platform / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            # Keyed on `job.key`, the same field that names the unit of work in the
            # log line below: a poller aimed at one tracker can run its own
            # instructions without every other job inheriting them.
            agent.ensure_persona(
                workspace,
                agent.persona_for("jobs", (job.key,) if job.key else ()),
            )
            session_uuid = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{job.platform}/{current_backend_key()}/{run_id}",
                )
            )
            # `None` on the job means "use the runner's configured limit", not
            # "no limit" — only JOBS_TIMEOUT can say the latter.
            timeout = job.timeout if job.timeout is not None else self.timeout
            try:
                response = await agent.run(
                    workspace=workspace,
                    session_uuid=session_uuid,
                    prompt=job.prompt,
                    platform=job.platform,
                    user_name=self.user_name,
                    # The key names the actual unit of work, which is what makes
                    # a log line traceable back to a ticket; the constant is the
                    # fallback for jobs that have no key.
                    channel_context=job.key or self.channel_context,
                    timeout=timeout,
                )
            except ClaudeUnavailableError as exc:
                logger.warning("jobs: agent unavailable: %s", exc)
                return Result(ok=False, text=f"Claude is unavailable: {exc}")
            except Exception as exc:  # NOT BaseException — CancelledError propagates
                logger.exception("jobs: agent run failed")
                return Result(ok=False, text=f"Job failed: {exc}")
            return Result(ok=True, text=response.body)
        finally:
            # A keyed job's workspace and session ARE its continuity, so nothing
            # is discarded: the next run with this key has to find them. Growth is
            # bounded by the number of distinct keys rather than by the number of
            # runs, and a key that stops being produced stops being written to.
            #
            # An unkeyed job gets a throwaway workspace; a long-lived worker would
            # grow data_dir/workspaces without bound otherwise. `finally` cleans up
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
            if not keyed:
                await asyncio.to_thread(_discard_workspace, workspace)
