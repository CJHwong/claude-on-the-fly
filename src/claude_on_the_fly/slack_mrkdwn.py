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

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# CommonMark + tables + strikethrough. Linkify stays OFF so bare URLs pass
# through as text instead of being rewritten into <url|url> links.
_MD = MarkdownIt("commonmark").enable("table").enable("strikethrough")


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
        return f"*{_inline(node)}*"
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
    if not rows:
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


def _inline(node: SyntaxTreeNode) -> str:
    """Render the inline children of a block node (paragraph, heading, cell)."""
    return "".join(_render_inline(child) for child in node.children or [])


def _render_inline(node: SyntaxTreeNode) -> str:
    kind = node.type
    if kind == "text":
        return node.content
    if kind == "code_inline":
        return f"`{node.content}`"
    if kind in ("softbreak", "hardbreak"):
        return "\n"
    if kind in ("html_inline", "html_block"):
        return node.content  # preserve raw Slack markup like <@U123>
    if kind == "strong":
        return f"*{_inline(node)}*"
    if kind == "em":
        return f"_{_inline(node)}_"
    if kind == "s":
        return f"~{_inline(node)}~"
    if kind == "link":
        return _render_link(node)
    if kind == "image":
        return str(node.attrs.get("src", "")) or _inline(node)
    return _inline(node) or node.content


def _render_link(node: SyntaxTreeNode) -> str:
    href = node.attrs.get("href", "")
    label = _inline(node)
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
