"""Frontend ABC - what each messaging platform must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from claude_on_the_fly.agent import Response


class Frontend(ABC):
    """Interface for messaging platforms (Telegram, Slack, etc.)."""

    def set_orchestrator(self, orchestrator: object) -> None:
        """Optional hook so orchestrator can inject itself back."""

    @abstractmethod
    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        """Start listening. Call on_message(chat_id, text) for each incoming message."""

    @abstractmethod
    async def send(self, chat_id: int, response: Response) -> None:
        """Send a response to the user."""

    @abstractmethod
    async def send_typing(self, chat_id: int) -> None:
        """Show typing indicator. No-op if unsupported."""

    async def notify_queued(self, chat_id: int, position: int) -> None:
        """Tell the user their message is queued behind others.

        Default sends a text response. Frontends with cheaper signals (e.g.
        emoji reactions) should override.
        """
        await self.send(chat_id, Response(body=f"Queued ({position} pending)."))

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""

    @abstractmethod
    def workspace_name(self, chat_id: int) -> str:
        """Human-readable workspace path segment, e.g. 'telegram/hoss'."""

    @abstractmethod
    def sender_name(self, chat_id: int) -> str:
        """The human-readable name of the person talking in this chat."""

    @abstractmethod
    def channel_context(self, chat_id: int) -> str:
        """Where this conversation is happening, e.g. 'dm', 'channel:#general'."""
