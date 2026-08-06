"""OrchestratorAgentRunner: reuses agent.run, maps outcomes to Result, and lets
CancelledError propagate."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner
from claude_on_the_fly.jobs.core import Job
from claude_on_the_fly.transcript import _workspace_to_claude_hash


def _job(prompt: str = "p", **overrides) -> Job:
    """An unkeyed job, the shape every Slack-triggered one has."""
    return Job(id="1-a", prompt=prompt, origin={"channel": "C1"}, **overrides)


async def test_run_calls_agent_run_for_jobs_platform(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path, timeout=42.0)
    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run") as mock_run,
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        mock_run.return_value = Response(body="the answer")
        result = await runner.run(_job("what is 2+2?"))

    assert result.ok is True
    assert result.text == "the answer"
    kwargs = mock_run.call_args.kwargs
    assert kwargs["platform"] == "jobs"
    assert kwargs["prompt"] == "what is 2+2?"
    assert kwargs["timeout"] == 42.0
    # Fresh workspace under data_dir/workspaces/jobs/<run_id>.
    assert kwargs["workspace"].parent == tmp_path / "workspaces" / "jobs"


async def test_fresh_workspace_and_persona_per_call(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    seen: list[Path] = []

    async def _fake_run(**kwargs):
        ws = kwargs["workspace"]
        assert ws.is_dir()  # live during the run
        seen.append(ws)
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona") as persona,
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("a"))
        await runner.run(_job("b"))

    assert len(seen) == 2
    assert seen[0] != seen[1]  # independent one-shot runs
    for ws in seen:
        assert not ws.exists()  # each throwaway workspace is cleaned up after run
    assert persona.call_count == 2


async def test_a_keyed_job_can_have_its_own_persona(tmp_path: Path) -> None:
    """The job key is the persona key, so one poller's instructions do not leak
    into every other job the worker runs."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    with (
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            return_value=Response(body="ok"),
        ),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona") as persona,
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.persona_for",
            return_value=tmp_path / "ticket-bot.md",
        ) as resolve,
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("a", key="ACE-1234"))
        await runner.run(_job("b"))

    assert [call.args for call in resolve.call_args_list] == [
        ("jobs", ("ACE-1234",)),
        ("jobs", ()),  # unkeyed: nothing to match on, so only the default file
    ]
    assert persona.call_args.args[1] == tmp_path / "ticket-bot.md"


async def test_agent_exception_becomes_failure_result(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    with (
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            side_effect=RuntimeError("kaboom"),
        ),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run(_job("p"))
    assert result.ok is False
    assert "kaboom" in result.text


async def test_claude_unavailable_becomes_failure_result(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    with (
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            side_effect=ClaudeUnavailableError("usage limit"),
        ),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run(_job("p"))
    assert result.ok is False
    assert "unavailable" in result.text.lower()


async def test_workspace_removed_after_successful_run(tmp_path: Path) -> None:
    """The throwaway per-job workspace must not linger — a long-lived worker would
    otherwise grow data_dir/workspaces/jobs without bound."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _fake_run(**kwargs):
        captured["ws"] = kwargs["workspace"]
        assert kwargs["workspace"].is_dir()  # exists while the agent runs
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run(_job("p"))

    assert result.ok is True
    assert not captured["ws"].exists()


async def test_workspace_removed_when_agent_run_raises(tmp_path: Path) -> None:
    """Cleanup runs on the handled-failure path too, not just on success."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _boom(**kwargs):
        captured["ws"] = kwargs["workspace"]
        raise RuntimeError("kaboom")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_boom),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run(_job("p"))

    assert result.ok is False
    assert not captured["ws"].exists()


async def test_cancelled_error_propagates(tmp_path: Path) -> None:
    """CancelledError must NOT be swallowed — cancel-in-flight shutdown relies on
    it reaching agent._exec's finally."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    with (
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            side_effect=asyncio.CancelledError(),
        ),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.run(_job("p"))


async def test_backend_session_dir_removed_with_the_workspace(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """Deleting the workspace is not enough: claude names a directory in its own
    config tree after the workspace path, and the name encodes a path that will
    never exist again — so anything left there is unreclaimable, one dead
    directory per job."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _fake_run(**kwargs):
        workspace = kwargs["workspace"]
        captured["ws"] = workspace
        # Stand in for what the agent CLI writes while it runs.
        (claude_projects_dir / _workspace_to_claude_hash(workspace)).mkdir(parents=True)
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run(_job("p"))

    assert result.ok is True
    workspace = captured["ws"]
    assert not (claude_projects_dir / _workspace_to_claude_hash(workspace)).exists()


async def test_cleanup_runs_when_the_task_is_cancelled_from_outside(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """The shape the real shutdown takes: `run_loop` cancels the task rather than
    letting the agent raise. Teardown now awaits, and an await inside a `finally`
    is exactly what a careless cancel would skip — so assert it still runs, or
    every stop leaks a workspace and a session directory."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}
    running = asyncio.Event()

    async def _hang(**kwargs):
        workspace = kwargs["workspace"]
        captured["ws"] = workspace
        (claude_projects_dir / _workspace_to_claude_hash(workspace)).mkdir(parents=True)
        running.set()
        await asyncio.sleep(3600)  # cancelled out from under us

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_hang),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        task = asyncio.create_task(runner.run(_job("p")))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    workspace = captured["ws"]
    assert not workspace.exists()
    assert not (claude_projects_dir / _workspace_to_claude_hash(workspace)).exists()


async def test_cleanup_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """Teardown is on the shutdown path, where the supervisor allows 5s before
    SIGKILL. A synchronous rmtree of a tree an agent just built spends that
    window on file deletion and gets the worker killed with its agent CLI still
    running — orphaned, since `_exec` spawns it into its own session. Assert the
    loop keeps turning by having a slow removal run concurrently with a tick."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    ticked = asyncio.Event()

    def _slow_discard(workspace: Path) -> None:
        time.sleep(0.2)  # a big tree, in a thread

    async def _tick() -> None:
        await asyncio.sleep(0.01)
        ticked.set()

    with (
        patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            return_value=Response(body="ok"),
        ),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
        patch(
            "claude_on_the_fly.jobs.agent_runner._discard_workspace",
            side_effect=_slow_discard,
        ),
    ):
        tick = asyncio.create_task(_tick())
        await runner.run(_job("p"))
        # A blocking cleanup would have starved this until after the sleep.
        assert ticked.is_set()
        await tick


async def test_workspace_removed_on_cancel_path(tmp_path: Path) -> None:
    """Cleanup runs while CancelledError unwinds — a cancel-in-flight shutdown must
    not leak the per-job workspace, which `run`'s `finally: rmtree` guarantees."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _cancel(**kwargs):
        captured["ws"] = kwargs["workspace"]
        assert kwargs["workspace"].is_dir()  # exists while the agent runs
        raise asyncio.CancelledError()

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_cancel),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.run(_job("p"))

    assert not captured["ws"].exists()  # torn down on the cancel path


# --- keyed jobs ------------------------------------------------------------


async def test_keyed_job_reuses_one_workspace_and_session_across_runs(
    tmp_path: Path,
) -> None:
    """The whole point of a session key: turn 2 continues turn 1's transcript
    instead of starting from nothing, with nothing persisted to arrange it."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    seen: list[tuple[Path, str]] = []

    async def _fake_run(**kwargs):
        seen.append((kwargs["workspace"], kwargs["session_uuid"]))
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("turn 1", session_key="jira/ACE-1", platform="cron"))
        await runner.run(_job("turn 2", session_key="jira/ACE-1", platform="cron"))

    assert seen[0] == seen[1], "a keyed job must resume, not start fresh"
    workspace, _ = seen[0]
    assert workspace.exists(), "a keyed workspace IS the continuity, so it must survive"
    # `/` in the key would otherwise make a nested directory.
    assert workspace.parent == tmp_path / "workspaces" / "cron"
    assert workspace.name == "jira_ACE-1"


async def test_different_keys_get_different_workspaces(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    seen: list[Path] = []

    async def _fake_run(**kwargs):
        seen.append(kwargs["workspace"])
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("a", session_key="jira/ACE-1"))
        await runner.run(_job("b", session_key="jira/ACE-2"))

    assert seen[0] != seen[1]


async def test_keyed_job_keeps_its_backend_session_dir(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """The mirror of the unkeyed case: deleting the backend's session directory
    would throw away exactly the transcript the next run means to resume."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _fake_run(**kwargs):
        workspace = kwargs["workspace"]
        captured["ws"] = workspace
        (claude_projects_dir / _workspace_to_claude_hash(workspace)).mkdir(parents=True)
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("p", session_key="jira/ACE-1"))

    session_dir = claude_projects_dir / _workspace_to_claude_hash(captured["ws"])
    assert session_dir.exists()


# --- per-job timeout and platform -----------------------------------------


async def test_job_timeout_overrides_the_runner_default(tmp_path: Path) -> None:
    runner = OrchestratorAgentRunner(data_dir=tmp_path, timeout=42.0)
    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run") as mock_run,
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        mock_run.return_value = Response(body="ok")
        await runner.run(_job("p", timeout=7.5))

    assert mock_run.call_args.kwargs["timeout"] == 7.5


async def test_job_without_a_timeout_falls_back_to_the_runner(tmp_path: Path) -> None:
    """None means "use what the worker was configured with", not "no limit" —
    only JOBS_TIMEOUT can say the latter."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path, timeout=42.0)
    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run") as mock_run,
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        mock_run.return_value = Response(body="ok")
        await runner.run(_job("p"))

    assert mock_run.call_args.kwargs["timeout"] == 42.0


async def test_platform_rides_on_the_job(tmp_path: Path) -> None:
    """It selects the agent's format hint, and it must not be inferred from
    `origin` — reading `origin` is the notifier's job alone."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run") as mock_run,
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        mock_run.return_value = Response(body="ok")
        await runner.run(_job("p", platform="cron"))

    assert mock_run.call_args.kwargs["platform"] == "cron"
