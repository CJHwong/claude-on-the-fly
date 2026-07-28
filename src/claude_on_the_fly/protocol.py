"""Frontend ABC - what each messaging platform must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_on_the_fly.agent import Response


class Frontend(ABC):
    """Interface for messaging platforms (Telegram, Slack, etc.)."""

    def set_orchestrator(self, orchestrator: object) -> None:
        """Optional hook so orchestrator can inject itself back."""

    @abstractmethod
    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        """Start listening. Call on_message(chat_id, text) for each incoming message."""

    @abstractmethod
    async def send(self, chat_id: int, response: Response) -> list[Path] | None:
        """Send a response to the user. Return the attachment paths actually
        handed off for delivery so the caller archives only what was sent;
        None or [] means nothing was delivered (don't archive)."""

    @abstractmethod
    async def send_typing(self, chat_id: int) -> None:
        """Show typing indicator. No-op if unsupported."""

    async def notify_queued(self, chat_id: int, position: int) -> None:
        """Tell the user their message is queued behind others.

        Default sends a text response. Frontends with cheaper signals (e.g.
        emoji reactions) should override.
        """
        await self.send(chat_id, Response(body=f"Queued ({position} pending)."))

    async def notify_start(self, chat_id: int) -> None:
        """Signal that processing has started for the next pending message.

        Frontends can use this to transition a queued indicator (e.g.
        :hourglass:) into a processing indicator (e.g. :eyes:). No-op by default.
        """

    async def notify_complete(self, chat_id: int) -> None:
        """Signal that processing has finished for the oldest in-flight message.

        Frontends can use this to clear a processing indicator (e.g. :eyes:)
        once the reply has been posted. No-op by default.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""

    def timeout_for(self, chat_id: int) -> float | None:
        """Per-message subprocess timeout in seconds. None = use agent default."""
        return None

    @abstractmethod
    def workspace_name(self, chat_id: int) -> str:
        """Human-readable workspace path segment, e.g. 'telegram/hoss'."""

    @abstractmethod
    def sender_name(self, chat_id: int) -> str:
        """The human-readable name of the person talking in this chat."""

    @abstractmethod
    def channel_context(self, chat_id: int) -> str:
        """Where this conversation is happening, e.g. 'dm', 'channel:#general'."""

    def describe(self) -> dict[str, str]:
        """Frontend-specific settings to print in the startup preview.

        Keys are field labels, values are stringified settings. Secrets must
        already be redacted by the frontend. Default is empty so frontends
        opt in only to what's useful.
        """
        return {}
