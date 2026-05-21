"""Live-tail formatter for a Claude Code session JSONL.

`tail()` polls a file and yields each appended NDJSON event as a dict.
`format_event()` turns one event into a human-readable string with Rich markup
(or None to skip). The CLI prints via a Rich Console so ANSI renders in a TTY;
Textual's RichLog parses the same markup natively.

Visual hierarchy:
    - USER and ASSISTANT messages get a colored rule + indented body
    - DONE summary gets a rule too
    - thinking / tool / result lines stay inline + dim, prefixed with action
      and response glyphs (▸ / ◂)
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


_SKIP_TYPES = {"ai-title", "attachment", "last-prompt", "pr-link", "system"}
_RULE_FILL = "━" * 60  # generous trailing run; terminals wider than ~80 cols swallow it
_BODY_INDENT = "    "


def _short_ts(ts: Any) -> str:
    """`2026-05-21T04:53:36.096Z` -> `04:53:36`. Blank-padded when missing."""
    if not isinstance(ts, str) or len(ts) < 19:
        return "        "
    return ts[11:19]


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _first_line_with_count(text: str) -> str:
    """`foo\\nbar\\nbaz` -> `foo (+2 lines)`. Single-line text returned as-is."""
    if "\n" not in text:
        return text
    lines = [ln for ln in text.split("\n") if ln]
    if not lines:
        return ""
    head = lines[0]
    remaining = len(lines) - 1
    if remaining <= 0:
        return head
    return f"{head} [dim italic](+{remaining} lines)[/dim italic]"


def _indent_body(text: str, max_chars: int = 800) -> str:
    """Indent each line of `text` by _BODY_INDENT and cap total length."""
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return "\n".join(_BODY_INDENT + ln for ln in text.split("\n"))


def _rule(color: str, ts: str, label: str, extra: str = "") -> str:
    """`━━━ 06:13:00 USER ━━━━━━━...` with the timestamp/label/extra inline."""
    title = f"{ts} {label}"
    if extra:
        title = f"{title}  {extra}"
    return f"[bold {color}]━━━ {title} {_RULE_FILL}[/bold {color}]"


_TOOL_ARG_KEYS = ("file_path", "path", "command", "url", "query", "pattern", "skill")


def _format_tool_args(args: dict) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    for key in _TOOL_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str):
            return _first_line_with_count(_truncate(val, 200))
    first = next(iter(args))
    return f"{first}=…"


def _format_user(raw: dict, ts: str) -> str | None:
    content = raw.get("message", {}).get("content")
    if isinstance(content, str):
        body = content.strip()
        if not body:
            return None
        header = _rule("cyan", ts, "USER")
        return f"\n{header}\n{_indent_body(body)}"
    if isinstance(content, list):
        lines: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            body_str = _first_line_with_count(_truncate(str(body or ""), 200))
            lines.append(f"[dim]{ts} ◂ result    {body_str}[/dim]")
        return "\n".join(lines) if lines else None
    return None


def _format_assistant(raw: dict, ts: str) -> str | None:
    content = raw.get("message", {}).get("content") or []
    if not isinstance(content, list):
        return None
    out_lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = (block.get("text") or "").strip()
            if not text:
                continue
            header = _rule("green", ts, "ASSISTANT")
            out_lines.append(f"\n{header}\n{_indent_body(text)}")
        elif bt == "thinking":
            think = (block.get("thinking") or "").strip()
            if not think:
                continue
            first = think.split("\n", 1)[0]
            out_lines.append(
                f"[dim italic]{ts} ▸ thinking  {_truncate(first, 200)}[/dim italic]"
            )
        elif bt == "tool_use":
            name = block.get("name", "?")
            args = _format_tool_args(block.get("input") or {})
            args_chunk = f"  {args}" if args else ""
            out_lines.append(
                f"[dim]{ts} ▸ [/dim][yellow]{name}[/yellow][dim]{args_chunk}[/dim]"
            )
    return "\n".join(out_lines) if out_lines else None


def _format_result(raw: dict, ts: str) -> str:
    cost = raw.get("total_cost_usd") or 0
    dur_ms = raw.get("duration_ms") or 0
    cost_str = f"${cost:.4f}"
    dur_str = f"{dur_ms / 1000:.1f}s" if dur_ms else "-"
    extra = f"{cost_str} | {dur_str}"
    header = _rule("magenta", ts, "DONE", extra)
    body = (raw.get("result") or "").strip()
    if body:
        return f"\n{header}\n{_indent_body(body, max_chars=400)}"
    return f"\n{header}"


def format_event(raw: dict) -> str | None:
    """Render one parsed JSONL event with Rich markup. None means skip."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t in _SKIP_TYPES:
        return None
    ts = _short_ts(raw.get("timestamp"))
    if t == "user":
        return _format_user(raw, ts)
    if t == "assistant":
        return _format_assistant(raw, ts)
    if t == "result":
        return _format_result(raw, ts)
    return None


def tail(path: Path, *, poll_s: float = 0.5) -> Iterator[dict]:
    """Yield each NDJSON event from `path` as it's appended.

    Starts from the beginning of the file, yields existing events first, then
    blocks polling for new lines. Caller is expected to wrap in try/except
    KeyboardInterrupt for clean exit. Skips malformed lines silently.
    """
    with path.open("r") as f:
        buf = ""
        while True:
            chunk = f.read()
            if not chunk:
                time.sleep(poll_s)
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
