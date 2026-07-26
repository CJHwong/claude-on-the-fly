"""OrchestratorAgentRunner: reuses agent.run, maps outcomes to Result, and lets
CancelledError propagate."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner


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
