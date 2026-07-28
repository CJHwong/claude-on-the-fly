"""Default `Notifier` — posts a job's Result into its originating Slack thread.

Runs in the worker process, separate from the Slack frontend, so it builds its
OWN `AsyncWebClient` (token resolved in `cli.py`; identity is deployer-config,
never hardcoded). It reads only the routing keys the producer put in `origin`
(`channel`, `thread_ts`) — never `sender_id` — and converts the agent's Markdown
to Slack mrkdwn with the shared `to_mrkdwn`.

Block splitting comes from `slack_mrkdwn.split_blocks`, shared with the chat
frontend so the same reply cannot render differently depending on which one
produced it. Delivery is best-effort: a post failure is logged, not raised —
the result is already durable in the queue's `done/`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from claude_on_the_fly.jobs.core import Result
from claude_on_the_fly.slack_mrkdwn import split_blocks, to_mrkdwn

logger = logging.getLogger(__name__)

# Slack's chat.postMessage accepts at most 50 blocks per message; past that it
# rejects the whole call with `invalid_blocks`. A result that produces more is
# posted across several messages rather than dropped (see `notify`).
SLACK_MAX_BLOCKS = 50
# `text` is the notification/fallback string, not the message body — Slack caps
# it at 40,000 characters and rejects the whole call past that. The blocks carry
# the content; this only has to say what arrived.
FALLBACK_TEXT_LIMIT = 200


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


def _blocks(mrkdwn_text: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in split_blocks(mrkdwn_text)
    ]


def _fallback_text(body: str) -> str:
    """The notification line Slack shows where blocks cannot render."""
    head = body.strip().split("\n", 1)[0]
    if len(head) <= FALLBACK_TEXT_LIMIT:
        return head or "Job result"
    return head[: FALLBACK_TEXT_LIMIT - 1] + "…"


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
            fallback = _fallback_text(body)
            for start in range(0, len(blocks), SLACK_MAX_BLOCKS):
                await self._client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=fallback if start == 0 else "(continued)",
                    blocks=blocks[start : start + SLACK_MAX_BLOCKS],
                )
        except Exception as exc:  # best-effort; result is durable in done/
            logger.exception("jobs: notify failed to post to %s: %s", channel, exc)
