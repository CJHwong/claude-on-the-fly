"""Tests for claude_on_the_fly.slack forwarded-message parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


from claude_on_the_fly.slack import (
    SlackFrontend,
    _extract_forwards,
    _flatten_rich_elements,
    _render_forward,
    _text_from_blocks,
)


# ---------------------------------------------------------------------------
# _flatten_rich_elements
# ---------------------------------------------------------------------------


class TestFlattenRichElements:
    def test_plain_text(self):
        elements = [{"type": "text", "text": "hello"}]
        assert _flatten_rich_elements(elements) == "hello"

    def test_user_mention(self):
        elements = [{"type": "user", "user_id": "U123"}]
        assert _flatten_rich_elements(elements) == "<@U123>"

    def test_channel_mention(self):
        elements = [{"type": "channel", "channel_id": "C999"}]
        assert _flatten_rich_elements(elements) == "<#C999>"

    def test_link(self):
        elements = [{"type": "link", "url": "https://example.com"}]
        assert _flatten_rich_elements(elements) == "https://example.com"

    def test_mixed_sequence(self):
        elements = [
            {"type": "user", "user_id": "U011U1KUVLK"},
            {"type": "text", "text": "  for the QBO"},
        ]
        assert _flatten_rich_elements(elements) == "<@U011U1KUVLK>  for the QBO"

    def test_empty_list(self):
        assert _flatten_rich_elements([]) == ""

    def test_unknown_type_ignored(self):
        elements = [
            {"type": "text", "text": "a"},
            {"type": "emoji", "name": "wave"},
            {"type": "text", "text": "b"},
        ]
        assert _flatten_rich_elements(elements) == "ab"


# ---------------------------------------------------------------------------
# _text_from_blocks
# ---------------------------------------------------------------------------


class TestTextFromBlocks:
    def test_section_with_text(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        assert _text_from_blocks(blocks) == "hi"

    def test_rich_text_with_nested_elements(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "hello world"}],
                    }
                ],
            }
        ]
        assert _text_from_blocks(blocks) == "hello world"

    def test_empty_list(self):
        assert _text_from_blocks([]) == ""


# ---------------------------------------------------------------------------
# _extract_forwards
# ---------------------------------------------------------------------------


CHOPIN_EVENT = {
    "type": "message",
    "user": "U03DXM5L8KX",
    "text": "Help…",
    "channel": "D0AMMU8BJSY",
    "channel_type": "im",
    "ts": "1776700000.000000",
    "attachments": [
        {
            "is_msg_unfurl": True,
            "channel_id": "C08M9HWQWV8",
            "channel_name": "soln-quickbooks",
            "author_name": "Linda C",
            "author_id": "U0366MHRX39",
            "text": (
                "<@U011U1KUVLK>  for the QBO, could you advise if we would be "
                "proposing the new pricing for AGFUS( Atlantic) or do we need "
                "another separate discussion?"
            ),
            "ts": "1776404384.245429",
        }
    ],
}


class TestExtractForwards:
    def test_shape_a_attachment_unfurl(self):
        forwards = _extract_forwards(CHOPIN_EVENT)
        assert len(forwards) == 1
        fwd = forwards[0]
        assert fwd["channel_id"] == "C08M9HWQWV8"
        assert fwd["channel_name"] == "soln-quickbooks"
        assert fwd["ts"] == "1776404384.245429"
        assert fwd["author_name"] == "Linda C"
        assert fwd["author_id"] == "U0366MHRX39"
        assert "AGFUS" in fwd["text"]

    def test_shape_b_rich_text_quote(self):
        event = {
            "text": "what do you think?",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_quote",
                            "elements": [
                                {"type": "text", "text": "the quoted body"},
                            ],
                        }
                    ],
                }
            ],
        }
        forwards = _extract_forwards(event)
        assert len(forwards) == 1
        assert forwards[0]["text"] == "the quoted body"
        # shape B has no channel_id/ts, that's expected
        assert forwards[0]["channel_id"] == ""
        assert forwards[0]["ts"] == ""

    def test_attachment_without_msg_markers_ignored(self):
        event = {
            "text": "see this",
            "attachments": [{"title": "some unrelated unfurl", "text": "web preview"}],
        }
        assert _extract_forwards(event) == []

    def test_attachment_with_channel_id_and_ts_counts_even_without_is_msg_unfurl(self):
        event = {
            "text": "",
            "attachments": [
                {
                    "channel_id": "C1",
                    "ts": "123.456",
                    "text": "body",
                }
            ],
        }
        forwards = _extract_forwards(event)
        assert len(forwards) == 1
        assert forwards[0]["channel_id"] == "C1"

    def test_empty_event(self):
        assert _extract_forwards({}) == []

    def test_empty_body_attachment_skipped(self):
        event = {
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C1",
                    "ts": "1.2",
                    "text": "",
                }
            ],
        }
        assert _extract_forwards(event) == []

    def test_multiple_forwards(self):
        event = {
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C1",
                    "ts": "1.1",
                    "text": "first",
                },
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C2",
                    "ts": "2.2",
                    "text": "second",
                },
            ],
        }
        forwards = _extract_forwards(event)
        assert len(forwards) == 2
        assert forwards[0]["text"] == "first"
        assert forwards[1]["text"] == "second"


# ---------------------------------------------------------------------------
# _render_forward
# ---------------------------------------------------------------------------


class TestRenderForward:
    def test_chopin_rendering_contains_required_fields(self):
        fwd = _extract_forwards(CHOPIN_EVENT)[0]
        rendered = _render_forward(fwd)
        # The three things the Apr 17 failure proved we needed
        assert "C08M9HWQWV8" in rendered
        assert "1776404384.245429" in rendered
        assert "AGFUS" in rendered
        # Structural markers
        assert rendered.startswith("<forwarded_message>")
        assert rendered.endswith("</forwarded_message>")
        assert "<channel_id>C08M9HWQWV8</channel_id>" in rendered
        assert "<thread_ts>1776404384.245429</thread_ts>" in rendered

    def test_source_line_formatted(self):
        fwd = {
            "channel_name": "soln-quickbooks",
            "author_name": "Linda C",
            "ts": "1776404384.245429",
            "channel_id": "C08M9HWQWV8",
            "text": "body",
        }
        rendered = _render_forward(fwd)
        assert (
            "<source>#soln-quickbooks · @Linda C · 1776404384.245429</source>"
            in rendered
        )

    def test_missing_channel_id_omits_tag(self):
        fwd = {"text": "body"}
        rendered = _render_forward(fwd)
        assert "<channel_id>" not in rendered
        assert "<thread_ts>" not in rendered
        assert "<source>" not in rendered
        assert "body" in rendered

    def test_empty_body_still_renders_tags(self):
        fwd = {"channel_id": "C1", "ts": "1.0", "text": ""}
        rendered = _render_forward(fwd)
        assert "<body>" in rendered
        assert "</body>" in rendered


# ---------------------------------------------------------------------------
# _ingest_event integration
# ---------------------------------------------------------------------------


def _make_frontend() -> SlackFrontend:
    with patch("claude_on_the_fly.slack.AsyncApp") as mock_app_cls:
        mock_app = MagicMock()
        mock_app.client = MagicMock()
        mock_app_cls.return_value = mock_app
        fe = SlackFrontend(
            app_token="xapp-fake",
            user_token="xoxp-fake",
            user_id="UBOT",
            allowed_user_ids={"*"},
        )
    fe._on_message = AsyncMock()  # type: ignore[assignment]
    # short-circuit helpers that would hit the network
    fe._resolve_sender = AsyncMock(return_value="chopin")  # type: ignore[assignment]
    fe._resolve_session_metadata = AsyncMock()  # type: ignore[assignment]
    return fe


class TestIngestEventForwards:
    async def test_chopin_repro_includes_forward_in_prompt(self):
        fe = _make_frontend()
        await fe._ingest_event(dict(CHOPIN_EVENT))

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]

        assert "C08M9HWQWV8" in text
        assert "1776404384.245429" in text
        assert "AGFUS" in text
        assert "[from: chopin] Help…" in text
        # forwarded block comes before the cover note
        assert text.index("<forwarded_message>") < text.index("Help…")

    async def test_bare_forward_without_cover_text(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700001.000000",
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C1",
                    "ts": "100.200",
                    "author_name": "Alice",
                    "text": "question body",
                }
            ],
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "question body" in text
        assert "[from: chopin]" in text

    async def test_forward_plus_files(self):
        fe = _make_frontend()
        fe._save_files = AsyncMock(return_value=["[File saved: doc.pdf]"])  # type: ignore[assignment]

        event = {
            "type": "message",
            "subtype": "file_share",
            "user": "U03DXM5L8KX",
            "text": "please review",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700002.000000",
            "files": [{"id": "F1", "name": "doc.pdf"}],
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C2",
                    "ts": "200.300",
                    "text": "linked thread body",
                }
            ],
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "linked thread body" in text
        assert "[File saved: doc.pdf]" in text
        assert "please review" in text

    async def test_no_forward_no_text_no_files_skipped(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700003.000000",
        }
        await fe._ingest_event(event)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_existing_plain_text_still_works(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "just a normal DM",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700004.000000",
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert text == "[from: chopin] just a normal DM"

    async def test_rich_text_quote_from_blocks(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "thoughts?",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700005.000000",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_quote",
                            "elements": [
                                {"type": "text", "text": "quoted content here"},
                            ],
                        }
                    ],
                }
            ],
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "quoted content here" in text
        assert "thoughts?" in text
