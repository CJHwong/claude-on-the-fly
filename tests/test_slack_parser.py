"""Tests for claude_on_the_fly.slack forwarded-message parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from claude_on_the_fly.agent import Response
from claude_on_the_fly.slack import (
    SlackFrontend,
    _extract_forwards,
    _flatten_primary_content,
    _flatten_rich_elements,
    _render_attachment,
    _render_forward,
    _session_key,
    _text_from_blocks,
    _text_from_primary_blocks,
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
# _text_from_primary_blocks
# ---------------------------------------------------------------------------


class TestTextFromPrimaryBlocks:
    def test_section_extracted(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "section body"}}
        ]
        assert _text_from_primary_blocks(blocks) == "section body"

    def test_header_extracted(self):
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Title"}}]
        assert _text_from_primary_blocks(blocks) == "Title"

    def test_context_extracted(self):
        blocks = [
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "footer line"},
                    {"type": "plain_text", "text": "extra"},
                ],
            }
        ]
        assert _text_from_primary_blocks(blocks) == "footer line extra"

    def test_rich_text_skipped(self):
        # rich_text duplicates event.text; explicitly excluded here.
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "hi"}],
                    }
                ],
            }
        ]
        assert _text_from_primary_blocks(blocks) == ""

    def test_mixed_blocks_joined(self):
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "Alert"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "details here"}},
        ]
        assert _text_from_primary_blocks(blocks) == "Alert\ndetails here"

    def test_empty_returns_empty(self):
        assert _text_from_primary_blocks([]) == ""


# ---------------------------------------------------------------------------
# _render_attachment
# ---------------------------------------------------------------------------


class TestRenderAttachment:
    def test_title_text_fields(self):
        att = {
            "title": "Sentry Alert",
            "text": "error in production",
            "fields": [
                {"title": "Env", "value": "prod"},
                {"title": "Count", "value": "42"},
            ],
        }
        rendered = _render_attachment(att)
        assert "Sentry Alert" in rendered
        assert "error in production" in rendered
        assert "Env: prod" in rendered
        assert "Count: 42" in rendered

    def test_field_without_title_just_value(self):
        att = {"fields": [{"value": "lonely value"}]}
        assert _render_attachment(att) == "lonely value"

    def test_pretext_first(self):
        att = {"pretext": "heads up", "title": "thing", "text": "details"}
        rendered = _render_attachment(att)
        assert rendered.index("heads up") < rendered.index("thing")
        assert rendered.index("thing") < rendered.index("details")

    def test_attachment_blocks_used_when_no_text(self):
        att = {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "from blocks"}}
            ]
        }
        assert "from blocks" in _render_attachment(att)

    def test_empty_attachment_returns_empty(self):
        assert _render_attachment({}) == ""


# ---------------------------------------------------------------------------
# _flatten_primary_content
# ---------------------------------------------------------------------------


class TestFlattenPrimaryContent:
    def test_app_post_with_blocks_only(self):
        event = {
            "text": "GitHub",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "PR #42"}},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "opened by alice"},
                },
            ],
        }
        content = _flatten_primary_content(event)
        assert "PR #42" in content
        assert "opened by alice" in content

    def test_non_forward_attachment_extracted(self):
        event = {
            "attachments": [
                {"title": "preview title", "text": "preview body"},
            ],
        }
        content = _flatten_primary_content(event)
        assert "preview title" in content
        assert "preview body" in content

    def test_forward_attachment_skipped(self):
        # Already handled by _extract_forwards; must not duplicate here.
        event = {
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "channel_id": "C1",
                    "ts": "1.2",
                    "text": "forwarded body",
                }
            ],
        }
        assert _flatten_primary_content(event) == ""

    def test_attachment_with_channel_ref_skipped(self):
        event = {
            "attachments": [{"channel_id": "C1", "ts": "1.2", "text": "ref body"}],
        }
        assert _flatten_primary_content(event) == ""

    def test_rich_text_block_does_not_duplicate(self):
        event = {
            "text": "hi there",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "hi there"}],
                        }
                    ],
                }
            ],
        }
        assert _flatten_primary_content(event) == ""

    def test_empty_event_returns_empty(self):
        assert _flatten_primary_content({}) == ""

    def test_mixed_blocks_and_attachments(self):
        event = {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "block A"}},
            ],
            "attachments": [
                {"title": "att title", "text": "att body"},
            ],
        }
        content = _flatten_primary_content(event)
        assert "block A" in content
        assert "att title" in content
        assert "att body" in content


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


def _make_frontend(
    allowed_user_ids: set[str] | None = None,
    allowed_bot_ids: set[str] | None = None,
    silent_sender_ids: set[str] | None = None,
    blocked_senders: set[str] | None = None,
) -> SlackFrontend:
    with patch("claude_on_the_fly.slack.AsyncApp") as mock_app_cls:
        mock_app = MagicMock()
        mock_app.client = MagicMock()
        mock_app_cls.return_value = mock_app
        fe = SlackFrontend(
            app_token="xapp-fake",
            token="xoxp-fake",
            user_id="UBOT",
            allowed_user_ids=allowed_user_ids or {"*"},
            allowed_bot_ids=allowed_bot_ids,
            silent_sender_ids=silent_sender_ids,
            blocked_senders=blocked_senders,
        )
    fe._on_message = AsyncMock()
    # short-circuit helpers that would hit the network
    fe._resolve_sender = AsyncMock(return_value="chopin")  # type: ignore[assignment]
    fe._resolve_session_metadata = AsyncMock()  # type: ignore[assignment]
    return fe


def _install_replies_mock(fe: SlackFrontend, **kwargs) -> AsyncMock:
    """Attach an AsyncMock to fe._app.client.conversations_replies and return it.

    Returning the mock directly avoids the static type checker losing track
    of the attribute as it's overwritten on a slack_sdk client.
    """
    mock = AsyncMock(**kwargs)
    fe._app.client.conversations_replies = mock  # type: ignore[invalid-assignment]
    return mock


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

    async def test_app_post_blocks_reach_prompt(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "GitHub",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700010.000000",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "PR #99"}},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "merged by bob"},
                },
            ],
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "PR #99" in payload
        assert "merged by bob" in payload

    async def test_non_forward_attachment_reaches_prompt(self):
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "check this out",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700011.000000",
            "attachments": [
                {"title": "Article title", "text": "Article preview body"},
            ],
        }
        await fe._ingest_event(event)

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "check this out" in payload
        assert "Article title" in payload
        assert "Article preview body" in payload

    async def test_app_only_blocks_no_text_still_processed(self):
        # event.text is empty but blocks carry content; should not be skipped.
        fe = _make_frontend()
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700012.000000",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "lone block"}},
            ],
        }
        await fe._ingest_event(event)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "lone block" in payload

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


# ---------------------------------------------------------------------------
# Thread-context fetch (mid-thread mention)
# ---------------------------------------------------------------------------


def _mid_thread_event(
    text: str = "<@UBOT> help", ts: str = "1776800010.000000"
) -> dict:
    return {
        "type": "message",
        "user": "U03DXM5L8KX",
        "text": text,
        "channel": "C9999",
        "channel_type": "channel",
        "ts": ts,
        "thread_ts": "1776800001.000000",
    }


class TestThreadContextFetch:
    async def test_mid_thread_mention_fetches_and_includes_context(self):
        fe = _make_frontend()
        replies = _install_replies_mock(
            fe,
            return_value={
                "messages": [
                    {
                        "ts": "1776800001.000000",
                        "user": "U_ALICE",
                        "text": "starting point",
                    },
                    {
                        "ts": "1776800002.000000",
                        "user": "U_BOB",
                        "text": "follow up",
                    },
                    {
                        "ts": "1776800010.000000",
                        "user": "U03DXM5L8KX",
                        "text": "<@UBOT> help",
                    },
                ]
            },
        )
        await fe._ingest_event(_mid_thread_event())

        replies.assert_awaited_once()
        kwargs = replies.call_args.kwargs
        assert kwargs["channel"] == "C9999"
        assert kwargs["ts"] == "1776800001.000000"

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "<thread_context>" in payload
        assert "starting point" in payload
        assert "follow up" in payload
        # current message must NOT appear inside the context block.
        ctx_block = payload.split("</thread_context>")[0]
        assert "1776800010.000000" not in ctx_block
        # context block precedes the [from:] cover.
        assert payload.index("<thread_context>") < payload.index("[from:")

    async def test_top_level_message_does_not_fetch(self):
        fe = _make_frontend()
        replies = _install_replies_mock(fe)
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "<@UBOT> hi",
            "channel": "C9999",
            "channel_type": "channel",
            "ts": "1776800020.000000",
            # no thread_ts → top-level message
        }
        await fe._ingest_event(event)
        replies.assert_not_called()

    async def test_thread_root_message_does_not_fetch(self):
        # thread_ts == ts means this IS the thread root, not a reply into one.
        fe = _make_frontend()
        replies = _install_replies_mock(fe)
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "<@UBOT> kick off",
            "channel": "C9999",
            "channel_type": "channel",
            "ts": "1776800030.000000",
            "thread_ts": "1776800030.000000",
        }
        await fe._ingest_event(event)
        replies.assert_not_called()

    async def test_second_message_in_session_skips_refetch(self):
        fe = _make_frontend()
        replies = _install_replies_mock(
            fe,
            return_value={
                "messages": [
                    {
                        "ts": "1776800001.000000",
                        "user": "U_ALICE",
                        "text": "earlier",
                    },
                ]
            },
        )
        await fe._ingest_event(_mid_thread_event(ts="1776800010.000000"))
        await fe._ingest_event(_mid_thread_event(ts="1776800011.000000"))
        # Only the first ingest in this session should trigger the fetch.
        assert replies.await_count == 1

    async def test_api_failure_does_not_block_message(self):
        fe = _make_frontend()
        _install_replies_mock(fe, side_effect=RuntimeError("slack api down"))
        await fe._ingest_event(_mid_thread_event())
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "<thread_context>" not in payload
        assert "[from: chopin]" in payload

    async def test_empty_thread_response_no_context_block(self):
        fe = _make_frontend()
        _install_replies_mock(
            fe,
            return_value={
                "messages": [
                    # only the current message comes back; nothing prior.
                    {
                        "ts": "1776800010.000000",
                        "user": "U03DXM5L8KX",
                        "text": "<@UBOT> help",
                    },
                ]
            },
        )
        await fe._ingest_event(_mid_thread_event())
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "<thread_context>" not in payload

    async def test_app_bot_message_uses_username(self):
        fe = _make_frontend()
        _install_replies_mock(
            fe,
            return_value={
                "messages": [
                    {
                        "ts": "1776800001.000000",
                        "bot_id": "B1",
                        "username": "github-bot",
                        "text": "PR opened",
                    },
                ]
            },
        )
        await fe._ingest_event(_mid_thread_event())
        _, payload = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert 'author="github-bot"' in payload
        assert "PR opened" in payload


# ---------------------------------------------------------------------------
# _ingest_event trusted-bot handling (HubSpot / Jira app posts)
# ---------------------------------------------------------------------------


def _bot_event(bot_id: str = "B07JPABE2", **overrides) -> dict:
    """A real-shaped bot_message: no user field, empty text, content in attachments."""
    event = {
        "type": "message",
        "subtype": "bot_message",
        "text": "",
        "bot_id": bot_id,
        "channel": "C07EBTHK6",
        "channel_type": "channel",
        "ts": "1781239423.701139",
        "attachments": [
            {
                "id": 1,
                "fallback": "HubSpot deal moved to Closed Won",
                "pretext": "HubSpot deal moved to Closed Won",
                "title": "Acme Corp",
            }
        ],
    }
    event.update(overrides)
    return event


class TestIngestEventTrustedBot:
    async def test_trusted_bot_dispatches_with_attachment_content(self):
        fe = _make_frontend(allowed_bot_ids={"B07JPABE2"})
        await fe._ingest_event(_bot_event())

        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]
        _, text = fe._on_message.call_args[0]  # type: ignore[union-attr]
        assert "HubSpot deal moved to Closed Won" in text

    async def test_untrusted_bot_is_skipped(self):
        fe = _make_frontend(allowed_bot_ids={"B07JPABE2"})
        await fe._ingest_event(_bot_event(bot_id="B_OTHER"))
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_bot_message_skipped_when_no_bots_allowlisted(self):
        fe = _make_frontend()  # default: no allowed_bot_ids
        await fe._ingest_event(_bot_event())
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_blocked_bot_is_skipped_even_if_allowlisted(self):
        fe = _make_frontend(
            allowed_bot_ids={"B07JPABE2"}, blocked_senders={"B07JPABE2"}
        )
        await fe._ingest_event(_bot_event())
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_trusted_bot_in_channel_needs_no_mention(self):
        # Channel post with no @mention still goes through for a trusted bot.
        fe = _make_frontend(allowed_bot_ids={"B07JPABE2"})
        await fe._ingest_event(_bot_event())
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_trusted_bot_bypasses_restricted_user_allowlist(self):
        # sender_id is "" for a bot; a non-"*" user allowlist must not drop it.
        fe = _make_frontend(
            allowed_user_ids={"USOMEONE"}, allowed_bot_ids={"B07JPABE2"}
        )
        await fe._ingest_event(_bot_event())
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_trusted_bot_sender_comes_from_username(self):
        fe = _make_frontend(allowed_bot_ids={"B07JPABE2"})
        await fe._ingest_event(_bot_event(username="HubSpot"))
        assert fe._sender_names[next(iter(fe._sender_names))] == "HubSpot"
        # never falls back to the human user lookup
        fe._resolve_sender.assert_not_awaited()  # type: ignore[union-attr]

    async def test_silenced_bot_reply_is_omitted(self):
        fe = _make_frontend(
            allowed_bot_ids={"B07JPABE2"}, silent_sender_ids={"B07JPABE2"}
        )
        fe._app.client.chat_postMessage = AsyncMock()  # type: ignore[invalid-assignment]
        event = _bot_event()

        await fe._ingest_event(event)
        session_id = _session_key(event["channel"], event["ts"])
        await fe.notify_start(session_id)
        delivered = await fe.send(session_id, Response(body="done"))

        assert delivered == []
        fe._app.client.chat_postMessage.assert_not_awaited()

    async def test_non_silenced_bot_reply_is_posted(self):
        fe = _make_frontend(allowed_bot_ids={"B07JPABE2"})
        fe._app.client.chat_postMessage = AsyncMock(  # type: ignore[invalid-assignment]
            return_value={"ok": True, "ts": "99.0"}
        )
        event = _bot_event()

        await fe._ingest_event(event)
        session_id = _session_key(event["channel"], event["ts"])
        await fe.notify_start(session_id)
        await fe.send(session_id, Response(body="done"))

        fe._app.client.chat_postMessage.assert_awaited_once()

    async def test_silenced_user_reply_is_omitted(self):
        fe = _make_frontend(silent_sender_ids={"U03DXM5L8KX"})
        fe._app.client.chat_postMessage = AsyncMock()  # type: ignore[invalid-assignment]
        event = {
            "type": "message",
            "user": "U03DXM5L8KX",
            "text": "hello",
            "channel": "D0AMMU8BJSY",
            "channel_type": "im",
            "ts": "1776700009.000000",
        }

        await fe._ingest_event(event)
        session_id = _session_key(event["channel"], event["ts"])
        await fe.notify_start(session_id)
        delivered = await fe.send(session_id, Response(body="done"))

        assert delivered == []
        fe._app.client.chat_postMessage.assert_not_awaited()
