"""Telegram frontend."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_on_the_fly.agent import Response
from claude_on_the_fly.protocol import Frontend

if TYPE_CHECKING:
    from claude_on_the_fly.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MEDIA_GROUP_WAIT = 0.5


class TelegramFrontend(Frontend):
    def __init__(self, token: str, allowed_user_id: int) -> None:
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._app: Application | None = None
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orchestrator: Orchestrator | None = None
        self._media_groups: dict[str, dict] = {}
        self._chat_names: dict[int, str] = {}
        self._session_counters: dict[int, int] = {}

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        if not isinstance(orchestrator, Orchestrator):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        name = self._chat_names.get(chat_id, str(chat_id))
        counter = self._session_counters.get(chat_id, 0)
        folder = name if counter == 0 else f"{name}-{counter}"
        return f"telegram/{folder}"

    def sender_name(self, chat_id: int) -> str:
        return self._chat_names.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        return "dm"  # Telegram bot is always a DM

    # --- Lifecycle ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.Document.ALL | filters.PHOTO)
                & ~filters.COMMAND,
                self._on_update,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.VOICE | filters.AUDIO | filters.VIDEO | filters.Sticker.ALL,
                self._on_unsupported,
            )
        )
        await self._app.initialize()
        await self._app.start()
        if not self._app.updater:
            raise RuntimeError("Application has no updater")
        await self._app.updater.start_polling()
        logger.info("Telegram bot started polling.")

    async def stop(self) -> None:
        if self._app:
            if self._app.updater:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # --- Sending ---

    async def send(self, chat_id: int, response: Response) -> None:
        if not self._app:
            return
        text = response.body
        if response.has_stats:
            text = f"{text}\n\n_{response.format_stats()}_"
        await self._send_chunked(chat_id, text)

    async def send_typing(self, chat_id: int) -> None:
        if self._app:
            await self._app.bot.send_chat_action(chat_id=chat_id, action="typing")

    async def _send_chunked(self, chat_id: int, text: str) -> None:
        lines = text.split("\n")
        chunk = ""
        for line in lines:
            candidate = f"{chunk}\n{line}" if chunk else line
            if len(candidate) > MAX_MESSAGE_LENGTH:
                if chunk:
                    await self._send_msg(chat_id, chunk)
                chunk = line[:MAX_MESSAGE_LENGTH]
            else:
                chunk = candidate
        if chunk:
            await self._send_msg(chat_id, chunk)

    async def _send_msg(self, chat_id: int, text: str) -> None:
        if not self._app:
            raise RuntimeError("App not started")
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown"
            )
        except BadRequest:
            await self._app.bot.send_message(chat_id=chat_id, text=text)

    # --- Receiving ---

    def _allowed(self, update: Update) -> bool:
        return (
            update.effective_user is not None
            and update.effective_user.id == self._allowed_user_id
        )

    async def _on_update(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message or not update.effective_chat:
            return
        msg = update.message
        chat_id = update.effective_chat.id
        self._track_name(chat_id, update.effective_user)
        caption = msg.text or msg.caption or ""

        file_id, file_name = self._extract_file(msg)

        if file_id and file_name and msg.media_group_id:
            self._enqueue_media_group(
                msg.media_group_id, chat_id, file_id, file_name, caption
            )
            return

        if file_id and file_name:
            await self._save_file(chat_id, file_id, file_name)
            text = f"[File saved: {file_name}]\n{caption or 'Please review the uploaded file.'}"
        else:
            text = caption

        if text:
            sender = self._chat_names.get(chat_id, "unknown")
            text = f"[from: {sender}] {text}"
            logger.info("chat %s: %s", chat_id, text[:80])
            if self._on_message:
                await self._on_message(chat_id, text)

    async def _on_unsupported(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._allowed(update) and update.message:
            await update.message.reply_text(
                "Audio, video, and stickers aren't supported yet. Send text, files, or images."
            )

    # --- Commands ---

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        self._session_counters[chat_id] = self._session_counters.get(chat_id, 0) + 1
        if self._orchestrator:
            self._orchestrator.reset_session(chat_id)
        await update.message.reply_text(
            f"New session (#{self._session_counters[chat_id]})."
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        if self._orchestrator:
            busy = self._orchestrator.is_busy(chat_id)
            queued = self._orchestrator.queue_size(chat_id)
            status = "Working..." if busy else "Idle."
            if queued:
                status += f" ({queued} queued)"
            await update.message.reply_text(status)
        else:
            await update.message.reply_text("Idle.")

    # --- Files ---

    def _track_name(self, chat_id: int, user) -> None:
        if user and chat_id not in self._chat_names:
            self._chat_names[chat_id] = user.username or user.first_name or str(chat_id)

    @staticmethod
    def _extract_file(msg) -> tuple[str | None, str | None]:
        if msg.document:
            return msg.document.file_id, msg.document.file_name or "document"
        if msg.photo:
            return msg.photo[-1].file_id, f"photo_{msg.message_id}.jpg"
        return None, None

    async def _save_file(self, chat_id: int, file_id: str, file_name: str) -> Path:
        if not self._app:
            raise RuntimeError("App not started")
        workspace = (
            Path.home()
            / ".claude-on-the-fly"
            / "workspaces"
            / self.workspace_name(chat_id)
        )
        workspace.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file_name).name
        dest = workspace / safe_name
        tg_file = await self._app.bot.get_file(file_id)
        await tg_file.download_to_drive(dest)
        logger.info("Saved file %s for chat %s", file_name, chat_id)
        return dest

    def _enqueue_media_group(
        self,
        group_id: str,
        chat_id: int,
        file_id: str,
        file_name: str,
        caption: str,
    ) -> None:
        if group_id not in self._media_groups:
            self._media_groups[group_id] = {
                "chat_id": chat_id,
                "files": [],
                "caption": caption,
            }
            asyncio.create_task(self._flush_media_group(group_id))
        self._media_groups[group_id]["files"].append((file_id, file_name))
        if caption:
            self._media_groups[group_id]["caption"] = caption

    async def _flush_media_group(self, group_id: str) -> None:
        await asyncio.sleep(MEDIA_GROUP_WAIT)
        group = self._media_groups.pop(group_id, None)
        if not group:
            return
        chat_id = group["chat_id"]
        file_lines = []
        for file_id, file_name in group["files"]:
            await self._save_file(chat_id, file_id, file_name)
            file_lines.append(f"[File saved: {file_name}]")
        text = "\n".join(file_lines)
        text += (
            f"\n{group['caption']}"
            if group["caption"]
            else "\nPlease review the uploaded files."
        )
        logger.info("chat %s: %s", chat_id, text[:80])
        if self._on_message:
            await self._on_message(chat_id, text)


def main() -> None:
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_telegram

    load_dotenv()
    token, allowed_user_id = run_telegram()
    frontend = TelegramFrontend(token=token, allowed_user_id=allowed_user_id)
    asyncio.run(run(frontend, platform="telegram"))


if __name__ == "__main__":
    main()
