"""OrchestratorAgentRunner: reuses agent.run, maps outcomes to Result, and lets
CancelledError propagate."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.jobs.agent_runner import (
    RUNS_DIRNAME,
    OrchestratorAgentRunner,
    sweep_run_workspaces,
)
from claude_on_the_fly.jobs.core import Job
from claude_on_the_fly.jobs.keys import safe_segment
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
    # Fresh workspace under data_dir/workspaces/jobs/__runs/<run_id>.
    assert kwargs["workspace"].parent == tmp_path / "workspaces" / "jobs" / "__runs"


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
        assert ws.exists()  # kept for inspection; the sweep retires them later
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


async def test_workspace_survives_a_successful_run(tmp_path: Path) -> None:
    """A finished one-shot workspace is kept, not deleted at the end of the run.
    Isolation does not depend on the delete — the next run gets its own uuid — so
    keeping it costs nothing and leaves the run inspectable until the sweep."""
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
    assert captured["ws"].exists()


async def test_workspace_survives_when_agent_run_raises(tmp_path: Path) -> None:
    """Most of all on the failure path: a run that died at 3am is the one whose
    files an operator actually wants to read."""
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
    assert captured["ws"].exists()


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


async def test_backend_session_dir_survives_the_run_too(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """The transcript is half of what makes a finished run worth keeping, so it
    outlives the run with the workspace. `sweep_run_workspaces` takes both."""
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
    assert (claude_projects_dir / _workspace_to_claude_hash(workspace)).exists()


async def test_a_cancelled_run_leaves_its_workspace_behind(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """The shape the real shutdown takes: `run_loop` cancels the task rather than
    letting the agent raise. Nothing is deleted while that unwinds, which is what
    keeps a stop from spending the supervisor's 5s grace on an rmtree."""
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
    assert workspace.exists()
    assert (claude_projects_dir / _workspace_to_claude_hash(workspace)).exists()


async def test_a_run_never_discards_a_workspace_itself(tmp_path: Path) -> None:
    """Regression guard for putting deletion back on the run path.

    Teardown there lands on the shutdown path, where the supervisor allows 5s
    before SIGKILL. An rmtree of a tree the agent just built (a repo clone takes
    seconds) competes with the in-flight cancel for that window, and losing means
    a killed worker with an orphaned agent CLI still holding bypassPermissions —
    `_exec` spawns it into its own session, so nothing else reaps it. Retention
    belongs in the startup sweep, where it races nothing."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)

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
        ) as discard,
    ):
        await runner.run(_job("p"))

    discard.assert_not_called()


async def test_workspace_survives_the_cancel_path(tmp_path: Path) -> None:
    """Same for the agent raising CancelledError itself rather than the task being
    cancelled from outside."""
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

    assert captured["ws"].exists()  # nothing is torn down on the cancel path


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


async def test_in_flight_is_populated_during_the_run_and_cleared_after(
    tmp_path: Path,
) -> None:
    """The heartbeat's jobs watch pane resolves a running job's live session
    from this; it must be present while the agent runs and gone when it
    ends."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)
    during: list[dict] = []

    async def _fake_run(**kwargs):
        during.append(dict(runner.in_flight))
        return Response(body="ok")

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        await runner.run(_job("a"))

    (entry,) = during
    info = entry["1-a"]
    assert info["session_uuid"]
    assert info["workspace"].startswith(str(tmp_path / "workspaces" / "jobs"))
    assert info["key"] == "p" or info["key"] is None  # _job() has no key
    assert runner.in_flight == {}


async def test_in_flight_is_cleared_when_the_run_is_cancelled(
    tmp_path: Path,
) -> None:
    """Shutdown cancels the in-flight job; the finally must clear the entry
    or the heartbeat would keep advertising a dead session."""
    runner = OrchestratorAgentRunner(data_dir=tmp_path)

    async def _fake_run(**kwargs):
        await asyncio.sleep(3600)

    with (
        patch("claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_fake_run),
        patch(
            "claude_on_the_fly.jobs.agent_runner.current_backend_key",
            return_value="claude:native:sonnet",
        ),
    ):
        task = asyncio.create_task(runner.run(_job("a")))
        await asyncio.sleep(0.05)
        assert "1-a" in runner.in_flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert runner.in_flight == {}


# --- retention sweep -------------------------------------------------------


DAY_S = 86400.0


def _aged_workspace(root: Path, *, age_days: float) -> Path:
    """A workspace directory whose mtime is `age_days` in the past."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.md").write_text("what the run left behind")
    stamp = time.time() - age_days * DAY_S
    os.utime(root, (stamp, stamp))
    return root


def test_sweep_removes_a_one_shot_workspace_past_the_window(tmp_path: Path) -> None:
    runs = tmp_path / "workspaces" / "cron" / "__runs"
    old = _aged_workspace(runs / "deadbeef", age_days=31)

    removed = sweep_run_workspaces(tmp_path, days=30)

    assert removed == [old]
    assert not old.exists()


def test_sweep_keeps_a_one_shot_workspace_inside_the_window(tmp_path: Path) -> None:
    """Retention is the whole point of keeping them: a run from this morning must
    still be there to read."""
    runs = tmp_path / "workspaces" / "jobs" / "__runs"
    recent = _aged_workspace(runs / "cafe", age_days=29)

    assert sweep_run_workspaces(tmp_path, days=30) == []
    assert (recent / "notes.md").exists()


def test_sweep_never_touches_a_keyed_workspace_however_old(tmp_path: Path) -> None:
    """A keyed workspace IS the continuity for the next job with that key, and age
    cannot tell "abandoned" from "waiting" — a ticket nobody has touched in a year
    still wants turn 2 to resume turn 1. Sweeping it would silently destroy the
    resume, so the sweep is scoped to `__runs/` by path, not filtered by age."""
    keyed = _aged_workspace(
        tmp_path / "workspaces" / "cron" / "jira_ACE-1234", age_days=400
    )

    assert sweep_run_workspaces(tmp_path, days=30) == []
    assert (keyed / "notes.md").exists()


def test_sweep_cannot_be_aimed_at_a_keyed_workspace_by_a_crafted_key(
    tmp_path: Path,
) -> None:
    """`safe_segment` maps unsafe characters to `_`, so a `session_key` of `/runs`
    would land on `_runs`. Were that the sweep's directory, the sweep would walk a
    live keyed workspace and delete the files inside it as dead runs. `__runs` is
    unreachable from any sanitized key, because sanitizing collapses runs of
    unsafe characters to a single `_`."""
    assert safe_segment("/runs") != RUNS_DIRNAME
    assert safe_segment("__runs") != RUNS_DIRNAME
    assert "__" not in safe_segment("anything/at\\all??here")


def test_sweep_takes_the_backend_session_dir_with_it(
    tmp_path: Path, claude_projects_dir: Path
) -> None:
    """Deleting the workspace alone is not enough: claude names a directory in its
    own config tree after the workspace path, and that name encodes a path that
    will never exist again — so anything left there is unreclaimable, one dead
    directory per job."""
    old = _aged_workspace(
        tmp_path / "workspaces" / "jobs" / "__runs" / "beef", age_days=31
    )
    session_dir = claude_projects_dir / _workspace_to_claude_hash(old)
    session_dir.mkdir(parents=True)

    sweep_run_workspaces(tmp_path, days=30)

    assert not session_dir.exists()


def test_sweep_is_disabled_by_a_non_positive_window(tmp_path: Path) -> None:
    """`workspace_keep_days: 0` means keep them forever, for an operator with the
    disk who wants the history."""
    old = _aged_workspace(
        tmp_path / "workspaces" / "jobs" / "__runs" / "beef", age_days=9999
    )

    assert sweep_run_workspaces(tmp_path, days=0) == []
    assert old.exists()


def test_sweep_on_a_data_dir_with_no_workspaces_yet(tmp_path: Path) -> None:
    """First start on a fresh install: nothing has run, so there is no directory."""
    assert sweep_run_workspaces(tmp_path, days=30) == []


def test_sweep_ignores_a_file_sitting_in_the_runs_dir(tmp_path: Path) -> None:
    runs = tmp_path / "workspaces" / "jobs" / "__runs"
    runs.mkdir(parents=True)
    stray = runs / "not-a-workspace.txt"
    stray.write_text("x")
    os.utime(stray, (time.time() - 400 * DAY_S,) * 2)

    assert sweep_run_workspaces(tmp_path, days=30) == []
    assert stray.exists()


def test_sweep_skips_an_entry_it_cannot_stat(tmp_path: Path) -> None:
    """A workspace that vanishes between the listing and the stat, or that the
    worker cannot read, must not take retention down with it — the next entry
    still deserves to be swept."""
    runs = tmp_path / "workspaces" / "jobs" / "__runs"
    _aged_workspace(runs / "aaa-unreadable", age_days=31)
    good = _aged_workspace(runs / "bbb-fine", age_days=31)

    real_stat = Path.stat

    def _stat(self, *args, **kwargs):
        if self.name == "aaa-unreadable":
            raise OSError("gone")
        return real_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", _stat):
        removed = sweep_run_workspaces(tmp_path, days=30)

    assert removed == [good]
    assert not good.exists()
