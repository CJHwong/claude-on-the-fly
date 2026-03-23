"""Session management, message queuing, and agent execution."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import agent
from claude_on_the_fly.agent import Response
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"


class Orchestrator:
    def __init__(self, frontend: Frontend, platform: str) -> None:
        self._frontend = frontend
        self._platform = platform
        self._running: dict[int, asyncio.Task] = {}
        self._started: set[int] = set()
        self._session_counters: dict[int, int] = {}
        self._queues: dict[int, asyncio.Queue] = {}

    def session_uuid(self, chat_id: int) -> str:
        counter = self._session_counters.get(chat_id, 0)
        tag = f"{chat_id}" if counter == 0 else f"{chat_id}-{counter}"
        return str(uuid5(NAMESPACE_URL, tag))

    def reset_session(self, chat_id: int) -> None:
        self._session_counters[chat_id] = self._session_counters.get(chat_id, 0) + 1
        self._started.discard(chat_id)

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running and not self._running[chat_id].done()

    def queue_size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

    async def on_message(self, chat_id: int, text: str) -> None:
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
        self._queues[chat_id].put_nowait(text)
        if self.is_busy(chat_id):
            queued = self._queues[chat_id].qsize()
            await self._frontend.send(
                chat_id, Response(body=f"Queued ({queued} pending).")
            )
        else:
            self._running[chat_id] = asyncio.create_task(self._drain(chat_id))

    async def _drain(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        try:
            while not queue.empty():
                text = await queue.get()
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
        session = self.session_uuid(chat_id)
        is_resume = chat_id in self._started

        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            response = await agent.run(
                workspace,
                session,
                text,
                is_resume,
                self._platform,
                user_name=self._frontend.sender_name(chat_id),
                channel_context=self._frontend.channel_context(chat_id),
            )
            self._started.add(chat_id)
            await self._frontend.send(chat_id, response)
        except Exception as exc:
            logger.exception("Agent error for chat %s", chat_id)
            await self._frontend.send(chat_id, Response(body=f"Error: {exc}"))
        finally:
            typing_task.cancel()

    async def shutdown(self) -> None:
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


async def run(frontend: Frontend, platform: str) -> None:
    """Start the orchestrator with the given frontend. Blocks until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    (DATA_DIR / "memory" / "users").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)

    orch = Orchestrator(frontend, platform)
    frontend.set_orchestrator(orch)

    await frontend.start(orch.on_message)
    logger.info("Running (%s). Ctrl+C to stop.", platform)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    logger.info("Shutting down...")
    await orch.shutdown()
    await frontend.stop()
