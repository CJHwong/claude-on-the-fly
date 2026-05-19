"""Claude Code CLI backend."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from claude_on_the_fly import agent, pricing, transcript
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    ClaudeUnavailableError,
    OllamaLauncher,
    Response,
    build_system_prompt,
)

logger = logging.getLogger(__name__)


class ClaudeBackend:
    """Drives the `claude` CLI in non-interactive (`-p`) mode.

    When `launcher` is set, the CLI call is wrapped with that launcher's prefix
    (e.g. `ollama launch claude --model <X> --yes --`) and claude's `--model`
    flag is dropped — the launcher decides the model.
    """

    def __init__(self, launcher: OllamaLauncher | None = None) -> None:
        self.launcher = launcher

    async def run(
        self,
        workspace: Path,
        session_uuid: str,
        prompt: str,
        platform: str,
        user_name: str = "unknown",
        channel_context: str = "dm",
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Response:
        logger.info(
            "session: id=%s platform=%s user=%s context=%s workspace=%s",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )
        system_prompt = build_system_prompt(platform, user_name, channel_context)
        # `ollama launch claude` already invokes the claude binary; repeating
        # "claude" after `--` would make it argv[1], which -p mode parses as
        # the prompt and silently drops the real one.
        prefix = self.launcher.prefix("claude") if self.launcher else []
        binary = [] if self.launcher else ["claude"]
        model_args = (
            []
            if self.launcher
            else ["--model", os.environ.get("CLAUDE_MODEL", "sonnet")]
        )
        base = [
            *prefix,
            *binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            *model_args,
            "--system-prompt",
            system_prompt,
        ]

        try:
            logger.debug(
                "agent.run: resuming session=%s prompt=%s", session_uuid, prompt[:80]
            )
            cli_output = await agent._exec(
                workspace, [*base, "--resume", session_uuid, prompt], timeout=timeout
            )
        except ClaudeUnavailableError:
            raise
        except RuntimeError as exc:
            if "No conversation found" not in str(exc):
                raise
            logger.info("No existing session %s, creating new", session_uuid)
            handoff_prompt = transcript.prepend_handoff(
                workspace,
                session_uuid,
                prompt,
                from_backend="codex",
                extractor=transcript.extract_codex,
            )
            cli_output = await agent._exec(
                workspace,
                [*base, "--session-id", session_uuid, handoff_prompt],
                timeout=timeout,
            )

        body = (cli_output.get("result") or "").strip()
        if not body:
            logger.warning(
                "agent.run: empty result, retrying with nudge, session=%s", session_uuid
            )
            retry_output = await agent._exec(
                workspace,
                [*base, "--resume", session_uuid, agent.NUDGE_PROMPT],
                timeout=timeout,
            )
            cli_output = agent._merge_cli_output(cli_output, retry_output)
            body = (cli_output.get("result") or "").strip() or "No response"

        usage = cli_output.get("usage", {})
        tokens_in = usage.get("input_tokens", 0) + usage.get(
            "cache_read_input_tokens", 0
        )
        tokens_out = usage.get("output_tokens", 0)
        model = next(iter(cli_output.get("modelUsage", {})), "")

        # In ollama mode the claude CLI still computes total_cost_usd from
        # Anthropic's price table, which is meaningless when ollama is
        # actually serving the model. Look up the routed model's price in
        # the OpenRouter registry instead, matching how the codex backend
        # handles its own cost. Native mode keeps the CLI's value, which
        # reflects Anthropic's real billing.
        if self.launcher is not None:
            cost = (
                await asyncio.to_thread(pricing.cost_for, model, tokens_in, tokens_out)
                or 0
            )
        else:
            cost = cli_output.get("total_cost_usd", 0)

        return Response(
            body=body,
            cost=cost,
            duration=cli_output.get("duration_ms", 0) / 1000,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            tool_counts=cli_output.get("tool_counts", {}),
            skill_counts=cli_output.get("skill_counts", {}),
        )
