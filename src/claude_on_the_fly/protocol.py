"""Frontend ABC - what each messaging platform must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_on_the_fly.agent import Response
from claude_on_the_fly.approvals import ApprovalRequest


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

    async def send_progress(self, chat_id: int, text: str) -> None:
        """Deliver one mid-turn narration message from the agent, while it runs.

        Called only while a turn is running, only when `interim.progress` is on,
        and only with the main agent's own text — not thinking, not sub-agent
        output. The caller has already coalesced and rate-limited it, so one call
        is one message the user should see. Implementations must mark it as machine
        progress, distinct from the reply `send()` will post, and must not count it
        against any reply budget.

        No-op by default, so a frontend with no thread to put it in — or no wish
        to — behaves exactly as it did before this existed.

        Best-effort, and deliberately one-way: an implementation that cannot
        deliver returns without saying so, and the caller has already started its
        next rate-limiting gap by the time this runs. A dropped message therefore
        costs a whole gap of silence rather than being retried — the accepted
        price of not putting a delivery-outcome contract on every frontend.
        """

    async def ask_approval(
        self, request: ApprovalRequest, chat_id: int | None = None
    ) -> bool:
        """Ask the operator to grant a runtime permission. True to grant.

        `chat_id` is the session whose agent triggered the request, so the prompt
        can land in the conversation that caused it. None means the request has no
        session context (cron, the job queue), and the implementation should fall
        back to a configured operator destination.

        Default denies, so a frontend that hasn't implemented an approval UI
        behaves exactly like one with no approval channel rather than silently
        granting. Implementations must also return False when they have nowhere
        to ask.

        Implementations must present request.detail verbatim, because it states
        what the sandbox actually observed. Verbatim means *neutralised, not
        reworded*: parts of a subject and detail are agent-reachable — a broker
        route-scope request carries the path tail the agent asked for — so an
        implementation rendering them as markup lets the agent restyle the
        operator's own prompt and hide the real subject behind a fake verdict
        line. Escape them, or put them somewhere the platform parses no markup.

        Whoever may *click* is a separate check the implementation owns, and it
        must not be the whole audience of the channel the prompt lands in.
        """
        return False

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
