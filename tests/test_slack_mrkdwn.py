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
