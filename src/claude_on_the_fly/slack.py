"""Slack frontend. Replies as you via user token + Socket Mode."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from typing import Awaitable, Callable

from claude_on_the_fly.agent import Response
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)


def _session_key(channel: str, thread_ts: str | None) -> int:
    return hash(f"{channel}:{thread_ts or 'root'}")


class SlackFrontend(Frontend):
    def __init__(
        self,
        app_token: str,
        user_token: str,
        user_id: str,
        allowed_user_ids: set[str] | None = None,
    ) -> None:
        self._app_token = app_token
        self._user_id = user_id
        self._allowed_user_ids = allowed_user_ids or set()
        self._allowed_user_ids.add(user_id)
        self._app = AsyncApp(token=user_token, ignoring_self_events_enabled=False)
        self._handler: AsyncSocketModeHandler | None = None
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orchestrator: object | None = None
        self._sessions: dict[int, tuple[str, str | None]] = {}
        self._our_sent_timestamps: deque[str] = deque(maxlen=500)
        self._workspace_names: dict[int, str] = {}
        self._sender_names: dict[int, str] = {}
        self._channel_contexts: dict[int, str] = {}
        self._user_name_cache: dict[str, str] = {}  # slack user_id -> display name

    def set_orchestrator(self, orchestrator: object) -> None:
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        return f"slack/{self._workspace_names.get(chat_id, str(chat_id))}"

    def sender_name(self, chat_id: int) -> str:
        return self._sender_names.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        return self._channel_contexts.get(chat_id, "dm")

    # --- Lifecycle ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message

        @self._app.event({"type": "message"})
        async def handle_message(event, say):
            if event.get("subtype"):
                return
            sender_id = event.get("user", "")
            if sender_id == self._user_id:
                return
            if sender_id not in self._allowed_user_ids:
                return

            text = event.get("text", "")
            channel = event.get("channel")
            thread_ts = event.get("thread_ts") or event.get("ts")
            channel_type = event.get("channel_type", "")

            # In channels, only respond to @mentions
            if channel_type in ("channel", "group"):
                mention = f"<@{self._user_id}>"
                if mention not in text:
                    return
                text = re.sub(f"<@{self._user_id}>\\s*", "", text).strip()

            if not text:
                return

            session_id = _session_key(channel, thread_ts)
            self._sessions[session_id] = (channel, thread_ts)

            sender = await self._resolve_sender(event.get("user", "unknown"))
            self._sender_names[session_id] = sender
            await self._resolve_session_metadata(
                session_id, sender, channel, channel_type, thread_ts
            )
            text = f"[from: {sender}] {text}"

            logger.info("slack %s/%s: %s", channel, thread_ts, text[:80])
            if self._on_message:
                await self._on_message(session_id, text)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        logger.info("Slack bot connected via Socket Mode.")

    async def stop(self) -> None:
        if self._handler:
            await self._handler.close_async()

    # --- Sending ---

    async def send(self, chat_id: int, response: Response) -> None:
        route = self._sessions.get(chat_id)
        if not route:
            logger.error("No channel found for session %s", chat_id)
            return
        channel, thread_ts = route

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": response.body}}
        ]
        if response.has_stats:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": response.format_stats()}],
                }
            )

        resp = await self._app.client.chat_postMessage(
            channel=channel,
            text=response.body,
            blocks=blocks,
            thread_ts=thread_ts,
        )
        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])

    async def send_typing(self, chat_id: int) -> None:
        pass

    # --- Helpers ---

    async def _resolve_sender(self, user_id: str) -> str:
        """Look up Slack user ID to display name. Cached on success only."""
        if user_id not in self._user_name_cache:
            try:
                info = await self._app.client.users_info(user=user_id)
                self._user_name_cache[user_id] = info["user"]["name"]
            except Exception as exc:
                logger.warning("Failed to resolve Slack user %s: %s", user_id, exc)
                return user_id
        return self._user_name_cache[user_id]

    async def _resolve_session_metadata(
        self,
        session_id: int,
        sender: str,
        channel: str,
        channel_type: str,
        thread_ts: str,
    ) -> None:
        if session_id in self._workspace_names:
            return

        short_ts = thread_ts.split(".")[0] if thread_ts else "root"

        if channel_type == "im":
            self._workspace_names[session_id] = f"dm-{sender}-{short_ts}"
            self._channel_contexts[session_id] = "dm"
        else:
            try:
                info = await self._app.client.conversations_info(channel=channel)
                name = info["channel"]["name"]
            except Exception as exc:
                logger.warning("Failed to resolve channel %s: %s", channel, exc)
                name = channel
            self._workspace_names[session_id] = f"{name}-{short_ts}"
            self._channel_contexts[session_id] = f"channel:#{name}"


def main() -> None:
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_slack

    load_dotenv()
    app_token, user_token, user_id, allowed_user_ids = run_slack()
    frontend = SlackFrontend(
        app_token=app_token,
        user_token=user_token,
        user_id=user_id,
        allowed_user_ids=allowed_user_ids,
    )
    asyncio.run(run(frontend, platform="slack"))


if __name__ == "__main__":
    main()
