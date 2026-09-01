"""Default `AgentRunner` — one agent run per job, reusing `agent.run`.

A job with no `session_key` is an independent one-shot: a fresh workspace under
`<platform>/__runs/<uuid>` and a fresh session uuid per call, so no prior
transcript bleeds in. Isolation comes from the uuid, not from any cleanup — the
workspace is new whatever happened to the last one. That is every Slack-triggered
job.

A job that carries a `session_key` is one step of something longer — turn 2 on a
ticket a poller keeps returning — so its workspace and session uuid derive from
that key instead of a random id, and it sits directly under `<platform>/`. The
next job with the same key resumes the same transcript rather than re-deriving
the world from nothing. Both the workspace path and the session uuid include the
key, so nothing else has to be persisted to make the resume happen.

Execution reuses the existing spawn-run-reap path in `agent.run` / backend
`_exec` — no new subprocess or kill code — so a graceful shutdown that cancels
the in-flight task lets `_exec`'s `finally` reap the whole process tree within
the supervisor's grace.

A handled agent failure becomes a `Result(ok=False, ...)`; `CancelledError`
(a `BaseException`, not caught here) still propagates so cancel-in-flight works.

No run deletes a workspace. Finished one-shots are retired later, in bulk, by
`sweep_run_workspaces` at worker startup: it takes the workspace *and* the
session directory the active backend named after it (claude keeps one outside the
workspace), for every `__runs/` entry past the retention window. Keeping them
until then means a run that failed overnight can still be inspected. A keyed
workspace is never swept; it is the continuity.

NOTE (stdin inheritance): `agent._exec` spawns the CLI without passing `stdin=`,
(the codex backend passes DEVNULL itself; this is about the claude path)
so the child inherits this process's stdin. That is harmless for the supervised
worker — `supervisor.spawn` starts daemons with `stdin=subprocess.DEVNULL` — but
a worker run in the foreground from a terminal hands the agent CLI that terminal's
stdin, and it may consume input meant for the shell. Run the worker detached
(`claude-tui start jobs`), or redirect its stdin, if that matters to you.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from claude_on_the_fly import agent, sandbox, tmux
from claude_on_the_fly.agent import ClaudeUnavailableError, current_backend_key
from claude_on_the_fly.jobs.core import Job, Result
from claude_on_the_fly.jobs.keys import safe_segment
from claude_on_the_fly.transcript import remove_workspace_sessions

logger = logging.getLogger(__name__)


# One-shot runs live under this segment, one level below the platform, so a
# sweep can tell them from a keyed workspace by path alone. A marker file inside
# the workspace would not do: the agent runs there under bypassPermissions and
# can delete anything it can see, which would make its own workspace immortal.
#
# The `__` is load-bearing, not decoration. Keyed workspaces are named by
# `safe_segment`, which collapses runs of unsafe characters to a *single* `_`, so
# no sanitized key can ever produce this name. A single-underscore `_runs` would
# be reachable from a `session_key` of `/runs`, and the sweep would then walk a
# live keyed workspace deleting the files inside it as if they were dead runs.
RUNS_DIRNAME = "__runs"


def _discard_workspace(workspace: Path) -> None:
    """Remove a finished job's workspace and the session directory the backend
    keyed to it. Blocking; call it off the event loop.

    Session dirs go first, while the workspace path still resolves — see
    `transcript.remove_workspace_sessions`, which is what stops
    `~/.claude/projects/` from gaining a dead directory per job.
    """
    remove_workspace_sessions(workspace)
    shutil.rmtree(workspace, ignore_errors=True)


def sweep_run_workspaces(data_dir: Path, *, days: int) -> list[Path]:
    """Delete one-shot workspaces last touched over `days` ago; return what went.
    `days` of 0 or less disables the sweep. Blocking; call it off the event loop.

    Scoped to `__runs/`, so a keyed job's workspace is never a candidate however
    long its key stays quiet. That workspace *is* the continuity for the next job
    with the same key, and age cannot tell "abandoned" from "waiting" — a ticket
    nobody touched for two months still wants turn 2 to resume turn 1.

    Modification time, not the directory name: a run id is a uuid and carries no
    date. A run still executing has a fresh mtime, so it cannot be swept out from
    under itself even if this is called on a live worker.

    Errors are ignored, as in `logs.prune`: retention must never take the worker
    down.
    """
    root = data_dir / "workspaces"
    if days <= 0 or not root.is_dir():
        return []
    cutoff = time.time() - days * 86400.0
    removed: list[Path] = []
    for runs_dir in sorted(root.glob(f"*/{RUNS_DIRNAME}")):
        for workspace in sorted(runs_dir.iterdir()):
            try:
                if not workspace.is_dir() or workspace.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            _discard_workspace(workspace)
            removed.append(workspace)
    return removed


@dataclass
class OrchestratorAgentRunner:
    """`AgentRunner` that drives `agent.run` in a fresh per-job workspace.

    `data_dir` roots the workspaces: `<data_dir>/workspaces/<platform>/__runs/<run>`
    for a one-shot, `<data_dir>/workspaces/<platform>/<key>` for a keyed job.
    `user_name` / `channel_context` / `timeout` are passed straight to
    `agent.run`. The composition root (`cli.py`) fills `timeout` from `jobs.timeout`;
    the
    other two keep the defaults below.
    """

    data_dir: Path
    timeout: float | None = agent.DEFAULT_TIMEOUT
    user_name: str = "jobs"
    channel_context: str = "jobs"
    # job_id -> {session_uuid, workspace, key, started_at_monotonic}, for the
    # heartbeat: the dashboard's jobs watch pane resolves a running job's live
    # session from this. Populated when a run starts, cleared when it ends —
    # including on cancel, via the finally below.
    in_flight: dict[str, dict] = field(default_factory=dict)

    async def run(self, job: Job) -> Result:
        # A keyed job's run id IS its session key, which is what makes the
        # workspace and the session uuid below stable across runs. An unkeyed one
        # gets a random id it will never see again.
        keyed = job.session_key is not None
        run_id = safe_segment(job.session_key) if job.session_key else uuid4().hex
        platform_dir = self.data_dir / "workspaces" / job.platform
        workspace = (
            platform_dir / run_id if keyed else platform_dir / RUNS_DIRNAME / run_id
        )
        workspace.mkdir(parents=True, exist_ok=True)
        # Host the run in its own tmux server, the same as a chat turn, so the
        # TUI can mirror a job nobody is watching in a conversation. The name
        # carries the key rather than the run id, because the key is what an
        # operator recognises in the watch list; an unkeyed job falls back to the
        # id, which is at least unique.
        # None when hosting is off, tmux is absent, or the socket path would not
        # fit an address: every one of those means an unhosted turn, not a failed
        # one.
        pane = (
            tmux.pane_for(tmux.job_session_name(run_id))
            if tmux.hosting_available()
            else None
        )
        env_token = sandbox.session_env(pane.env) if pane is not None else None
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
            self.in_flight[job.id] = {
                "session_uuid": session_uuid,
                "workspace": str(workspace),
                "key": job.key,
                "started_at_monotonic": time.monotonic(),
            }
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
            self.in_flight.pop(job.id, None)
            if env_token is not None:
                sandbox.reset_session_env(env_token)
            if pane is not None:
                # Ends the server, which is the only reap that reaches a process
                # the agent left running inside the pane.
                tmux.kill(pane)
            # No workspace is deleted here, keyed or not.
            #
            # A keyed job's workspace and session ARE its continuity: the next run
            # with this key has to find them. Growth is bounded by the number of
            # distinct keys rather than by the number of runs.
            #
            # An unkeyed job's workspace is dead the moment the run ends — the run
            # id is a uuid nothing records, so nothing can ask for it again — but
            # it is kept anyway, until `sweep_run_workspaces` retires it at
            # startup. What a failed 3am run left on disk is worth more than the
            # bytes, and isolation does not depend on the delete: the next run
            # gets its own uuid regardless.
            #
            # Deleting here was also the wrong place for it. This runs on the
            # shutdown path, where an agent that cloned a repo leaves a tree whose
            # rmtree takes seconds, and that is the same 5s grace the in-flight
            # cancel needs to reap the agent's process tree. Losing that race meant
            # a SIGKILLed worker and an orphaned agent CLI still holding
            # bypassPermissions.
