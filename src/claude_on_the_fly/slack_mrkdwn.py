"""Convert GitHub-flavored Markdown to Slack mrkdwn.

The agent emits normal Markdown; Slack's mrkdwn is a different dialect that
renders ``**bold**``, ``# headings``, and ``| tables |`` as literal text. We
tokenize with markdown-it-py (already in the tree via rich) and re-emit each
node as mrkdwn, degrading gracefully where mrkdwn has no equivalent:

    headings      -> *bold*
    tables        -> aligned monospace inside a code fence
    deep nesting  -> flattened, indented bullets

Code fences and inline code pass through untouched, so asterisks/pipes inside
them survive. Raw inline HTML (e.g. Slack ``<@U123>`` mentions) is preserved.
"""

from __future__ import annotations

import string

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# CommonMark + tables + strikethrough. Linkify stays OFF so bare URLs pass
# through as text instead of being rewritten into <url|url> links.
_MD = MarkdownIt("commonmark").enable("table").enable("strikethrough")

# Slack parses *bold* / _italic_ / ~strike~ only when the character outside
# each marker is whitespace, a line boundary, or half-width punctuation. CJK
# text has none of those between words, so a converted marker would arrive as
# a literal character; the guard inserts a space to give the marker a
# boundary. Backtick is half-width but not in string.punctuation.
_BOUNDARY = frozenset(string.punctuation + "`")


def _is_boundary(char: str) -> bool:
    return char.isspace() or char in _BOUNDARY


def to_mrkdwn(text: str) -> str:
    """Render Markdown as Slack mrkdwn. Empty/blank input is returned as-is."""
    if not text.strip():
        return text
    root = SyntaxTreeNode(_MD.parse(text))
    blocks = [_render_block(child, 0) for child in root.children]
    return "\n\n".join(block for block in blocks if block)


def _render_block(node: SyntaxTreeNode, depth: int) -> str:
    kind = node.type
    if kind == "paragraph":
        return _inline(node)
    if kind == "heading":
        # The wrapper's *…* would pair with a strong's asterisks instead of its
        # own, so strong renders as plain text inside a heading. Bold-in-bold
        # is invisible anyway, and the whole line stays bold.
        return f"*{_inline(node, flatten_strong=True)}*"
    if kind in ("fence", "code_block"):
        return _fence(node.content)
    if kind in ("bullet_list", "ordered_list"):
        return _render_list(node, depth)
    if kind == "blockquote":
        return _render_blockquote(node, depth)
    if kind == "table":
        return _render_table(node)
    if kind == "hr":
        return ""  # mrkdwn has no horizontal rule
    return "\n\n".join(_render_block(child, depth) for child in node.children)


def _render_list(node: SyntaxTreeNode, depth: int) -> str:
    ordered = node.type == "ordered_list"
    start = int(node.attrs.get("start", 1)) if ordered else 1
    indent = "    " * depth
    lines = []
    for offset, item in enumerate(node.children):
        marker = f"{start + offset}." if ordered else "-"
        lines.append(_render_list_item(item, marker, indent, depth))
    return "\n".join(lines)


def _render_list_item(
    item: SyntaxTreeNode, marker: str, indent: str, depth: int
) -> str:
    pieces = [_render_item_child(child, depth) for child in item.children]
    pieces = [piece for piece in pieces if piece]
    head = pieces[0] if pieces else ""
    line = f"{indent}{marker} {head}"
    rest = pieces[1:]
    return line + ("\n" + "\n".join(rest) if rest else "")


def _render_item_child(child: SyntaxTreeNode, depth: int) -> str:
    if child.type in ("bullet_list", "ordered_list"):
        return _render_list(child, depth + 1)
    return _render_block(child, depth)


def _render_blockquote(node: SyntaxTreeNode, depth: int) -> str:
    inner = "\n\n".join(_render_block(child, depth) for child in node.children)
    return "\n".join(f"> {line}" for line in inner.split("\n"))


def _render_table(node: SyntaxTreeNode) -> str:
    rows = [
        [_inline(cell) for cell in row.children]
        for section in node.children
        for row in section.children
    ]
    # No rows at all, or rows whose every cell is empty. A header-only table is
    # the common source of the second case, and it used to render as a bare ```
    # fence around a blank line, which reads as a broken reply rather than as an
    # absent table.
    if not any(cell.strip() for row in rows for cell in row):
        return ""
    ncols = max(len(row) for row in rows)
    rows = [row + [""] * (ncols - len(row)) for row in rows]
    widths = [max(len(row[col]) for row in rows) for col in range(ncols)]
    lines = [
        " | ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip()
        for row in rows
    ]
    return _fence("\n".join(lines))


def _fence(content: str) -> str:
    return f"```\n{content.rstrip(chr(10))}\n```"


def _inline(node: SyntaxTreeNode, flatten_strong: bool = False) -> str:
    """Render the inline children of a block node (paragraph, heading, cell).

    Emphasis markers need a boundary on each side or Slack renders them
    literally, so the guard lives at this join point, where the neighbouring
    characters are known.
    """
    pairs = []
    for child in node.children or []:
        part = _render_inline(child, flatten_strong)
        if part:
            pairs.append((child, part))
    out = []
    for index, (child, part) in enumerate(pairs):
        if _emits_markers(child, flatten_strong):
            if index and not _is_boundary(pairs[index - 1][1][-1]):
                part = f" {part}"
            if index + 1 < len(pairs) and not _is_boundary(pairs[index + 1][1][0]):
                part = f"{part} "
        out.append(part)
    return "".join(out)


def _emits_markers(node: SyntaxTreeNode, flatten_strong: bool) -> bool:
    """True when this child rendered as *…* / _…_ / ~…~ — the marker forms
    that need a boundary. A flattened strong renders as plain text instead."""
    return node.type in ("em", "s") or (node.type == "strong" and not flatten_strong)


def _render_inline(node: SyntaxTreeNode, flatten_strong: bool = False) -> str:
    kind = node.type
    if kind == "text":
        return node.content
    if kind == "code_inline":
        return f"`{node.content}`"
    if kind in ("softbreak", "hardbreak"):
        return "\n"
    if kind == "html_inline":
        # Preserved rather than escaped: an agent writing raw Slack markup mid-
        # sentence means it. Block-level HTML does not arrive here — it is a block
        # node, so `_render_block` handles it, and its fallthrough drops it.
        return node.content
    if kind == "strong":
        return _inline(node) if flatten_strong else f"*{_inline(node)}*"
    if kind == "em":
        return f"_{_inline(node, flatten_strong)}_"
    if kind == "s":
        return f"~{_inline(node, flatten_strong)}~"
    if kind == "link":
        return _render_link(node, flatten_strong)
    if kind == "image":
        return str(node.attrs.get("src", "")) or _inline(node)
    # `node.content` is the raw source, meant for leaf unknowns like entity.
    # For a container whose children all render empty (an image with no src and
    # no alt), resurrecting the source would leak literal `![]()` into the
    # message — same class of noise as a literal `---` or `<div>`.
    return _inline(node, flatten_strong) if node.children else node.content


def _render_link(node: SyntaxTreeNode, flatten_strong: bool = False) -> str:
    href = node.attrs.get("href", "")
    label = _inline(node, flatten_strong)
    if not href:
        return label
    return f"<{href}|{label}>" if label and label != href else f"<{href}>"


# Slack rejects a section block whose text exceeds this, so a long reply has to
# be laid across several blocks. Lives here beside `to_mrkdwn` because every
# caller that splits has already converted: chunking is the last step of
# rendering mrkdwn for Slack, not a concern of any one frontend.
SLACK_BLOCK_LIMIT = 3000


def split_blocks(text: str) -> list[str]:
    """Split text into chunks within Slack's per-block limit, preferring line
    breaks.

    Lossless: every character of `text`, newlines included, lands in exactly one
    chunk in order, so ``"".join(split_blocks(text)) == text``. A single line
    longer than the limit is sliced into limit-sized pieces rather than cut off,
    because the alternative is dropping the tail of somebody's output with no
    error and no log line — and the reader has no way to tell it happened.
    """
    chunks: list[str] = []
    chunk = ""
    for index, line in enumerate(text.split("\n")):
        segment = f"\n{line}" if index else line  # restore the split newline
        if len(chunk) + len(segment) <= SLACK_BLOCK_LIMIT:
            chunk += segment
            continue
        # Overflow: flush the running chunk, then lay `segment` down, slicing it
        # into limit-sized pieces if the line alone exceeds the limit.
        if chunk:
            chunks.append(chunk)
            chunk = ""
        while len(segment) > SLACK_BLOCK_LIMIT:
            chunks.append(segment[:SLACK_BLOCK_LIMIT])
            segment = segment[SLACK_BLOCK_LIMIT:]
        chunk = segment
    if chunk:
        chunks.append(chunk)
    return chunks or [""]
