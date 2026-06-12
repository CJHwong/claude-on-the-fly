"""Telegram frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from datetime import datetime
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

from claude_on_the_fly.agent import DATA_DIR, Response, footer_parts
from claude_on_the_fly.protocol import Frontend

if TYPE_CHECKING:
    from claude_on_the_fly.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MEDIA_GROUP_WAIT = 0.5
# Telegram's sendPhoto only rasterizes these. SVG and everything else must go
# as a document or it 400s with Image_process_failed.
PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
# Persists the current /new session token per chat so a restart resumes the
# session the user was last on, instead of snapping back to the base session.
# Base sessions (no token) need no entry — their UUID is deterministic.
SESSIONS_FILE = DATA_DIR / "state" / "telegram-sessions.json"


class TelegramFrontend(Frontend):
    def __init__(self, token: str, allowed_user_id: int) -> None:
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._app: Application | None = None
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orchestrator: Orchestrator | None = None
        self._media_groups: dict[str, dict] = {}
        self._chat_names: dict[int, str] = {}
        # chat_id -> current session token (a /new timestamp). Absent = the base
        # session (no suffix). Tokens are unique and never recycle, so pruning
        # old workspaces can't make a future session collide with a stale one.
        self._session_tokens: dict[int, str] = {}

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        if not isinstance(orchestrator, Orchestrator):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        name = self._chat_names.get(chat_id, str(chat_id))
        token = self._session_tokens.get(chat_id)
        folder = f"{name}-{token}" if token else name
        return f"telegram/{folder}"

    def sender_name(self, chat_id: int) -> str:
        return self._chat_names.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        return "dm"  # Telegram bot is always a DM

    def describe(self) -> dict[str, str]:
        from claude_on_the_fly.orchestrator import _redact_token

        return {
            "bot_token": _redact_token(self._token),
            "allowed_user_id": str(self._allowed_user_id),
        }

    # --- Session persistence ---

    def _load_sessions(self) -> None:
        """Seed _session_tokens from disk and push each onto the orchestrator so
        the workspace suffix and session UUID resume in step after a restart."""
        try:
            data = json.loads(SESSIONS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self._session_tokens = {int(k): str(v) for k, v in data.items()}
        for chat_id, token in self._session_tokens.items():
            if self._orchestrator:
                self._orchestrator.set_session_token(chat_id, token)

    def _save_sessions(self) -> None:
        try:
            SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSIONS_FILE.write_text(
                json.dumps({str(k): v for k, v in self._session_tokens.items()})
            )
        except OSError:
            logger.exception(
                "telegram: failed to persist sessions to %s", SESSIONS_FILE
            )

    # --- Lifecycle ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message
        self._load_sessions()
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

    async def send(self, chat_id: int, response: Response) -> list[Path] | None:
        if not self._app:
            return []
        text = response.body
        stats, tools = footer_parts(response, "telegram")
        if stats:
            text = f"{text}\n\n_{stats}_"
        if tools:
            text = f"{text}\n_{tools}_"
        logger.info("chat %s => %s", chat_id, text[:80])
        await self._send_chunked(chat_id, text)
        await self._send_attachments(chat_id, response.attachments)
        return response.attachments

    async def _send_attachments(self, chat_id: int, attachments: list[Path]) -> None:
        """Send outbox files: raster images as photos, everything else as
        documents. Falls back to a document if a photo send is rejected (e.g.
        Telegram can't rasterize the file). Reads each file off the event loop;
        logs and continues per-file."""
        if not self._app:
            return
        for path in attachments:
            kind, _ = mimetypes.guess_type(path.name)
            try:
                data = await asyncio.to_thread(path.read_bytes)
                if kind in PHOTO_MIME_TYPES and await self._try_send_photo(
                    chat_id, path, data
                ):
                    logger.info("sent file %s to chat %s", path.name, chat_id)
                    continue
                await self._app.bot.send_document(
                    chat_id=chat_id, document=data, filename=path.name
                )
                logger.info("sent file %s to chat %s", path.name, chat_id)
            except Exception as exc:
                logger.error("send: failed to send file %s: %s", path.name, exc)

    async def _try_send_photo(self, chat_id: int, path: Path, data: bytes) -> bool:
        """Attempt a photo send with the already-read bytes. Returns False (so
        the caller falls back to a document) when Telegram rejects it."""
        if not self._app:
            return False
        try:
            await self._app.bot.send_photo(chat_id=chat_id, photo=data)
            return True
        except BadRequest as exc:
            logger.warning(
                "send: %s rejected as photo (%s), retrying as document",
                path.name,
                exc,
            )
            return False

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

    @staticmethod
    def _mint_session_token() -> str:
        """A unique, sortable token for a /new session. Timestamp-based, so it
        never recycles an old workspace (no disk scan, no counter that resets on
        restart) and survives pruning — deleting a workspace can't make a future
        token collide with a stale one."""
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        token = self._mint_session_token()
        self._session_tokens[chat_id] = token
        self._save_sessions()  # survive restart — resume this session, not base
        if self._orchestrator:
            # Keep the session UUID in step with the workspace suffix.
            self._orchestrator.set_session_token(chat_id, token)
        await update.message.reply_text(f"New session ({token}).")

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
        try:
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
            sender = self._chat_names.get(chat_id, "unknown")
            text = f"[from: {sender}] {text}"
            logger.info("chat %s: %s", chat_id, text[:80])
            if self._on_message:
                await self._on_message(chat_id, text)
        except Exception:
            logger.exception("Failed to process media group for chat %s", chat_id)


def main() -> None:  # pragma: no cover
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_telegram

    load_dotenv()
    token, allowed_user_id = run_telegram()
    frontend = TelegramFrontend(token=token, allowed_user_id=allowed_user_id)
    asyncio.run(run(frontend, platform="telegram"))


if __name__ == "__main__":  # pragma: no cover
    main()
