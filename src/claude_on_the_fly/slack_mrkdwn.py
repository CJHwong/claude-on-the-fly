"""Convert GitHub-flavored Markdown to Slack mrkdwn.

The agent emits normal Markdown; Slack's mrkdwn is a different dialect that
renders ``**bold**``, ``# headings``, and ``| tables |`` as literal text. We
tokenize with markdown-it-py (already in the tree via rich) and re-emit each
node as mrkdwn, degrading gracefully where mrkdwn has no equivalent:

    headings      -> *bold*
    tables        -> aligned monospace inside a code fence
    deep nesting  -> flattened, indented bullets

The content of a code fence or an inline code span passes through untouched, so
asterisks/pipes inside one survive; the spacing *around* an inline span is still
adjusted, because Slack needs a boundary there. Raw inline HTML (e.g. Slack
``<@U123>`` mentions) is preserved.

Reply bodies do NOT come through here. They ship as a ``markdown`` block, which
Slack parses server-side into real lists and tables -- everything the degrading
above gives up. This conversion is for the surfaces that have no markdown block:
a ``context`` element takes ``mrkdwn`` and nothing else, which is where the
mid-turn progress line lands.
"""

from __future__ import annotations

import string
import unicodedata
from collections.abc import Callable

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# CommonMark + tables + strikethrough. Linkify stays OFF so bare URLs pass
# through as text instead of being rewritten into <url|url> links.
_MD = MarkdownIt("commonmark").enable("table").enable("strikethrough")

# Slack has two boundary rules, and a marker with no boundary beside it arrives
# as a literal character.
#
# For *bold* / _italic_ / ~strike~ the boundary is whitespace, a line boundary,
# or half-width punctuation. CJK text has none of those between words, so the
# guard inserts a space to give the marker one. Backtick is half-width but not
# in string.punctuation.
_BOUNDARY = frozenset(string.punctuation + "`")

# For a `code span` the set is wider: punctuation counts at any width, so `x`。
# renders where *x*。 does not. Only a letter, a number, or a mark breaks a span,
# which leaves the categories below. Half-width is no exemption -- run`pytest`now
# fails in Slack exactly as 前面`x`後面 does.
_WORD_CATEGORIES = frozenset("LNM")

# `_` is the one hand-cut exception, and it is here for its meaning rather than
# its category: it is Pc, punctuation, so the rule above would call it a
# boundary, but it also opens italics in Slack and a span closing onto one does
# not render. Only the closing side was seen to fail; the guard fires on both
# because over-guarding costs a cosmetic space and under-guarding costs a broken
# span, and the opening side is untested rather than known good.
_CODE_NON_BOUNDARY = frozenset("_")


def _is_boundary(char: str) -> bool:
    return char.isspace() or char in _BOUNDARY


def _is_code_boundary(char: str) -> bool:
    """The same question for a code span, which Slack parses more loosely."""
    if char in _CODE_NON_BOUNDARY:
        return False
    return char.isspace() or unicodedata.category(char)[0] not in _WORD_CATEGORIES


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
    # A cell is rendered with the boundary guard off. The table lands inside a
    # fence, where Slack parses no mrkdwn at all, so a guard space buys nothing
    # there and costs twice: it shows up as a stray space in the cell, and it
    # widens the string the column arithmetic below measures, so it pushes every
    # other row's padding out with it.
    rows = [
        [_inline(cell, guard=False) for cell in row.children]
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


def _inline(
    node: SyntaxTreeNode, flatten_strong: bool = False, guard: bool = True
) -> str:
    """Render the inline children of a block node (paragraph, heading, cell).

    Emphasis markers and code spans need a boundary on each side or Slack
    renders them literally, so the guard lives at this join point, where the
    neighbouring characters are known. Each form brings its own boundary test.
    `guard=False` turns it off for content bound for a fence, and has to travel
    down with `flatten_strong` because emphasis nests.
    """
    pairs = []
    for child in node.children or []:
        part = _render_inline(child, flatten_strong, guard)
        if part:
            pairs.append((child, part))
    out = []
    for index, (child, part) in enumerate(pairs):
        boundary = _boundary_test(child, flatten_strong) if guard else None
        if boundary:
            if index and not boundary(pairs[index - 1][1][-1]):
                part = f" {part}"
            if index + 1 < len(pairs) and not boundary(pairs[index + 1][1][0]):
                part = f"{part} "
        out.append(part)
    return "".join(out)


def _boundary_test(
    node: SyntaxTreeNode, flatten_strong: bool
) -> Callable[[str], bool] | None:
    """The boundary rule this child's markers answer to, or None when it
    emitted none. A flattened strong renders as plain text instead."""
    if node.type in ("em", "s") or (node.type == "strong" and not flatten_strong):
        return _is_boundary
    if node.type == "code_inline":
        return _is_code_boundary
    return None


def _render_inline(
    node: SyntaxTreeNode, flatten_strong: bool = False, guard: bool = True
) -> str:
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
        inner = _inline(node, guard=guard)
        return inner if flatten_strong else f"*{inner}*"
    if kind == "em":
        return f"_{_inline(node, flatten_strong, guard)}_"
    if kind == "s":
        return f"~{_inline(node, flatten_strong, guard)}~"
    if kind == "link":
        return _render_link(node, flatten_strong, guard)
    if kind == "image":
        return str(node.attrs.get("src", "")) or _inline(node, guard=guard)
    # `node.content` is the raw source, meant for leaf unknowns like entity.
    # For a container whose children all render empty (an image with no src and
    # no alt), resurrecting the source would leak literal `![]()` into the
    # message — same class of noise as a literal `---` or `<div>`.
    return _inline(node, flatten_strong, guard) if node.children else node.content


def _render_link(
    node: SyntaxTreeNode, flatten_strong: bool = False, guard: bool = True
) -> str:
    href = node.attrs.get("href", "")
    label = _inline(node, flatten_strong, guard)
    if not href:
        return label
    return f"<{href}|{label}>" if label and label != href else f"<{href}>"


# Slack rejects a markdown block whose text exceeds this. Measured against a live
# workspace: 12000 passes and 12001 fails, and the count is characters rather than
# bytes -- 12000 CJK characters (36000 bytes) go through. That is four times what a
# `section` holds.
SLACK_MARKDOWN_LIMIT = 12000

# Slack caps the whole message as well as one block, and the message cap is the
# smaller of the two. Slack publishes no number for it: the method reference
# defers block limits to the block types, and `msg_blocks_too_long` comes back
# with no count. What this install has measured is a 5,732 character body
# accepted and a 15,181 character body rejected, with no send landing between.
# 5,500 keeps a message under the accepted one whichever half of the payload
# Slack counts, because the body ships twice: once as the message `text`, once
# as its markdown blocks.
SLACK_MESSAGE_CHAR_LIMIT = 5500

# Slack caps the `text` argument of `chat.update` at 4,000, and the count is
# UTF-8 bytes rather than the characters the method reference names: this
# install measured a 1,993-character (4,121-byte) CJK reply rejected with
# `msg_too_long` and a 2,035-character (3,971-byte) one accepted. The only
# `chat.update` that re-sends a reply body is the suggestion-menu retire, so
# every reply part posts its markdown block at full length and fits only its
# `text` fallback under this ceiling. That fallback is what a later
# `chat.update` re-sends, so fitting it there is the safety space: the split
# itself needs none, because `chat.postMessage` truncates at 40k instead of
# rejecting.
SLACK_UPDATE_TEXT_MAX_BYTES = 4000
# The retire re-sends the stored text with room to spare under the 4,000-byte
# cap, so a measured limit drift or a surrogate pair at the cut cannot flip the
# update to `msg_too_long`.
SLACK_TEXT_FIELD_BYTES = 3800

_FENCE_MARKERS = ("```", "~~~")


def fit_text_field(text: str, max_bytes: int = SLACK_TEXT_FIELD_BYTES) -> str:
    """A `text` field value Slack can re-send through `chat.update` intact.

    Slack rejects an update whose `text` exceeds `SLACK_UPDATE_TEXT_MAX_BYTES`
    UTF-8 bytes with `msg_too_long`, and a CJK body passes the character count
    the reference names long after it fails the byte count. Long bodies are cut
    on a code-point boundary with a trailing ellipsis; blocks carry the full
    content, so only the notification fallback loses tail text.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # "…" costs 3 bytes, so the kept slice targets max_bytes - 3. Stepping back
    # from the cut while it lands on a continuation byte ends the slice on a
    # codepoint boundary, and `errors="ignore"` makes that belt-and-suspenders.
    cut = max_bytes - len("…".encode())
    while cut > 0 and cut < len(encoded) and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore") + "…"


def _fence_marker(line: str) -> str | None:
    stripped = line.lstrip()
    for marker in _FENCE_MARKERS:
        if stripped.startswith(marker):
            return marker
    return None


def _open_fence_at(lines: list[str]) -> list[bool]:
    """Mark the lines that leave a code fence open: the opening marker and every
    line inside it, the closing marker excluded.

    A split may not land after one of these. An unclosed fence at the end of a
    message swallows the text after it, and the next message opens a fence that
    closes somewhere else. A split before the opening marker is fine, and so is
    one after the closing marker.
    """
    open_fence = [False] * len(lines)
    opened: str | None = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if opened is None:
            if marker is None:
                continue
            opened = marker
        elif marker == opened:
            opened = None
            continue
        open_fence[index] = True
    return open_fence


def _in_table(lines: list[str], open_fence: list[bool]) -> list[bool]:
    """Mark the lines that carry a table row: a non-blank line with a pipe in it,
    outside a fence. Coarser than the markdown table grammar on purpose. A prose
    line that holds a stray pipe only costs a worse split point, while a table
    cut at a row boundary loses its header row and Slack renders the tail as
    plain text."""
    return [
        "|" in line and bool(line.strip()) and not open_fence[index]
        for index, line in enumerate(lines)
    ]


def _cut_points(
    lines: list[str], open_fence: list[bool], in_table: list[bool]
) -> tuple[list[bool], list[bool]]:
    """Boundaries a split may land on, indexed by the line that would open the
    next chunk. Returns the block boundaries (a blank line ends a markdown block)
    and the line boundaries a split falls back to."""
    block_cut = [False] * len(lines)
    line_cut = [False] * len(lines)
    for index in range(1, len(lines)):
        previous = index - 1
        if open_fence[previous]:
            continue  # the chunk would end with the fence still open
        if in_table[previous] and in_table[index]:
            continue  # a table keeps its header with its rows
        line_cut[index] = True
        block_cut[index] = not lines[previous].strip()
    return block_cut, line_cut


def _rendered(lines: list[str], start: int, end: int) -> str:
    """The chunk holding lines [start, end). The newline the split consumed rides
    with the following chunk, so the chunks still reassemble into the input."""
    leading = "\n" if start else ""
    return leading + "\n".join(lines[start:end])


def _sliced_line(line: str, leading_newline: bool, limit: int) -> list[str]:
    """One line longer than the limit, cut into limit-sized pieces."""
    segment = f"\n{line}" if leading_newline else line
    pieces: list[str] = []
    while len(segment) > limit:
        pieces.append(segment[:limit])
        segment = segment[limit:]
    pieces.append(segment)
    return pieces


def _next_cut(start: int, end: int, block_cut: list[bool], line_cut: list[bool]) -> int:
    """The line the next chunk opens on: the last block boundary that fits, else
    the last line boundary, else `end`, which cuts inside a fence or a table and
    is what a table longer than the whole limit leaves."""
    for allowed in (block_cut, line_cut):
        for index in range(end, start, -1):
            if allowed[index]:
                return index
    return end


def split_blocks(text: str, limit: int = SLACK_MARKDOWN_LIMIT) -> list[str]:
    """Split text into chunks of at most `limit` characters.

    Lossless: every character of `text`, newlines included, lands in exactly one
    chunk in order, so ``"".join(split_blocks(text)) == text``.

    Split points, in the order they are taken:

    1. a blank line outside a fence and outside a table. That is a markdown
       block boundary, so a paragraph, a list and a table stay in one piece.
    2. any line boundary outside a fence and outside a table.
    3. `end`, when neither of the above fits, so a table or a fence longer than
       `limit` is cut mid-way. Nothing else is left, and dropping the tail is not
       an option: the reader would have no way to tell it happened.
    """
    lines = text.split("\n")
    open_fence = _open_fence_at(lines)
    in_table = _in_table(lines, open_fence)
    block_cut, line_cut = _cut_points(lines, open_fence, in_table)
    total = len(lines)

    chunks: list[str] = []
    start = 0
    while start < total:
        first = start == 0  # a later chunk carries the newline the split consumed
        room = limit if first else limit - 1
        if len(lines[start]) > room:
            chunks.extend(_sliced_line(lines[start], not first, limit))
            start += 1
            continue
        end = start + 1
        while end < total and len(_rendered(lines, start, end + 1)) <= limit:
            end += 1
        if end >= total:
            chunks.append(_rendered(lines, start, total))
            break
        cut = _next_cut(start, end, block_cut, line_cut)
        chunks.append(_rendered(lines, start, cut))
        start = cut
    return chunks or [""]
