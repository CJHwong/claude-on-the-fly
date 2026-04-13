"""Claude Code CLI wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".claude-on-the-fly" / "memory"
MEMORY_ROOT = str(MEMORY_DIR)
KNOWLEDGE_DIR = str(MEMORY_DIR / "knowledge")
PROMPT_TEMPLATE = (Path(__file__).parent / "system_prompt.md").read_text()

FORMAT_HINTS = {
    "telegram": (
        "Format responses using Telegram-compatible markdown: "
        "*bold*, _italic_, `inline code`, ```code blocks```, - for lists. "
        "Do NOT use # headings or --- dividers."
    ),
    "slack": (
        "Format responses using Slack mrkdwn: "
        "*bold*, _italic_, `inline code`, ```code blocks```, - for lists. "
        "Do NOT use # headings, --- dividers, or ** for bold."
    ),
    "gmail": (
        "Format responses as plain text. No markdown, no HTML. "
        "Use line breaks for structure. Keep it concise."
    ),
}


def build_system_prompt(
    platform: str, user_name: str, channel_context: str = "dm"
) -> str:
    return PROMPT_TEMPLATE.format(
        format_hint=FORMAT_HINTS.get(platform, FORMAT_HINTS["telegram"]),
        user_name=user_name,
        channel_context=channel_context,
        memory_root=MEMORY_ROOT,
        knowledge_dir=KNOWLEDGE_DIR,
    )


@dataclass
class Response:
    """Structured response from the agent."""

    body: str
    cost: float = 0
    duration: float = 0
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""

    @property
    def has_stats(self) -> bool:
        return bool(self.cost or self.model)

    def format_stats(self) -> str:
        parts = []
        if self.cost:
            parts.append(f"${self.cost:.4f}")
        if self.duration:
            parts.append(f"{self.duration:.1f}s")
        if self.tokens_in or self.tokens_out:
            parts.append(f"↑{self.tokens_in} ↓{self.tokens_out}")
        if self.model:
            parts.append(self.model)
        return " | ".join(parts)


async def _exec(workspace: Path, cmd: list[str]) -> dict:
    logger.debug("exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:6]) + "...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    stdout, stderr = await proc.communicate()
    logger.debug(
        "exec: returncode=%s stdout=%d bytes stderr=%d bytes",
        proc.returncode,
        len(stdout),
        len(stderr),
    )
    if proc.returncode != 0:
        err_msg = stderr.decode().strip() or f"Exit code {proc.returncode}"
        logger.debug("exec: failed: %s", err_msg[:200])
        raise RuntimeError(err_msg)
    cli_output = json.loads(stdout)
    if cli_output.get("is_error") or cli_output.get("subtype", "").startswith("error"):
        raise RuntimeError(
            f"{cli_output.get('subtype', 'error')}: {cli_output.get('result', '?')}"
        )
    return cli_output


async def run(
    workspace: Path,
    session_uuid: str,
    prompt: str,
    platform: str,
    user_name: str = "unknown",
    channel_context: str = "dm",
) -> Response:
    """Run Claude Code and return a structured response."""
    system_prompt = build_system_prompt(platform, user_name, channel_context)
    model = os.environ.get("CLAUDE_MODEL", "sonnet")
    base = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--system-prompt",
        system_prompt,
    ]

    try:
        logger.debug(
            "agent.run: resuming session=%s prompt=%s", session_uuid, prompt[:80]
        )
        cli_output = await _exec(workspace, [*base, "--resume", session_uuid, prompt])
    except RuntimeError as exc:
        if "No conversation found" not in str(exc):
            raise
        logger.info("No existing session %s, creating new", session_uuid)
        cli_output = await _exec(
            workspace, [*base, "--session-id", session_uuid, prompt]
        )

    usage = cli_output.get("usage", {})
    return Response(
        body=cli_output.get("result", "No response"),
        cost=cli_output.get("total_cost_usd", 0),
        duration=cli_output.get("duration_ms", 0) / 1000,
        tokens_in=usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0),
        tokens_out=usage.get("output_tokens", 0),
        model=next(iter(cli_output.get("modelUsage", {})), ""),
    )
