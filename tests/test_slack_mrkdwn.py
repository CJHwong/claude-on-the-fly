"""Tests for the Markdown -> Slack mrkdwn converter."""

import string

import pytest

from claude_on_the_fly.slack_mrkdwn import to_mrkdwn


def test_bold_becomes_single_asterisk():
    assert to_mrkdwn("**bold**") == "*bold*"


def test_italic_becomes_underscore():
    assert to_mrkdwn("*italic*") == "_italic_"
    assert to_mrkdwn("_italic_") == "_italic_"


def test_strikethrough_becomes_single_tilde():
    assert to_mrkdwn("~~gone~~") == "~gone~"


def test_heading_becomes_bold():
    assert to_mrkdwn("# Title") == "*Title*"
    assert to_mrkdwn("### Deep") == "*Deep*"


def test_horizontal_rule_is_dropped():
    assert to_mrkdwn("a\n\n---\n\nb") == "a\n\nb"


def test_link_becomes_slack_link():
    assert to_mrkdwn("[GoFreight](https://gofreight.com)") == (
        "<https://gofreight.com|GoFreight>"
    )


def test_bare_autolink_keeps_no_label():
    assert to_mrkdwn("<https://x.dev>") == "<https://x.dev>"


def test_inline_code_content_is_verbatim():
    assert to_mrkdwn("use `**not bold**` here") == "use `**not bold**` here"


def test_code_fence_survives_untouched():
    src = "```\n**still literal**\n# not a heading\n| a | b |\n```"
    assert to_mrkdwn(src) == "```\n**still literal**\n# not a heading\n| a | b |\n```"


def test_code_fence_language_hint_is_dropped():
    assert to_mrkdwn("```python\nx = 1\n```") == "```\nx = 1\n```"


def test_table_becomes_aligned_fence():
    src = "| Name | Status |\n|---|---|\n| edi | green |\n| falcon | degraded |"
    expected = "```\nName   | Status\nedi    | green\nfalcon | degraded\n```"
    assert to_mrkdwn(src) == expected


def test_unordered_list():
    assert to_mrkdwn("- one\n- two") == "- one\n- two"


def test_ordered_list_keeps_numbers():
    assert to_mrkdwn("1. first\n2. second") == "1. first\n2. second"


def test_nested_list_is_indented():
    src = "- top\n    - child"
    assert to_mrkdwn(src) == "- top\n    - child"


def test_blockquote_uses_slack_quote():
    assert to_mrkdwn("> quoted") == "> quoted"


def test_slack_mention_is_preserved():
    assert to_mrkdwn("hi <@U050Q31E4TB>") == "hi <@U050Q31E4TB>"


def test_plain_text_round_trips():
    assert to_mrkdwn("just a sentence.") == "just a sentence."


def test_blank_input_returned_as_is():
    assert to_mrkdwn("") == ""
    assert to_mrkdwn("   ") == "   "


def test_bold_inside_paragraph():
    assert to_mrkdwn("a **b** c") == "a *b* c"


def test_emphasis_gets_a_boundary_space_around_cjk():
    assert to_mrkdwn("這是**粗體**字") == "這是 *粗體* 字"
    assert to_mrkdwn("這是*斜體*嗎") == "這是 _斜體_ 嗎"
    assert to_mrkdwn("這是~~刪除~~字") == "這是 ~刪除~ 字"


def test_emphasis_gets_a_boundary_space_before_full_width_punctuation():
    assert to_mrkdwn("**結論**：可以做") == "*結論* ：可以做"


def test_emphasis_gets_a_boundary_space_inside_a_list_item():
    assert to_mrkdwn("- **項目**內容") == "- *項目* 內容"


def test_intraword_emphasis_gets_boundary_spaces():
    assert to_mrkdwn("un**bold**ed") == "un *bold* ed"


def test_emphasis_keeps_an_existing_boundary():
    assert to_mrkdwn("**Done**. next") == "*Done*. next"
    assert to_mrkdwn("這是 **粗體** 字") == "這是 *粗體* 字"
    assert to_mrkdwn("**開頭粗體** 在句首") == "*開頭粗體* 在句首"


def test_heading_with_bold_flattens_the_strong():
    assert to_mrkdwn("## Title: **key**") == "*Title: key*"
    assert to_mrkdwn("## **key** trailing") == "*key trailing*"
    assert to_mrkdwn("# **A** and **B**") == "*A and B*"
    assert to_mrkdwn("## 標題：**重點**") == "*標題：重點*"
    assert to_mrkdwn("## **key**") == "*key*"


def test_heading_with_italic_or_strike_keeps_them():
    assert to_mrkdwn("## *ital*") == "*_ital_*"
    assert to_mrkdwn("## ~~struck~~") == "*~struck~*"


def test_heading_with_bold_link_flattens_the_label():
    assert to_mrkdwn("## [**bold**](https://x.dev)") == "*<https://x.dev|bold>*"


def test_an_image_with_no_source_and_no_alt_renders_as_nothing():
    """No src, no alt, nothing to show — the paragraph drops rather than
    leaking a literal `![]()` into the message, same as a literal `---`."""
    assert to_mrkdwn("![]()") == ""
    assert to_mrkdwn("看圖 ![]() 說明") == "看圖  說明"


def test_an_entity_renders_as_its_decoded_character():
    """Entity nodes are leaf unknowns, so they fall back to their content."""
    assert to_mrkdwn("a &amp; b") == "a & b"
    assert to_mrkdwn("a &lt; b") == "a < b"


# Slack's boundary rule is half-width punctuation, whitespace, or a line
# boundary. Full-width forms (U+FF01-FF5E punctuation) and CJK punctuation look
# like boundaries but are not, so the guard must insert a space before them.
# U+3000 is the one full-width character that IS a boundary — it is whitespace.
_FULL_WIDTH_PUNCT = (
    "！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    "、。〃〈〉《》「」『』【】〔〕〖〗〘〙〚〛〜〝〞〟・…—"
)


@pytest.mark.parametrize(
    ("source", "mrkdwn"),
    [("**粗體**", "*粗體*"), ("*斜體*", "_斜體_"), ("~~刪除~~", "~刪除~")],
)
@pytest.mark.parametrize("punct", _FULL_WIDTH_PUNCT)
def test_full_width_punctuation_is_not_a_boundary(source, mrkdwn, punct):
    assert to_mrkdwn(f"{source}{punct}後") == f"{mrkdwn} {punct}後"
    assert to_mrkdwn(f"前{punct}{source}") == f"前{punct} {mrkdwn}"


# Backslash is a markdown escape character: `\**粗體**` parses as an escaped
# literal asterisk plus emphasis, so it changes the tree instead of sitting
# next to a marker as a plain neighbour. The guard itself treats it as a
# boundary, same as any other ASCII punctuation.
@pytest.mark.parametrize("punct", string.punctuation.replace("\\", ""))
def test_ascii_punctuation_is_a_boundary(punct):
    assert to_mrkdwn(f"**粗體**{punct}後") == f"*粗體*{punct}後"
    assert to_mrkdwn(f"前{punct}**粗體**") == f"前{punct}*粗體*"


def test_full_width_space_is_a_boundary():
    assert to_mrkdwn("**粗體**　後") == "*粗體*　後"
    assert to_mrkdwn("前　**粗體**") == "前　*粗體*"


def test_nested_emphasis_needs_no_boundary():
    assert to_mrkdwn("**bold _ital_**") == "*bold _ital_*"


def test_adjacent_emphasis_markers_need_no_boundary():
    assert to_mrkdwn("**a***b*") == "*a*_b_"


def test_emphasis_adjacent_to_a_code_span_keeps_the_backtick_boundary():
    assert to_mrkdwn("**a**`b`") == "*a*`b`"


def test_heading_with_code_span_keeps_the_span():
    assert to_mrkdwn("## `a*b`") == "*`a*b`*"


def test_bold_link_label_gets_no_boundary_spaces():
    assert to_mrkdwn("[**粗體**](https://x.dev)") == "<https://x.dev|*粗體*>"


def test_paragraphs_separated_by_blank_line():
    assert to_mrkdwn("first\n\nsecond") == "first\n\nsecond"


def test_a_horizontal_rule_disappears():
    """mrkdwn has no horizontal rule, and the literal `---` reads as a typo."""
    assert to_mrkdwn("above\n\n---\n\nbelow") == "above\n\nbelow"


def test_an_empty_table_renders_as_nothing():
    """A header-only table has no rows to align, and an empty code fence is worse
    than no table."""
    assert to_mrkdwn("|  |\n|--|") == ""


def test_a_table_cell_gets_no_boundary_spaces():
    """The table lands in a fence, where Slack parses nothing, so a boundary
    space there is a stray character rather than a guard."""
    src = "| 欄位 |\n| --- |\n| 前面**粗**後面 |"
    assert to_mrkdwn(src) == "```\n欄位\n前面*粗*後面\n```"


def test_a_table_cell_gets_no_boundary_spaces_in_nested_emphasis():
    """The guard is off for the whole subtree, not just the cell's top level."""
    src = "| 欄位 |\n| --- |\n| **粗前*斜*粗後** |"
    assert to_mrkdwn(src) == "```\n欄位\n*粗前_斜_粗後*\n```"


def test_a_table_cell_gets_no_boundary_spaces_inside_a_link_label():
    src = "| 欄位 |\n| --- |\n| [前**粗**後](https://x.dev) |"
    assert to_mrkdwn(src) == "```\n欄位\n<https://x.dev|前*粗*後>\n```"


def test_table_columns_are_measured_without_boundary_spaces():
    """The guard used to widen the cell it touched, which pushed every other
    row in that column out by the same two characters."""
    src = "| a | b |\n| --- | --- |\n| 前**粗**後 | x |\n| yy | z |"
    assert to_mrkdwn(src) == "```\na     | b\n前*粗*後 | x\nyy    | z\n```"


def test_raw_slack_markup_survives_untouched():
    """`<@U123>` and `<#C123>` are Slack's own mention syntax. Escaping them would
    turn a working mention into visible noise."""
    assert to_mrkdwn("ping <@U123> in <#C1>") == "ping <@U123> in <#C1>"


def test_an_image_becomes_its_url():
    """Slack renders no inline images in mrkdwn, so the URL is the only thing that
    still gets the reader to the picture."""
    assert to_mrkdwn("![alt text](https://example.com/x.png)") == (
        "https://example.com/x.png"
    )


def test_an_image_without_a_source_falls_back_to_its_alt_text():
    assert to_mrkdwn("![just alt]()") == "just alt"


def test_a_link_without_a_target_is_just_its_label():
    assert to_mrkdwn("[label]()") == "label"


def test_a_nested_container_renders_its_children():
    """An unrecognised container still has to yield its contents rather than
    swallowing them."""
    out = to_mrkdwn("> - first\n> - second")
    assert "first" in out and "second" in out


def test_a_soft_line_break_inside_a_paragraph_is_kept():
    """Slack renders the newline, and joining the lines would run two sentences
    together."""
    assert to_mrkdwn("first line\nsecond line") == "first line\nsecond line"


def test_a_real_table_still_renders_aligned_in_a_fence():
    """The guard against empty tables must not swallow a table with content."""
    out = to_mrkdwn("| a | bb |\n|---|----|\n| 1 | 2 |")
    assert out == "```\na | bb\n1 | 2\n```"


def test_inline_html_survives_mid_sentence():
    """An agent writing raw markup inside a sentence means it, and escaping it would
    turn a working Slack mention into visible noise."""
    assert to_mrkdwn("a <b>bold</b> c") == "a <b>bold</b> c"


def test_a_block_of_raw_html_is_dropped():
    """Pins current behaviour rather than blessing it: a raw HTML block is a block
    node, so it falls through `_render_block` and contributes nothing. Slack would
    render `<div>` as literal text, so dropping it is the tidier of the two."""
    assert to_mrkdwn("<div>\nraw block\n</div>") == ""


def test_slack_link_syntax_written_by_the_agent_survives():
    """`<url|label>` is Slack's own form, and an agent that writes it directly must
    not have it re-escaped into something Slack renders literally."""
    assert to_mrkdwn("<https://example.com|click here>") == (
        "<https://example.com|click here>"
    )
