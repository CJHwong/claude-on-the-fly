"""SlackThreadNotifier: posts into origin's thread, reads only channel/thread_ts,
splits long bodies, marks failures, and swallows post errors."""

from __future__ import annotations

from unittest.mock import AsyncMock

from claude_on_the_fly.jobs.core import Result
from claude_on_the_fly.jobs.slack_notifier import (
    FALLBACK_TEXT_LIMIT,
    SLACK_MAX_BLOCKS,
    SlackThreadNotifier,
    _blocks,
)
from claude_on_the_fly.slack_mrkdwn import SLACK_BLOCK_LIMIT, split_blocks, to_mrkdwn


def _client() -> AsyncMock:
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.0"})
    return client


async def test_posts_result_into_origin_thread() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    origin = {"channel": "C123", "thread_ts": "1699.5", "sender_id": "U9"}
    await notifier.notify(origin, Result(ok=True, text="the answer"))

    client.chat_postMessage.assert_awaited_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C123"
    assert kwargs["thread_ts"] == "1699.5"
    assert kwargs["text"] == "the answer"
    assert kwargs["blocks"][0]["type"] == "section"


async def test_reads_only_channel_and_thread_ts_never_sender_id() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    await notifier.notify(
        {"channel": "C1", "thread_ts": "9.9", "sender_id": "U-secret"},
        Result(ok=True, text="ok"),
    )
    kwargs = client.chat_postMessage.call_args.kwargs
    # sender_id is never forwarded to Slack.
    assert "U-secret" not in str(kwargs)


async def test_missing_thread_ts_posts_to_channel_root() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    await notifier.notify({"channel": "C1"}, Result(ok=True, text="ok"))
    assert client.chat_postMessage.call_args.kwargs["thread_ts"] is None


async def test_failure_result_is_marked() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    await notifier.notify({"channel": "C1"}, Result(ok=False, text="stack trace"))
    kwargs = client.chat_postMessage.call_args.kwargs
    # The notification line marks the failure; the detail rides in the blocks,
    # which are what Slack actually renders.
    assert "Job failed" in kwargs["text"]
    assert "stack trace" in kwargs["blocks"][0]["text"]["text"]


async def test_no_channel_skips_post() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    await notifier.notify({"thread_ts": "1.0"}, Result(ok=True, text="ok"))
    client.chat_postMessage.assert_not_awaited()


async def test_long_body_splits_into_multiple_blocks() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    body = "\n".join(["x" * 500 for _ in range(20)])  # ~10k chars
    await notifier.notify({"channel": "C1"}, Result(ok=True, text=body))
    blocks = client.chat_postMessage.call_args.kwargs["blocks"]
    assert len(blocks) > 1
    for block in blocks:
        assert len(block["text"]["text"]) <= SLACK_BLOCK_LIMIT


async def test_post_failure_is_swallowed() -> None:
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(side_effect=RuntimeError("network down"))
    notifier = SlackThreadNotifier(client)
    # Must not raise — delivery is best-effort; result stays durable in done/.
    await notifier.notify({"channel": "C1"}, Result(ok=True, text="ok"))


def test_single_over_limit_line_reassembles_with_zero_loss() -> None:
    # One unbroken line (no newlines) well over the limit — the JSON-blob case.
    # The old splitter truncated it to line[:SLACK_BLOCK_LIMIT] and dropped the
    # tail; every character must now survive across the produced chunks.
    line = "J" * (SLACK_BLOCK_LIMIT * 2 + 137)
    chunks = split_blocks(line)
    assert len(chunks) == 3  # 3000 + 3000 + 137
    assert all(len(c) <= SLACK_BLOCK_LIMIT for c in chunks)
    assert "".join(chunks) == line  # nothing dropped


async def test_over_limit_line_delivered_without_content_loss() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    body = "K" * (SLACK_BLOCK_LIMIT * 3 + 50)
    await notifier.notify({"channel": "C1"}, Result(ok=True, text=body))
    blocks = client.chat_postMessage.call_args.kwargs["blocks"]
    assert len(blocks) > 1
    for block in blocks:
        assert len(block["text"]["text"]) <= SLACK_BLOCK_LIMIT
    reassembled = "".join(block["text"]["text"] for block in blocks)
    assert reassembled == to_mrkdwn(body)


async def test_result_over_block_budget_splits_into_multiple_messages() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)
    # Many separate over-limit lines → far more than SLACK_MAX_BLOCKS blocks, which
    # a single chat_postMessage would reject with `invalid_blocks`.
    body = "\n".join(
        "z" * (SLACK_BLOCK_LIMIT + 10) for _ in range(SLACK_MAX_BLOCKS + 5)
    )
    await notifier.notify({"channel": "C1"}, Result(ok=True, text=body))

    calls = client.chat_postMessage.call_args_list
    assert len(calls) >= 2  # split across multiple posted messages
    for call in calls:
        assert len(call.kwargs["blocks"]) <= SLACK_MAX_BLOCKS
    # Posted to the same origin, and no content lost across the messages.
    assert all(call.kwargs["channel"] == "C1" for call in calls)
    posted = "".join(
        block["text"]["text"] for call in calls for block in call.kwargs["blocks"]
    )
    assert posted == to_mrkdwn(body)


def test_blocks_helper_never_empty() -> None:
    assert _blocks("") == [{"type": "section", "text": {"type": "mrkdwn", "text": ""}}]


async def test_fallback_text_stays_within_slacks_cap() -> None:
    """`text` is the notification string, not the body: Slack caps it at 40,000
    characters and rejects the whole call past that. Passing the full result
    there defeated the block batching exactly in the case it exists for — a
    reply big enough to need it — and the surrounding except swallowed the
    rejection, so nothing at all was delivered."""
    client = _client()
    notifier = SlackThreadNotifier(client)

    await notifier.notify({"channel": "C1"}, Result(ok=True, text="L" * 60_000))

    for call in client.chat_postMessage.call_args_list:
        assert len(call.kwargs["text"]) <= FALLBACK_TEXT_LIMIT


async def test_fallback_text_summarises_the_first_line() -> None:
    client = _client()
    notifier = SlackThreadNotifier(client)

    await notifier.notify(
        {"channel": "C1"}, Result(ok=True, text="Deploy finished\ndetail\nmore detail")
    )

    assert client.chat_postMessage.call_args.kwargs["text"] == "Deploy finished"
