"""Session management, message queuing, and agent execution."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import time
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import (
    DATA_DIR,
    ClaudeUnavailableError,
    Response,
    current_backend_key,
)
from claude_on_the_fly.events import (
    EVENT_DISPATCHED,
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from claude_on_the_fly.heartbeat import HeartbeatWriter
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        frontend: Frontend,
        platform: str,
        event_log: EventLog | None = None,
    ) -> None:
        self._frontend = frontend
        self._platform = platform
        self._running: dict[int, asyncio.Task] = {}
        # Session discriminator per chat: the scheduler bumps an int via
        # reset_session; telegram /new pins a string token via set_session_token.
        # Either feeds session_uuid's `{chat_id}-{value}` tag.
        self._session_counters: dict[int, int | str] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._event_log = event_log if event_log is not None else EventLog()
        # chat_id -> {identifier, started_at_monotonic, session_uuid}.
        # Populated at dispatch, cleared on completion. Drives the heartbeat
        # `running_jobs` slot consumed by the TUI's Active AI jobs pane.
        self._in_flight: dict[int, dict] = {}

    def session_uuid(self, chat_id: int) -> str:
        counter = self._session_counters.get(chat_id, 0)
        tag = f"{chat_id}" if counter == 0 else f"{chat_id}-{counter}"
        return str(uuid5(NAMESPACE_URL, tag))

    def reset_session(self, chat_id: int) -> None:
        # The scheduler uses an int counter here; telegram pins a str token via
        # set_session_token. Only the int form is bumped (different chat_id
        # spaces), so coerce defensively to keep the +1 well-typed.
        current = self._session_counters.get(chat_id, 0)
        self._session_counters[chat_id] = (
            current if isinstance(current, int) else 0
        ) + 1

    def set_session_token(self, chat_id: int, token: str) -> None:
        """Pin the session discriminator to a token the frontend minted, so the
        session UUID matches the frontend's workspace suffix (telegram's /new
        uses a unique timestamp token). The tag formatting in session_uuid
        accepts a string just as it does the scheduler's integer counter, which
        reset_session still bumps."""
        self._session_counters[chat_id] = token

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running and not self._running[chat_id].done()

    def queue_size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

    async def abort(self, chat_id: int) -> bool:
        """Stop the in-flight turn for a chat and drop anything queued behind it.

        Cancelling the drain task raises CancelledError into the awaited
        agent.run; the backend's exec finally reaps the whole process tree
        (spawned with start_new_session), so the agent CLI and its tool
        subprocesses die together instead of orphaning. Returns whether a turn
        was actually running.
        """
        queue = self._queues.get(chat_id)
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        task = self._running.get(chat_id)
        if task is None or task.done():
            return False
        logger.info("abort: chat_id=%s cancelling in-flight turn", chat_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def on_message(self, chat_id: int, text: str) -> None:
        logger.debug("on_message: chat_id=%s text=%s", chat_id, text[:80])
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
        self._queues[chat_id].put_nowait(text)
        if self.is_busy(chat_id):
            queued = self._queues[chat_id].qsize()
            logger.debug("on_message: chat_id=%s busy, queued=%s", chat_id, queued)
            await self._frontend.notify_queued(chat_id, queued)
        else:
            logger.debug("on_message: chat_id=%s starting drain", chat_id)
            self._running[chat_id] = asyncio.create_task(self._drain(chat_id))

    async def _drain(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        try:
            while True:
                try:
                    text = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._process(chat_id, text)
        finally:
            if self._running.get(chat_id) is asyncio.current_task():
                self._running.pop(chat_id, None)

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            await self._frontend.send_typing(chat_id)
            await asyncio.sleep(4)

    async def _process(self, chat_id: int, text: str) -> None:
        workspace = DATA_DIR / "workspaces" / self._frontend.workspace_name(chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        if self._platform in agent.ATTACHMENT_PLATFORMS:
            (workspace / agent.OUTBOX_DIRNAME).mkdir(exist_ok=True)
        agent.ensure_persona(workspace)
        session = self.session_uuid(chat_id)
        identifier = self._frontend.workspace_name(chat_id)
        logger.debug(
            "process: chat_id=%s workspace=%s session=%s", chat_id, workspace, session
        )

        self._event_log.append(
            EVENT_DISPATCHED,
            source=self._platform,
            backend=current_backend_key(),
            identifier=identifier,
            workspace=workspace,
            session_uuid=session,
        )
        self._in_flight[chat_id] = {
            "identifier": identifier,
            "started_at_monotonic": time.monotonic(),
            "session_uuid": session,
        }

        # Startup notification performs frontend I/O and is therefore a
        # cancellation point. Keep it inside the lifecycle try/finally so an
        # abort while it is in progress still clears the in-flight slot and
        # any partially-applied frontend status/reaction.
        typing_task: asyncio.Task | None = None
        try:
            await self._frontend.notify_start(chat_id)
            typing_task = asyncio.create_task(self._typing_loop(chat_id))
            response = await agent.run(
                workspace,
                session,
                text,
                self._platform,
                user_name=self._frontend.sender_name(chat_id),
                channel_context=self._frontend.channel_context(chat_id),
                timeout=self._frontend.timeout_for(chat_id),
            )
            logger.debug(
                "process: chat_id=%s response cost=%.4f tokens_in=%s tokens_out=%s",
                chat_id,
                response.cost,
                response.tokens_in,
                response.tokens_out,
            )
            if self._platform in agent.ATTACHMENT_PLATFORMS:
                response.attachments = agent.collect_outbox(workspace)
            delivered = await self._frontend.send(chat_id, response)
            if delivered:
                agent.archive_outbox(workspace, delivered)
            self._event_log.append(
                EVENT_WORKER_DONE,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                cost=response.cost,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
            )
        except asyncio.CancelledError:
            logger.info("process: chat_id=%s aborted", chat_id)
            raise
        except ClaudeUnavailableError as exc:
            logger.warning("Claude unavailable for chat %s: %s", chat_id, exc)
            await self._frontend.send(
                chat_id, Response(body=f"Claude unavailable: {exc}")
            )
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
                reason="unavailable",
            )
        except Exception as exc:
            logger.exception("Agent error for chat %s", chat_id)
            await self._frontend.send(chat_id, Response(body=f"Error: {exc}"))
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
            )
        finally:
            self._in_flight.pop(chat_id, None)
            if typing_task is not None:
                typing_task.cancel()
            await self._frontend.notify_complete(chat_id)

    def heartbeat_extra(self) -> dict:
        """Snapshot in-flight chat jobs for the TUI's Active AI jobs pane.

        Shape mirrors symphony's running_tickets so the dashboard can merge
        across sources with a single normalizer.
        """
        now = time.monotonic()
        running_jobs = [
            {
                "identifier": j["identifier"],
                "chat_id": chat_id,
                "uptime_s": int(now - j["started_at_monotonic"]),
                "session_uuid": j["session_uuid"],
            }
            for chat_id, j in self._in_flight.items()
        ]
        return {"running_jobs": running_jobs}

    async def shutdown(self) -> None:
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


def _redact_token(token: str) -> str:
    """Mask a secret for log output. Matches symphony's redaction format."""
    if not token:
        return "<unset>"
    if len(token) <= 4:
        return "***"
    return f"{token[:2]}***{token[-2:]}"


def _log_settings_summary(platform: str, frontend: Frontend) -> None:
    """Dump the resolved runtime settings at startup, symphony-style.

    Pulls shared bits (log level, data dir, agent backend) from env, then
    appends frontend-specific fields via Frontend.describe(). Secrets are
    expected to be redacted by the frontend before being returned.
    """
    import os

    backend = os.environ.get("AGENT_BACKEND", "claude").lower()
    mode_var = f"{backend.upper()}_MODE"
    mode = os.environ.get(mode_var, "native").lower()

    logger.info("%s settings:", platform)
    logger.info("  platform        = %s", platform)
    logger.info("  log_level       = %s", os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.info("  data_dir        = %s", DATA_DIR)
    logger.info("  agent_backend   = %s", backend)
    logger.info("  %-15s = %s", mode_var.lower(), mode)
    if mode == "ollama":
        logger.info("  ollama_model    = %s", os.environ.get("OLLAMA_MODEL", "<unset>"))

    for label, value in frontend.describe().items():
        logger.info("  %-15s = %s", label, value)


async def run(frontend: Frontend, platform: str) -> None:
    """Start the orchestrator with the given frontend. Blocks until SIGINT/SIGTERM."""
    import os

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Console: respects LOG_LEVEL
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_fmt,
    )

    # File: always DEBUG, daily rotation, 7 days
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / f"{platform}.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)
    (DATA_DIR / "memory" / "users").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)

    _log_settings_summary(platform, frontend)

    orch = Orchestrator(frontend, platform)
    frontend.set_orchestrator(orch)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    heartbeat = HeartbeatWriter(platform, extra_provider=orch.heartbeat_extra)
    heartbeat_task = asyncio.create_task(heartbeat.run())

    frontend_task = asyncio.create_task(frontend.start(orch.on_message))
    logger.info("Running (%s). Ctrl+C to stop.", platform)

    await stop.wait()

    logger.info("Shutting down...")
    heartbeat_task.cancel()
    frontend_task.cancel()
    await asyncio.gather(heartbeat_task, frontend_task, return_exceptions=True)
    await orch.shutdown()
    await frontend.stop()
    try:
        heartbeat.path.unlink()
    except FileNotFoundError:
        pass
