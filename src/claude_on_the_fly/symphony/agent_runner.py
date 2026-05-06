"""Per-ticket Claude session runner.

Bridges the orchestrator's per-ticket lifecycle and the existing claude-on-the-fly
ClaudeAgent subprocess driver. One ticket = one stable session UUID = many turns
via --resume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import Response

from .config import SymphonyConfig
from .prompt import render_prompt
from .tracker.issue import Issue
from .workspace import Workspace

logger = logging.getLogger(__name__)


def session_uuid_for(issue_identifier: str) -> str:
    """Deterministic UUID per ticket so daemon restarts resume the prior session."""
    return str(uuid5(NAMESPACE_URL, f"claude-symphony/{issue_identifier}"))


@dataclass
class TicketRunner:
    issue: Issue
    workspace: Workspace
    config: SymphonyConfig
    prompt_source: str
    session_uuid: str

    async def run_turn(self, attempt: int) -> Response:
        prompt = render_prompt(
            self.prompt_source,
            issue=self.issue,
            attempt=attempt,
            workspace_path=self.workspace.path,
            gate_label=self.config.gate_label,
        )
        logger.debug(
            "[%s] turn %d: prompt len=%d session=%s",
            self.issue.identifier,
            attempt,
            len(prompt),
            self.session_uuid,
        )
        return await agent.run(
            workspace=self.workspace.path,
            session_uuid=self.session_uuid,
            prompt=prompt,
            platform="symphony",
            user_name="symphony",
            channel_context=self.issue.identifier,
            timeout=self.config.turn_timeout_ms / 1000,
        )
