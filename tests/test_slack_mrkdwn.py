"""Tests for the Markdown -> Slack mrkdwn converter."""

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


def test_paragraphs_separated_by_blank_line():
    assert to_mrkdwn("first\n\nsecond") == "first\n\nsecond"


def test_a_horizontal_rule_disappears():
    """mrkdwn has no horizontal rule, and the literal `---` reads as a typo."""
    assert to_mrkdwn("above\n\n---\n\nbelow") == "above\n\nbelow"


def test_an_empty_table_renders_as_nothing():
    """A header-only table has no rows to align, and an empty code fence is worse
    than no table."""
    assert to_mrkdwn("|  |\n|--|") == ""


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
