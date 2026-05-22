"""Gmail frontend. Watches inbox via gws CLI and replies as plain text."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from typing import Awaitable, Callable

from claude_on_the_fly.agent import Response, footer_parts
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)

_RE_WROTE_EN = re.compile(r"^On .+ wrote:\s*$")
_RE_WROTE_LOCALIZED = re.compile(r"^.+\d{4}.+[:：]\s*$")


def _thread_key(thread_id: str) -> int:
    return int(hashlib.sha256(thread_id.encode()).hexdigest()[:16], 16)


def _extract_header(headers: list[dict], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _parse_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email) from a From header like 'Alice <alice@x.com>'."""
    if "<" in from_header and ">" in from_header:
        email = from_header.split("<")[1].split(">")[0].strip()
        name = from_header.split("<")[0].strip().strip('"')
        return name or email, email
    return from_header.strip(), from_header.strip()


def _strip_quoted(text: str) -> str:
    """Strip quoted reply content from an email body."""
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        # "On <date>, <name> wrote:" pattern (English)
        if _RE_WROTE_EN.match(line):
            return "\n".join(lines[:idx]).rstrip()
        # Gmail-style localized: line contains a year + ends with colon variant,
        # followed (possibly after blank lines) by ">" quoted block
        if _RE_WROTE_LOCALIZED.match(line):
            rest = [ln for ln in lines[idx + 1 :] if ln.strip()]
            if rest and rest[0].startswith(">"):
                return "\n".join(lines[:idx]).rstrip()
    # Fallback: strip trailing ">" quoted blocks
    while lines and (lines[-1].startswith(">") or lines[-1].strip() == ""):
        lines.pop()
    return "\n".join(lines).rstrip()


def _extract_plain_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload, latest part only."""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in parts:
        result = _extract_plain_body(part)
        if result:
            return result

    return ""


class GmailFrontend(Frontend):
    def __init__(
        self,
        gcp_project: str,
        allowed_senders: set[str],
        poll_interval: int = 5,
    ) -> None:
        self._gcp_project = gcp_project
        self._allow_all_senders = False
        self._allowed_emails: set[str] = set()
        self._allowed_domains: set[str] = set()
        for entry in allowed_senders:
            normalized = entry.strip().lower()
            if not normalized:
                continue
            if normalized == "*":
                self._allow_all_senders = True
            elif normalized.startswith("*@") and len(normalized) > 2:
                self._allowed_domains.add(normalized[2:])
            else:
                self._allowed_emails.add(normalized)
        self._poll_interval = poll_interval
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._watch_proc: asyncio.subprocess.Process | None = None
        self._watch_cmd: list[str] = []
        self._watch_task: asyncio.Task | None = None
        self._sessions: dict[int, str] = {}  # chat_id -> latest message_id
        self._sender_names_map: dict[int, str] = {}
        self._sender_emails: dict[int, str] = {}
        self._subjects: dict[int, str] = {}

    def _sender_allowed(self, sender_email: str) -> bool:
        if self._allow_all_senders:
            return True
        email = sender_email.lower()
        if email in self._allowed_emails:
            return True
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        return bool(domain) and domain in self._allowed_domains

    def workspace_name(self, chat_id: int) -> str:
        sender = self._sender_emails.get(chat_id, "unknown").split("@")[0]
        short_hash = hex(chat_id)[-8:]
        return f"gmail/{sender}-{short_hash}"

    def sender_name(self, chat_id: int) -> str:
        return self._sender_names_map.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        subject = self._subjects.get(chat_id, "")
        sender = self._sender_emails.get(chat_id, "unknown")
        return f'email:thread subject="{subject}" from={sender}'

    def describe(self) -> dict[str, str]:
        if self._allow_all_senders:
            allowed = "*"
        else:
            parts: list[str] = sorted(self._allowed_emails)
            parts.extend(f"*@{d}" for d in sorted(self._allowed_domains))
            allowed = ",".join(parts) or "<none>"
        return {
            "gcp_project": self._gcp_project,
            "poll_interval_s": str(self._poll_interval),
            "allowed_senders": allowed,
        }

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message

        await self._sweep_unread()

        cmd = [
            "gws",
            "gmail",
            "+watch",
            "--project",
            self._gcp_project,
            "--label-ids",
            "INBOX",
            "--msg-format",
            "full",
            "--poll-interval",
            str(self._poll_interval),
            "--cleanup",
        ]
        self._watch_cmd = cmd
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def _sweep_unread(self) -> None:
        """Process existing unread emails at startup."""
        logger.info("Sweeping unread inbox...")
        msg_ids = await self._list_unread_ids()
        if not msg_ids:
            logger.info("No unread emails to process.")
            return
        logger.info("Found %d unread emails, fetching full messages...", len(msg_ids))
        messages = await asyncio.gather(
            *(self._fetch_message(mid) for mid in msg_ids),
            return_exceptions=True,
        )
        for msg_id, result in zip(msg_ids, messages):
            if isinstance(result, Exception):
                logger.exception("Failed to fetch message %s: %s", msg_id, result)
                continue
            if result:
                try:
                    await self._handle_message(result)
                except Exception:
                    logger.exception("Failed to process message %s", msg_id)

    async def _list_unread_ids(self) -> list[str]:
        """List unread inbox message IDs via gws CLI."""
        proc = await asyncio.create_subprocess_exec(
            "gws",
            "gmail",
            "users",
            "messages",
            "list",
            "--params",
            json.dumps({"userId": "me", "q": "is:unread in:inbox", "maxResults": 20}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Failed to list unread: %s", stderr.decode()[:500])
            return []
        data = json.loads(stdout)
        return [m["id"] for m in data.get("messages", [])]

    async def _fetch_message(self, msg_id: str) -> dict | None:
        """Fetch a single message in full format."""
        proc = await asyncio.create_subprocess_exec(
            "gws",
            "gmail",
            "users",
            "messages",
            "get",
            "--params",
            json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to fetch message %s: %s", msg_id, stderr.decode()[:500]
            )
            return None
        return json.loads(stdout)

    async def _watch_loop(self) -> None:
        """Run +watch with auto-restart and exponential backoff."""
        backoff = 1
        max_backoff = 300  # 5 minutes
        while True:
            logger.info("Starting: %s", " ".join(self._watch_cmd))
            self._watch_proc = await asyncio.create_subprocess_exec(
                *self._watch_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,  # 1MB line buffer for full Gmail messages
            )
            processed = await self._read_stream()
            if processed:
                backoff = 1
            else:
                backoff = min(backoff * 2, max_backoff)
            logger.warning("gws +watch exited, restarting in %ds...", backoff)
            await asyncio.sleep(backoff)

    async def _read_stream(self) -> int:
        """Read NDJSON from +watch. Returns count of messages processed."""
        if not self._watch_proc or not self._watch_proc.stdout:
            return 0
        processed = 0
        while True:
            line = await self._watch_proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
                await self._handle_message(msg)
                processed += 1
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from +watch: %s", line[:200])
            except Exception:
                logger.exception("Failed to handle Gmail message")

        rc = await self._watch_proc.wait()
        if rc != 0 and self._watch_proc.stderr:
            stderr = await self._watch_proc.stderr.read()
            logger.error("gws +watch exited %d: %s", rc, stderr.decode()[:500])
        return processed

    async def _handle_message(self, msg: dict) -> None:
        labels = msg.get("labelIds", [])
        if "SENT" in labels:
            logger.debug("Ignored own sent message %s", msg.get("id", ""))
            return

        headers = msg.get("payload", {}).get("headers", [])
        from_header = _extract_header(headers, "From")
        display_name, sender_email = _parse_sender(from_header)

        auto_submitted = _extract_header(headers, "Auto-Submitted")
        if auto_submitted and auto_submitted != "no":
            logger.debug("Ignored auto-generated email from %s", sender_email)
            return

        if not self._sender_allowed(sender_email):
            logger.debug("Ignored email from %s (not in allowlist)", sender_email)
            return

        thread_id = msg.get("threadId", "")
        message_id = msg.get("id", "")
        subject = _extract_header(headers, "Subject")
        body = _strip_quoted(_extract_plain_body(msg.get("payload", {})))

        if not body.strip():
            logger.info("Skipped empty email from %s", sender_email)
            return

        chat_id = _thread_key(thread_id)

        self._sessions[chat_id] = message_id
        self._sender_names_map[chat_id] = display_name
        self._sender_emails[chat_id] = sender_email
        self._subjects[chat_id] = subject

        if subject:
            text = f"[from: {display_name}] Subject: {subject}\n\n{body}"
        else:
            text = f"[from: {display_name}] {body}"

        logger.info(
            "gmail %s/%s from %s: %s", thread_id, message_id, sender_email, body[:80]
        )
        if self._on_message:
            await self._on_message(chat_id, text)

    async def send(self, chat_id: int, response: Response) -> None:
        message_id = self._sessions.get(chat_id)
        if not message_id:
            logger.error("No message_id for session %s, cannot reply", chat_id)
            return

        body = response.body
        stats, tools = footer_parts(response, "gmail")
        if stats:
            body = f"{body}\n\n---\n{stats}"
        if tools:
            body = f"{body}\n{tools}"

        cmd = [
            "gws",
            "gmail",
            "+reply",
            "--message-id",
            message_id,
            "--body",
            body,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("gws +reply failed: %s", stderr.decode()[:500])
        else:
            logger.info("Replied to message %s", message_id)

    async def send_typing(self, chat_id: int) -> None:
        pass  # no typing indicator in email

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
        if self._watch_proc:
            self._watch_proc.terminate()
            await self._watch_proc.wait()


def main() -> None:  # pragma: no cover
    import os

    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_gmail

    load_dotenv()
    gcp_project, allowed_senders = run_gmail()
    frontend = GmailFrontend(
        gcp_project=gcp_project,
        allowed_senders=allowed_senders,
        poll_interval=int(os.environ.get("GMAIL_POLL_INTERVAL", "5")),
    )
    asyncio.run(run(frontend, platform="gmail"))


if __name__ == "__main__":  # pragma: no cover
    main()
