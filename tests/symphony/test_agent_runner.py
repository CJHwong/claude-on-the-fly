"""Tests for symphony agent_runner: TicketRunner and session_uuid_for."""

from pathlib import Path
from unittest.mock import AsyncMock, patch


from claude_on_the_fly.symphony.agent_runner import TicketRunner, session_uuid_for
from claude_on_the_fly.symphony.config import JiraTrackerConfig, SymphonyConfig
from claude_on_the_fly.symphony.tracker.issue import Issue


def _issue(**overrides: object) -> Issue:
    defaults = {
        "id": "10042",
        "identifier": "PROJ-1133",
        "title": "Fix login bug",
        "state": "In Progress",
        "description_raw": None,
        "priority": 3,
        "labels": (),
        "blocked_by": (),
        "parent_key": None,
        "url": "https://jira.example.com/browse/PROJ-1133",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-02T00:00:00",
    }
    return Issue(**(defaults | {k: v for k, v in overrides.items() if k in defaults}))  # type: ignore[arg-type]


def _config(**overrides: object) -> SymphonyConfig:
    tracker = JiraTrackerConfig(
        kind="jira",
        base_url="https://x.atlassian.net",
        email="me@x.com",
        api_token="tok",
        project_key="PROJ",
        gate_label="exit_label",
        prompt_path=Path("/tmp/symphony-prompt.md"),
    )
    global_defaults = {
        "turn_timeout_ms": 60_000,
        "max_turns": 10,
        "stall_timeout_ms": 300_000,
        "polling_ms": 30_000,
        "max_concurrent": 3,
        "max_retry_backoff_ms": 3600_000,
    }
    return SymphonyConfig(
        trackers={"jira": tracker},
        **(global_defaults | overrides),
    )


# ---------------------------------------------------------------------------
# session_uuid_for
# ---------------------------------------------------------------------------


def test_session_uuid_deterministic() -> None:
    a = session_uuid_for("PROJ-1")
    b = session_uuid_for("PROJ-1")
    c = session_uuid_for("PROJ-2")
    assert a == b
    assert a != c


def test_session_uuid_is_string() -> None:
    result = session_uuid_for("PROJ-1")
    assert isinstance(result, str)
    assert len(result) == 36  # standard UUID


def test_session_uuid_includes_source_in_seed() -> None:
    """The same identifier across two sources mints distinct UUIDs so a
    Jira ticket and a GitHub PR with overlapping keys don't collide."""
    jira_uuid = session_uuid_for("PROJ-1", source="jira")
    gh_uuid = session_uuid_for("PROJ-1", source="github")
    assert jira_uuid != gh_uuid

    # Determinism per (source, identifier).
    assert session_uuid_for("owner/repo#1", source="github") == session_uuid_for(
        "owner/repo#1", source="github"
    )


def test_session_uuid_includes_backend_key_in_seed() -> None:
    """Switching model/backend must mint a distinct UUID so the saved JSONL
    from a different upstream is never replayed under an incompatible CLI."""
    native = session_uuid_for(
        "PROJ-1", source="jira", backend_key="claude:native:sonnet"
    )
    ollama = session_uuid_for(
        "PROJ-1", source="jira", backend_key="claude:ollama:qwen2.5-coder"
    )
    codex_native = session_uuid_for(
        "PROJ-1", source="jira", backend_key="codex:native:gpt-5"
    )
    assert len({native, ollama, codex_native}) == 3

    # Same (source, identifier, backend_key) is still deterministic.
    assert native == session_uuid_for(
        "PROJ-1", source="jira", backend_key="claude:native:sonnet"
    )


# ---------------------------------------------------------------------------
# TicketRunner.run_turn
# ---------------------------------------------------------------------------


async def test_run_turn_calls_agent_run_with_correct_args() -> None:
    issue = _issue()
    workspace = Path("/tmp/ws")
    config = _config()
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=config,
        tracker_cfg=config.tracker,
        prompt_source="You are a helpful assistant.",
        session_uuid="test-uuid",
    )

    mock_resp = AsyncMock()
    mock_resp.body = "done"

    with (
        patch("claude_on_the_fly.symphony.agent_runner.render_prompt") as mock_render,
        patch("claude_on_the_fly.symphony.agent_runner.agent.run") as mock_agent_run,
    ):
        mock_render.return_value = "rendered prompt text"
        mock_agent_run.return_value = mock_resp

        result = await runner.run_turn(attempt=3)

        mock_render.assert_called_once_with(
            "You are a helpful assistant.",
            issue=issue,
            attempt=3,
            workspace_path=Path("/tmp/ws"),
            gate_label="exit_label",
        )
        mock_agent_run.assert_awaited_once_with(
            workspace=Path("/tmp/ws"),
            session_uuid="test-uuid",
            prompt="rendered prompt text",
            platform="symphony",
            user_name="symphony",
            channel_context="PROJ-1133",
            timeout=60.0,
        )
        assert result is mock_resp


async def test_run_turn_attempt_zero() -> None:
    """First turn (attempt=0)."""
    issue = _issue()
    workspace = Path("/tmp/ws")
    cfg = _config()
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=cfg,
        tracker_cfg=cfg.tracker,
        prompt_source="prompt src",
        session_uuid="uuid-0",
    )

    mock_resp = AsyncMock()
    with (
        patch("claude_on_the_fly.symphony.agent_runner.render_prompt") as mock_render,
        patch("claude_on_the_fly.symphony.agent_runner.agent.run") as mock_agent_run,
    ):
        mock_render.return_value = "rendered"
        mock_agent_run.return_value = mock_resp

        await runner.run_turn(attempt=0)

        mock_render.assert_called_once()
        assert mock_render.call_args.kwargs["attempt"] == 0


async def test_run_turn_custom_timeout() -> None:
    """Verify timeout_ms is converted to seconds for agent.run."""
    issue = _issue()
    workspace = Path("/tmp/ws")
    config = _config(turn_timeout_ms=120_000)
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=config,
        tracker_cfg=config.tracker,
        prompt_source="src",
        session_uuid="u",
    )

    mock_resp = AsyncMock()
    with (
        patch(
            "claude_on_the_fly.symphony.agent_runner.render_prompt", return_value="r"
        ),
        patch("claude_on_the_fly.symphony.agent_runner.agent.run") as mock_agent_run,
    ):
        mock_agent_run.return_value = mock_resp
        await runner.run_turn(attempt=1)

        assert mock_agent_run.call_args.kwargs["timeout"] == 120.0
