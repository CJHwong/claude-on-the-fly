"""Per-ticket Claude session runner.

Bridges the orchestrator's per-ticket lifecycle and the existing claude-on-the-fly
ClaudeAgent subprocess driver. One ticket = one stable session UUID = many turns
via --resume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import Response

from .config import SymphonyConfig, TrackerCommonConfig
from .prompt import render_prompt
from .tracker.issue import Issue

logger = logging.getLogger(__name__)


def session_uuid_for(
    issue_identifier: str,
    source: str = "jira",
    backend_key: str = "claude:native:sonnet",
) -> str:
    """Deterministic UUID per (source, backend_key, ticket) so daemon restarts
    resume the prior session for the same backend/model combo. Source is in
    the seed so two trackers minting the same `identifier` get distinct UUIDs;
    backend_key isolates sessions across model switches (e.g. claude-native vs
    claude-via-ollama) so the saved JSONL is never replayed against an
    incompatible upstream. Cross-key context survives via the handoff path
    in `transcript.find_latest_prior_transcript`."""
    return str(
        uuid5(
            NAMESPACE_URL, f"claude-symphony/{source}/{backend_key}/{issue_identifier}"
        )
    )


@dataclass
class TicketRunner:
    issue: Issue
    workspace: Path
    config: SymphonyConfig
    tracker_cfg: TrackerCommonConfig
    prompt_source: str
    session_uuid: str

    async def run_turn(self, attempt: int) -> Response:
        prompt = render_prompt(
            self.prompt_source,
            issue=self.issue,
            attempt=attempt,
            workspace_path=self.workspace,
        )
        logger.debug(
            "[%s] turn %d: prompt len=%d session=%s",
            self.issue.identifier,
            attempt,
            len(prompt),
            self.session_uuid,
        )
        return await agent.run(
            workspace=self.workspace,
            session_uuid=self.session_uuid,
            prompt=prompt,
            platform="symphony",
            user_name="symphony",
            channel_context=self.issue.identifier,
            timeout=self.config.turn_timeout_ms / 1000,
        )
