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
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rich.markup import escape as _escape_markup
from rich.text import Text

logger = logging.getLogger(__name__)

_SKIP_TYPES = {"ai-title", "attachment", "last-prompt", "pr-link", "system"}
_RULE_FILL = "━" * 60  # generous trailing run; terminals wider than ~80 cols swallow it
_BODY_INDENT = "    "
# ANSI escape runs (CSI, plus OSC strings) that a transcript can carry verbatim.
# Claude Code writes the stdout of a local command into the transcript as-is, so
# anything the command coloured arrives with its escapes intact.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _safe(value: Any) -> str:
    """Transcript text as a markup-safe, escape-free string.

    Everything here is rendered through Rich markup, and a transcript is *data*
    we don't author — so a body containing `[/path/to/thing]` reads as a closing
    tag and raises MarkupError, taking the whole TUI down. That is not
    hypothetical: a `PostCompact [/Users/…/postcompact_envelope.sh] completed`
    notice did exactly that.

    Call this once, where a value is pulled out of the event. Escaping is not
    idempotent (a second pass shows the backslashes), so downstream helpers must
    assume their input is already safe.
    """
    text = value if isinstance(value, str) else str(value or "")
    return _escape_markup(_ANSI_RE.sub("", text))


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
            return _first_line_with_count(_truncate(_safe(val), 200))
    first = next(iter(args))
    return f"{_safe(first)}=…"


def _format_user(raw: dict, ts: str) -> str | None:
    content = raw.get("message", {}).get("content")
    if isinstance(content, str):
        body = _safe(content).strip()
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
            body_str = _first_line_with_count(_truncate(_safe(body), 200))
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
            text = _safe(block.get("text")).strip()
            if not text:
                continue
            header = _rule("green", ts, "ASSISTANT")
            out_lines.append(f"\n{header}\n{_indent_body(text)}")
        elif bt == "thinking":
            think = _safe(block.get("thinking")).strip()
            if not think:
                continue
            first = think.split("\n", 1)[0]
            out_lines.append(
                f"[dim italic]{ts} ▸ thinking  {_truncate(first, 200)}[/dim italic]"
            )
        elif bt == "tool_use":
            name = _safe(block.get("name") or "?")
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
    body = _safe(raw.get("result")).strip()
    if body:
        return f"\n{header}\n{_indent_body(body, max_chars=400)}"
    return f"\n{header}"


def format_event(raw: dict) -> str | None:
    """Render one parsed JSONL event with Rich markup. None means skip.

    The result is guaranteed to parse as markup — see `_guarantee_parseable`.
    Callers render it straight into a Textual RichLog or a Rich Console, and a
    viewer that dies on the file it is tailing is worse than one that renders a
    line plainly.
    """
    return _guarantee_parseable(_format_event(raw))


def _guarantee_parseable(formatted: str | None) -> str | None:
    """Return `formatted` if Rich can parse it, else a fully escaped fallback.

    Belt and braces over `_safe`: that escapes every value we know about, but the
    panes are otherwise one missed interpolation away from taking the whole TUI
    down. Degrading to a plain line keeps the content visible, and the warning
    says which event to go and look at rather than swallowing the problem.
    """
    if not formatted:
        return formatted
    try:
        Text.from_markup(formatted)
    except Exception as exc:
        logger.warning("watch: unparseable markup (%s), falling back to plain", exc)
        return _escape_markup(formatted)
    return formatted


def _format_event(raw: dict) -> str | None:
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
    # Legacy codex emits a single top-level `message` type rather than claude's
    # separate user/assistant types.
    if t == "message":
        return _format_message_event(raw, ts)
    # codex_exec wraps each turn item: type=response_item with the real message
    # under payload (payload.type=message, role, content). reasoning /
    # function_call payloads carry no text and fall through to None.
    if t == "response_item":
        payload = raw.get("payload")
        return _format_message_event(payload, ts) if isinstance(payload, dict) else None
    return None


def _extract_message_text(content: Any) -> str:
    """Renderable text from a codex message's content — a string, or a list
    of blocks carrying a `text` field ({type: text | input_text | output_text}).
    Tool / reasoning blocks without text are ignored."""
    if isinstance(content, str):
        return _safe(content).strip()
    if not isinstance(content, list):
        return ""
    parts = [
        _safe(block["text"])
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts).strip()


def _format_message_event(raw: dict, ts: str) -> str | None:
    """Render a codex `type:"message"` event. Some emitters nest role/content
    under `message`, others put them at the top level. Both carry Anthropic-ish
    content blocks with a `text` field, so one path handles both."""
    message = raw.get("message")
    msg = message if isinstance(message, dict) else raw
    text = _extract_message_text(msg.get("content"))
    if not text:
        return None
    role = msg.get("role")
    if role == "user":
        return f"\n{_rule('cyan', ts, 'USER')}\n{_indent_body(text)}"
    if role == "assistant":
        return f"\n{_rule('green', ts, 'ASSISTANT')}\n{_indent_body(text)}"
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
