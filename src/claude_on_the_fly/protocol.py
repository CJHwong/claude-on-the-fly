"""Frontend ABC - what each messaging platform must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_on_the_fly.agent import Response
from claude_on_the_fly.approvals import ApprovalRequest


def interrupted_notice(*, running: bool, queued: int) -> str:
    """The text a chat gets when a daemon stop interrupts its unfinished work.

    A module function so every frontend and the tests word it identically, and
    so a frontend that overrides `notify_interrupted` for a richer format can
    still reuse the sentence.

    The fallback, not the normal path. A frontend that can show state without
    talking should do that instead (Slack puts the message back to an hourglass),
    because a stop costs nobody an answer now and prose about it is the daemon
    narrating its own lifecycle.

    So it is written as a person would say it in passing, not as a status report:
    short, no restatement of what the reader can see, and no instruction, since
    there is nothing for them to do.
    """
    total = queued + (1 if running else 0)
    if total > 1:
        return "Restarting. I'll get back to these in a moment."
    return "Restarting. I'll get back to this in a moment."


def nudge_notice(text: str) -> str:
    """The text offering back a turn the daemon has stopped retrying.

    Only reached when a replay is the wrong answer: a turn that has already been
    replayed to its limit, most likely because running it is what keeps taking
    the daemon down. Everything else resumes on its own.

    The prompt is quoted with its sender markers stripped: they are prompt
    grammar for the model, and quoting them back at the person who typed the
    message shows them scaffolding they never wrote.
    """
    from claude_on_the_fly.agent import strip_sender_markers

    return (
        "I keep failing on this one, so I have stopped retrying it:\n\n"
        f"{strip_sender_markers(text)}\n\nSend it again if you want me to try once more."
    )


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

    async def notify_interrupted(
        self, chat_id: int, *, running: bool, queued: int
    ) -> None:
        """Tell a chat that a daemon stop interrupted its unfinished work.

        Called once per affected chat while the daemon shuts down, before the
        in-flight turn is cancelled. `running` says a turn was mid-answer;
        `queued` counts the turns waiting behind it. Both are journaled
        (`turns.py`) and both come back on the next start, so this explains the
        pause rather than asking for anything.

        Default sends a text response. A frontend with a cheaper signal may
        override, but it must still say something: without it the sender watches
        a turn go quiet with no idea why.
        """
        await self.send(
            chat_id, Response(body=interrupted_notice(running=running, queued=queued))
        )

    def route_for(self, chat_id: int) -> dict:
        """Routing context to journal with a pending turn, for after a restart.

        A chat id is not always enough to reach the conversation again. Slack's
        is `sha256(channel:thread_ts)`, one way, so a reply has nowhere to go
        unless the pair travels with the turn. Telegram's chat id *is* the
        address, hence the empty default.

        Whatever is returned must be JSON-serializable and must not contain a
        credential: it lands in a file on disk. Nothing but the frontend that
        produced it ever reads it.
        """
        return {}

    def restore_route(self, chat_id: int, route: dict) -> None:
        """Re-register a route read back from the journal, before a replay.

        The frontend's own session tables are in memory and empty after a
        restart, so a replayed turn would otherwise run and then have nowhere to
        post. Slack already does exactly this when a suggestion button is tapped
        after a restart; this is the same move from the journal instead of a tap.
        """

    async def notify_resumed(self, chat_id: int, count: int) -> None:
        """Signal that pending turns are being picked back up. No-op by default.

        Deliberately silent. A resumed turn is announced by the same thing that
        announces a fresh one -- the reaction it gets while it runs and the reply
        it posts when it finishes -- so a message here would be the daemon
        talking about itself instead of doing the work. A frontend with no such
        affordance at all may override this to say something.
        """

    async def notify_nudge(self, chat_id: int, text: str) -> None:
        """Offer back a turn the daemon has stopped retrying.

        Rare by design: everything else resumes silently, and this is reached
        only for a turn that hit the replay limit. Default posts the prompt as
        text for the person to resend. A frontend with a tappable affordance
        should override and offer it as one, since a button carries the text back
        verbatim with no copy and paste.
        """
        await self.send(chat_id, Response(body=nudge_notice(text)))

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

    def persona_source(self, chat_id: int) -> Path | None:
        """Per-chat persona file, or None for the global CLAUDE.md.

        Override to let one channel or DM run different instructions than the
        rest; see `agent.persona_for`. Default is None, so a frontend that has no
        opinion keeps the single global persona.
        """
        return None

    def describe(self) -> dict[str, str]:
        """Frontend-specific settings to print in the startup preview.

        Keys are field labels, values are stringified settings. Secrets must
        already be redacted by the frontend. Default is empty so frontends
        opt in only to what's useful.
        """
        return {}
