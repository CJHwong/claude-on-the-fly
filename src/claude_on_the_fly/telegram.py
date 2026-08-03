"""Telegram frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from claude_on_the_fly import logs, settings
from claude_on_the_fly.agent import (
    DATA_DIR,
    Response,
    footer_parts,
    install_download,
    read_attachment,
    sender_marker,
)
from claude_on_the_fly.approvals import ApprovalRequest
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
        # The event loop only holds a weak reference to a bare create_task, so a
        # flush sleeping out its MEDIA_GROUP_WAIT can be garbage-collected before
        # it ever delivers the album. Hold a strong reference until it finishes.
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._chat_names: dict[int, str] = {}
        # chat_id -> current session token (a /new timestamp). Absent = the base
        # session (no suffix). Tokens are unique and never recycle, so pruning
        # old workspaces can't make a future session collide with a stale one.
        self._session_tokens: dict[int, str] = {}
        # nonce -> future awaiting an approve/deny tap. Keyed by nonce because
        # Telegram caps callback_data at 64 bytes, too small for a host plus
        # port plus scope, and short enough that a subject would get truncated
        # into ambiguity.
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        if not isinstance(orchestrator, Orchestrator):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        token = self._session_tokens.get(chat_id)
        # User-controlled usernames and first names are display data, never
        # filesystem identifiers. The Telegram chat id is platform-assigned and
        # therefore cannot traverse the workspace or change the seatbelt project
        # grant. Existing name-based workspaces are intentionally not reused.
        folder = f"{chat_id}-{token}" if token else str(chat_id)
        return f"telegram/{folder}"

    def sender_name(self, chat_id: int) -> str:
        return self._chat_names.get(chat_id, "unknown")

    def sender_identity(self, chat_id: int) -> str:
        """Stable platform identity used for prompt/memory routing."""
        return str(chat_id)

    def channel_context(self, chat_id: int) -> str:
        return "dm"  # Telegram bot is always a DM

    def describe(self) -> dict[str, str]:
        from claude_on_the_fly.orchestrator import _redact_token

        return {
            "bot_token": _redact_token(self._token),
            "allowed_user_id": str(self._current_allowed_user_id()),
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
        # A real bot command rather than Slack's `$compact` text prefix: Telegram
        # delivers slash commands everywhere, so the `$` workaround Slack needs
        # (its own slash commands are blocked inside threads) buys nothing here,
        # and a command shows up in the client's command menu.
        self._app.add_handler(CommandHandler("compact", self._cmd_compact))
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
        self._app.add_handler(
            CallbackQueryHandler(self._on_approval_tap, pattern=r"^cotf-(grant|deny):")
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

    # --- Runtime permission approvals ---

    def _approval_chat(self, chat_id: int | None = None) -> int | None:
        """Where an approval prompt goes.

        The session's own chat, so the question appears where the work is. For
        work with no conversation behind it (cron, the job queue), the allowed
        user's DM -- a Telegram DM chat id equals the user id, so the fallback is
        never a group.

        There is deliberately no override: a prompt belongs with the work that
        caused it, and routing it elsewhere only makes it harder to judge. Slack
        goes further and denies for sessionless work rather than falling back at
        all, because nobody is watching a cron job's thread.
        """
        return chat_id if chat_id is not None else self._current_allowed_user_id()

    async def ask_approval(
        self, request: ApprovalRequest, chat_id: int | None = None
    ) -> bool:
        """Post an approve/deny keyboard and wait for a tap.

        The caller (approvals.ApprovalBroker) owns the timeout and treats any
        exception as a denial, so this only has to resolve or raise.
        """
        chat_id = self._approval_chat(chat_id)
        if not self._app or chat_id is None:
            return False
        nonce = secrets.token_urlsafe(8)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_approvals[nonce] = future
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"cotf-grant:{nonce}"
                    ),
                    InlineKeyboardButton("Deny", callback_data=f"cotf-deny:{nonce}"),
                ]
            ]
        )
        minutes = request.ttl_seconds / 60
        # Escaped, not interpolated raw. Parts of a subject/detail are agent-
        # reachable -- a broker route-scope request carries the path tail the agent
        # asked for -- so unescaped Markdown here lets the agent style the operator's
        # own prompt, hiding the real subject behind formatting or a fake verdict
        # line. Escaping keeps the one text the operator must be able to trust
        # literal.
        subject = escape_markdown(request.subject, version=1)
        detail = escape_markdown(request.detail, version=1)
        # Same split as the Slack card: an `origin` requester has a digest for a
        # subject, so the readable label leads and the digest drops to the footer
        # where it is still available for matching against the grant log.
        # The footer is the lifetime and nothing else. Repeating the subject, or the
        # scope in front of it, said the same thing the detail block already says --
        # see slack._approval_footer for the card that made that obvious.
        foot = f"Grant lasts {minutes:.0f} min and dies on restart."
        if request.origin:
            head = (
                f"*Permission request* ({escape_markdown(request.origin, version=1)})"
            )
        else:
            head = f"*Permission request*\n\n`{subject}`"
        message = await self._app.bot.send_message(
            chat_id=chat_id,
            text=f"{head}\n\n{detail}\n\n{foot}",
            parse_mode="Markdown",
            reply_markup=keyboard,
            # The detail names the host the agent asked for, so a preview would
            # fetch the very destination being gated -- from the operator's client,
            # before any decision, which is both noise and a small leak of the
            # pending request. Also keeps the buttons above the fold on a phone.
            disable_web_page_preview=True,
        )
        granted = False
        try:
            granted = await future
        finally:
            # Reached on the caller's timeout cancellation too, so a stale
            # nonce can never accumulate or be answered later. The prompt is
            # retired in here for the same reason: on the timeout path the
            # cancellation used to skip it, leaving a spent card with a
            # live-looking keyboard in the chat forever.
            self._pending_approvals.pop(nonce, None)
            verdict = "APPROVED" if granted else "DENIED"
            try:
                await self._retire_approval(
                    chat_id, message.message_id, request, verdict
                )
            except Exception:
                # Never let a cosmetic edit change the answer, but never lose it
                # either: `_retire_approval` already logs the BadRequest it
                # expects, so anything arriving here is a surprise and the one
                # place it would be visible is this log line.
                logger.exception("telegram: retiring the approval prompt failed")
        return granted

    async def _retire_approval(
        self, chat_id: int, message_id: int, request: ApprovalRequest, verdict: str
    ) -> None:
        """Strip the buttons and stamp the outcome so the prompt can't be reused."""
        if self._app is None:
            return
        # The scope, not the subject: a subject is the grant key, scoped to the
        # program, so this read "Permission approved / bash:chmod" -- a record that
        # does not say which file. The scope carries the arguments.
        decided = request.scope or request.subject
        try:
            await self._app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"*Permission {verdict}*\n\n`{escape_markdown(decided, version=1)}`"
                ),
                parse_mode="Markdown",
            )
        except BadRequest as exc:
            logger.warning("could not retire approval prompt: %s", exc)

    async def _on_approval_tap(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resolve a pending approval from a button tap."""
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        # Only the configured operator may widen policy. Without this any user
        # who can see the message could grant the agent a new host.
        if not self._allowed(update):
            logger.warning(
                "ignoring approval tap from unauthorized user %s",
                update.effective_user.id if update.effective_user else "unknown",
            )
            return
        action, _, nonce = query.data.partition(":")
        future = self._pending_approvals.get(nonce)
        if future is None or future.done():
            logger.info("approval tap for unknown or settled nonce %s", nonce)
            return
        future.set_result(action == "cotf-grant")

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
        logger.info("chat %s => %s", chat_id, logs.redact(text))
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
                data = await asyncio.to_thread(read_attachment, path)
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
            and update.effective_user.id == self._current_allowed_user_id()
        )

    def _current_allowed_user_id(self) -> int:
        """Authorization principal, re-read so revocation applies immediately."""
        raw = settings.get("TELEGRAM_ALLOWED_USER_ID", str(self._allowed_user_id))
        try:
            return int(raw)
        except ValueError:
            logger.error(
                "telegram.allowed_user_id=%r is not an integer; retaining the "
                "last valid startup value",
                raw,
            )
            return self._allowed_user_id

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
            text = f"{sender_marker(chat_id, sender)} {text}"
            logger.info("chat %s: %s", chat_id, logs.redact(text))
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

    async def _cmd_compact(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Summarize this chat's history so later turns stop re-paying for it.

        Queued as a turn rather than run here, so it takes its place in FIFO order
        and reports through the same path a reply does. Auto-compaction already
        reached Telegram (it lives in `Orchestrator.on_message`); without this it
        could fire with nothing the user could do to trigger — or to expect — it.
        """
        if not self._allowed(update) or not update.message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        if not self._orchestrator:
            await update.message.reply_text("Not connected to a session yet.")
            return
        await self._orchestrator.on_compact(chat_id)

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
        fd, temp_name = tempfile.mkstemp(prefix=".cotf-download-", dir=workspace)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            await tg_file.download_to_drive(temp_path)
            install_download(temp_path, dest)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
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
            task = asyncio.create_task(self._flush_media_group(group_id))
            self._flush_tasks.add(task)
            task.add_done_callback(self._flush_tasks.discard)
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
            text = f"{sender_marker(chat_id, sender)} {text}"
            logger.info("chat %s: %s", chat_id, logs.redact(text))
            if self._on_message:
                await self._on_message(chat_id, text)
        except Exception:
            logger.exception("Failed to process media group for chat %s", chat_id)


def main() -> None:
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_telegram

    load_dotenv()
    token, allowed_user_id = run_telegram()
    frontend = TelegramFrontend(token=token, allowed_user_id=allowed_user_id)
    asyncio.run(run(frontend, platform="telegram"))


if __name__ == "__main__":  # pragma: no cover
    main()
