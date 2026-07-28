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
from claude_on_the_fly.transcript import (
    _workspace_to_claude_hash,
    _workspace_to_pi_hash,
)


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
        result = await runner.run("what is 2+2?")

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
        await runner.run("a")
        await runner.run("b")

    assert len(seen) == 2
    assert seen[0] != seen[1]  # independent one-shot runs
    for ws in seen:
        assert not ws.exists()  # each throwaway workspace is cleaned up after run
    assert persona.call_count == 2


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
        result = await runner.run("p")
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
        result = await runner.run("p")
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
        result = await runner.run("p")

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
        result = await runner.run("p")

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
    ):
        with pytest.raises(asyncio.CancelledError):
            await runner.run("p")


async def test_backend_session_dir_removed_with_the_workspace(
    tmp_path: Path, claude_projects_dir: Path, pi_sessions_dir: Path
) -> None:
    """Deleting the workspace is not enough: claude and pi each name a directory
    in their own config tree after the workspace path, and the name encodes a
    path that will never exist again — so anything left there is unreclaimable,
    one dead directory per job."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    captured: dict[str, Path] = {}

    async def _fake_run(**kwargs):
        workspace = kwargs["workspace"]
        captured["ws"] = workspace
        # Stand in for what the agent CLI writes while it runs.
        (claude_projects_dir / _workspace_to_claude_hash(workspace)).mkdir(parents=True)
        (pi_sessions_dir / _workspace_to_pi_hash(workspace)).mkdir(parents=True)
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch("claude_on_the_fly.jobs.agent_runner.agent.ensure_persona"),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        result = await runner.run("p")

    assert result.ok is True
    workspace = captured["ws"]
    assert not (claude_projects_dir / _workspace_to_claude_hash(workspace)).exists()
    assert not (pi_sessions_dir / _workspace_to_pi_hash(workspace)).exists()


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
        task = asyncio.create_task(runner.run("p"))
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
        await runner.run("p")
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
    ):
        with pytest.raises(asyncio.CancelledError):
            await runner.run("p")

    assert not captured["ws"].exists()  # torn down on the cancel path
