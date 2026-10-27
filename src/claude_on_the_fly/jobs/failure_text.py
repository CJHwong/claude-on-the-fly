"""Failure text fit for a chat surface.

A failed job's text is whatever the backend raised, and two codex shapes carry
host details: the `codex exec` banner and the screen a hosted turn leaves in its
pane. Both the failure alert and the origin-thread reply can land in a public
channel, so both run the text through `public_failure_text`. The raw text is
left alone everywhere else: the entry log and quota detection read all of it.
"""

from __future__ import annotations

import re

# What a reader gets when the failure text says nothing on its own.
SEE_THE_LOG = "See the entry log."

# `codex exec` writes this banner to stderr before anything else, and the
# backend raises that stderr on a failing exit. It names the workspace path, the
# sandbox mode and the session id, none of which belong in a monitoring
# channel. Shape measured on codex 0.154.0.
_CODEX_BANNER = re.compile(
    r"(?:Reading additional input from stdin\.\.\.\n)?"
    r"OpenAI Codex v\S+[^\n]*\n-{4,}\n(?:[^\n]*\n)*?-{4,}\n"
)
# After the banner codex echoes the whole prompt under a `user` line, with no
# closing delimiter. The echo ends at the first line codex itself writes.
_CODEX_NARRATION = re.compile(
    r"^(?:thinking|codex|exec|tokens used|(?:ERROR|warning|hook|mcp startup):.*)$",
    re.MULTILINE,
)

# A hosted codex turn that dies before it answers raises the last screen of its
# pane. The redraw drops most line breaks, so the start screen, the workspace
# path and the permission mode run together, and codex's own error sits among
# them. Any one marker counts: the backend keeps only the pane's tail, which can
# cut the header off. Shape seen on codex 0.158.0 to 0.163.0.
_CODEX_TUI_SCREEN = re.compile(
    r">_ ?OpenAI Codex \(v|Codex \(v\d|Ask Codex to do anything"
    r"|permissions: \S+ mode|~/\.claude-on-the-fly/workspaces/"
)
_CODEX_TUI_ERROR = re.compile(r"error:[^\n]*", re.IGNORECASE)


def _without_codex_banner(text: str) -> str:
    """Drop the banner and the prompt echo, keep what codex said after them.

    Only the failure lines are kept when there are any: the warnings and hook
    lines before them would otherwise use the whole character budget.
    """
    match = _CODEX_BANNER.search(text)
    if match is None:
        return text
    head = text[: match.start()].rstrip()
    rest = text[match.end() :]
    if rest.startswith("user\n"):
        narration = _CODEX_NARRATION.search(rest, len("user\n"))
        rest = rest[narration.start() :] if narration else ""
    errors = [line for line in rest.splitlines() if line.startswith("ERROR:")]
    if errors:
        rest = "\n".join(dict.fromkeys(errors))
    return "\n".join(part for part in (head, rest.strip() or SEE_THE_LOG) if part)


def _without_codex_screen(text: str) -> str:
    """Keep the text before the screen and codex's error line, drop the screen.

    The first error wins, because a wrapped error quotes its cause after it, and
    the error ends at its line: the pane's footer rows can follow it.
    """
    match = _CODEX_TUI_SCREEN.search(text)
    if match is None:
        return text
    head = text[: match.start()].rstrip()
    error = _CODEX_TUI_ERROR.search(text, match.start())
    detail = error[0].strip() if error else SEE_THE_LOG
    return "\n".join(part for part in (head, detail) if part)


def public_failure_text(text: str) -> str:
    """The failure text without the codex banner or the codex start screen."""
    return _without_codex_screen(_without_codex_banner(text))
