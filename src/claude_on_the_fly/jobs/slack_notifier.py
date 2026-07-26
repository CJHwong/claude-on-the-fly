"""Default `Notifier` — posts a job's Result into its originating Slack thread.

Runs in the worker process, separate from the Slack frontend, so it builds its
OWN `AsyncWebClient` (token resolved in `cli.py`; identity is deployer-config,
never hardcoded). It reads only the routing keys the producer put in `origin`
(`channel`, `thread_ts`) — never `sender_id` — and converts the agent's Markdown
to Slack mrkdwn with the shared `to_mrkdwn`.

It owns a small block splitter rather than reaching into `slack.py`'s private
`_split_blocks`; a shared-helper extraction is a deliberate follow-up.
Delivery is best-effort: a post failure is logged, not raised — the result is
already durable in the queue's `done/`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from claude_on_the_fly.jobs.core import Result
from claude_on_the_fly.slack_mrkdwn import to_mrkdwn

logger = logging.getLogger(__name__)

SLACK_BLOCK_LIMIT = 3000
# Slack's chat.postMessage accepts at most 50 blocks per message; past that it
# rejects the whole call with `invalid_blocks`. A result that produces more is
# posted across several messages rather than dropped (see `notify`).
SLACK_MAX_BLOCKS = 50


class _PostsMessages(Protocol):
    """The one Slack method the notifier needs — keeps it testable with a fake.

    `channel` is spelled out as a required keyword-only param so this Protocol is
    a structural supertype of the real `AsyncWebClient.chat_postMessage` (which
    requires `channel`): a bare `**kwargs` signature promised a zero-argument
    call the concrete client rejects, so ty found `AsyncWebClient` unassignable
    here. `text` / `thread_ts` / `blocks` ride in `**kwargs`; the notifier passes
    them by keyword and a test fake with the same `(*, channel, **kwargs)` shape
    still satisfies it.
    """

    async def chat_postMessage(self, *, channel: str, **kwargs: Any) -> Any: ...


def _split(text: str) -> list[str]:
    """Split text into chunks within Slack's per-block limit, preferring line breaks.

    Every character of `text` (newlines included) lands in exactly one chunk, in
    order, so ``"".join(_split(text)) == text``: a single line longer than the
    limit is hard-sliced into limit-sized pieces rather than truncated, so its
    tail is never dropped.
    """
    chunks: list[str] = []
    chunk = ""
    for i, line in enumerate(text.split("\n")):
        segment = f"\n{line}" if i else line  # restore the split newline
        if len(chunk) + len(segment) <= SLACK_BLOCK_LIMIT:
            chunk += segment
            continue
        # Overflow: flush the running chunk, then lay `segment` down, hard-slicing
        # it into limit-sized pieces if the line alone exceeds the limit.
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


def _blocks(mrkdwn_text: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in _split(mrkdwn_text)
    ]


class SlackThreadNotifier:
    """`Notifier` that posts into `origin`'s channel/thread via a Slack client."""

    def __init__(self, client: _PostsMessages) -> None:
        self._client = client

    async def notify(self, origin: dict[str, Any], result: Result) -> None:
        channel = origin.get("channel")
        if not channel:
            logger.warning("jobs: notify skipped — origin has no channel: %r", origin)
            return
        thread_ts = origin.get("thread_ts")
        body = result.text if result.ok else f":warning: Job failed\n{result.text}"
        blocks = _blocks(to_mrkdwn(body))
        try:
            # Post in batches of at most SLACK_MAX_BLOCKS so a large result is
            # delivered across several messages instead of tripping Slack's
            # per-message block cap (which `chat_postMessage` would reject with
            # `invalid_blocks` — swallowed below and lost). No content is dropped.
            for start in range(0, len(blocks), SLACK_MAX_BLOCKS):
                await self._client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=body if start == 0 else "(continued)",
                    blocks=blocks[start : start + SLACK_MAX_BLOCKS],
                )
        except Exception as exc:  # best-effort; result is durable in done/
            logger.exception("jobs: notify failed to post to %s: %s", channel, exc)
