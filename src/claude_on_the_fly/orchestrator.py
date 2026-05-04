"""Session management, message queuing, and agent execution."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import ClaudeUnavailableError, Response
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"


class Orchestrator:
    def __init__(self, frontend: Frontend, platform: str) -> None:
        self._frontend = frontend
        self._platform = platform
        self._running: dict[int, asyncio.Task] = {}
        self._session_counters: dict[int, int] = {}
        self._queues: dict[int, asyncio.Queue] = {}

    def session_uuid(self, chat_id: int) -> str:
        counter = self._session_counters.get(chat_id, 0)
        tag = f"{chat_id}" if counter == 0 else f"{chat_id}-{counter}"
        return str(uuid5(NAMESPACE_URL, tag))

    def reset_session(self, chat_id: int) -> None:
        self._session_counters[chat_id] = self._session_counters.get(chat_id, 0) + 1

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running and not self._running[chat_id].done()

    def queue_size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

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

    @staticmethod
    def _ensure_persona(workspace: Path) -> None:
        """Symlink the global CLAUDE.md persona into the workspace if it exists."""
        source = DATA_DIR / "CLAUDE.md"
        if not source.is_file():
            return
        target = workspace / "CLAUDE.md"
        if target.is_symlink() and target.resolve() == source.resolve():
            return
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(source)

    async def _process(self, chat_id: int, text: str) -> None:
        workspace = DATA_DIR / "workspaces" / self._frontend.workspace_name(chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        self._ensure_persona(workspace)
        session = self.session_uuid(chat_id)
        logger.debug(
            "process: chat_id=%s workspace=%s session=%s", chat_id, workspace, session
        )

        await self._frontend.notify_start(chat_id)
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
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
            await self._frontend.send(chat_id, response)
        except ClaudeUnavailableError as exc:
            logger.warning("Claude unavailable for chat %s: %s", chat_id, exc)
            await self._frontend.send(
                chat_id, Response(body=f"Claude unavailable: {exc}")
            )
        except Exception as exc:
            logger.exception("Agent error for chat %s", chat_id)
            await self._frontend.send(chat_id, Response(body=f"Error: {exc}"))
        finally:
            typing_task.cancel()
            await self._frontend.notify_complete(chat_id)

    async def shutdown(self) -> None:
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


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

    orch = Orchestrator(frontend, platform)
    frontend.set_orchestrator(orch)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    frontend_task = asyncio.create_task(frontend.start(orch.on_message))
    logger.info("Running (%s). Ctrl+C to stop.", platform)

    await stop.wait()

    logger.info("Shutting down...")
    frontend_task.cancel()
    await asyncio.gather(frontend_task, return_exceptions=True)
    await orch.shutdown()
    await frontend.stop()
