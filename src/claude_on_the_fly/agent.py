"""Claude Code CLI wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".claude-on-the-fly" / "memory"
MEMORY_ROOT = str(MEMORY_DIR)
KNOWLEDGE_DIR = str(MEMORY_DIR / "knowledge")
PROMPT_TEMPLATE = (Path(__file__).parent / "system_prompt.md").read_text()

STATS_MODES = ("off", "summary", "detailed")


def stats_mode(platform: str) -> str:
    """Read the reply-footer mode for a given frontend from its env var.

    Returns one of STATS_MODES. Defaults to "summary" for unknown or unset.
    Platform "telegram" reads TELEGRAM_STATS_MODE, and so on.
    """
    env_name = f"{platform.upper()}_STATS_MODE"
    mode = os.environ.get(env_name, "summary").lower()
    return mode if mode in STATS_MODES else "summary"


def footer_parts(response: "Response", platform: str) -> tuple[str, str]:
    """Return (stats_line, tools_line) for a reply, gated by the platform's mode.

    Either value is "" when the mode suppresses that line.
    """
    mode = stats_mode(platform)
    stats = response.format_stats() if mode != "off" and response.has_stats else ""
    tools = response.format_tools() if mode == "detailed" and response.has_tools else ""
    return stats, tools


FORMAT_HINTS = {
    "telegram": (
        "Format responses using Telegram-compatible markdown: "
        "*bold*, _italic_, `inline code`, ```code blocks```, - for lists. "
        "Do NOT use # headings or --- dividers."
    ),
    "slack": (
        "Format responses using Slack mrkdwn: "
        "*bold*, _italic_, `inline code`, ```code blocks```, - for lists. "
        "Do NOT use # headings, --- dividers, or ** for bold. "
        "For tables, use code blocks - Slack mrkdwn has no table syntax."
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
    tool_counts: dict[str, int] = field(default_factory=dict)
    skill_counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_stats(self) -> bool:
        return bool(self.cost or self.model)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_counts)

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

    def format_tools(self) -> str:
        if not self.tool_counts:
            return ""
        total = sum(self.tool_counts.values())
        items = sorted(self.tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        breakdown = " ".join(f"{name}×{count}" for name, count in items)
        return f"🔧 {total} ({breakdown})"


def _fold(
    msg: dict, tool_counts: dict[str, int], skill_counts: dict[str, int]
) -> dict | None:
    """Apply one parsed stream-json message to running tallies.

    Returns the message dict if it is a `type: "result"` line, else None.
    Mutates tool_counts and skill_counts in place.
    """
    msg_type = msg.get("type")
    if msg_type == "assistant":
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1
            if name == "Skill":
                skill = block.get("input", {}).get("skill")
                if skill:
                    skill_counts[skill] = skill_counts.get(skill, 0) + 1
    elif msg_type == "result":
        return dict(msg)
    return None


def parse_stream(stdout: bytes) -> dict:
    """Batch parser for stream-json NDJSON output from `claude -p`.

    Used by tests and smoke scripts. Runtime path uses _exec which streams
    line-by-line to avoid buffering the full output in memory.
    """
    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    result: dict = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("parse_stream: skipping malformed line: %s", line[:120])
            continue
        r = _fold(msg, tool_counts, skill_counts)
        if r is not None:
            result = r
    if result:
        result["tool_counts"] = tool_counts
        result["skill_counts"] = skill_counts
    return result


async def _exec(workspace: Path, cmd: list[str]) -> dict:
    logger.debug("exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:6]) + "...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    assert proc.stdout is not None and proc.stderr is not None

    # Drain stderr concurrently so the subprocess can't block on a full pipe.
    stderr_task = asyncio.create_task(proc.stderr.read())

    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    result: dict = {}
    line_count = 0
    try:
        async for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            line_count += 1
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("exec: skipping malformed line: %s", line[:120])
                continue
            r = _fold(msg, tool_counts, skill_counts)
            if r is not None:
                result = r
    except BaseException:
        stderr_task.cancel()
        raise
    finally:
        try:
            stderr_bytes = await stderr_task
        except asyncio.CancelledError:
            stderr_bytes = b""

    await proc.wait()
    logger.debug(
        "exec: returncode=%s lines=%d stderr=%d bytes",
        proc.returncode,
        line_count,
        len(stderr_bytes),
    )

    if result:
        result["tool_counts"] = tool_counts
        result["skill_counts"] = skill_counts

    if proc.returncode != 0:
        err_stderr = stderr_bytes.decode().strip()
        logger.debug(
            "exec: failed: stderr=%s parsed_result=%s",
            err_stderr[:200],
            str(result.get("result", ""))[:200],
        )
        if result.get("result"):
            raise RuntimeError(result["result"])
        raise RuntimeError(err_stderr or f"Exit code {proc.returncode}")
    if result.get("is_error") or result.get("subtype", "").startswith("error"):
        raise RuntimeError(result.get("result", "Unknown error"))
    return result


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
        "stream-json",
        "--verbose",
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
        tool_counts=cli_output.get("tool_counts", {}),
        skill_counts=cli_output.get("skill_counts", {}),
    )
