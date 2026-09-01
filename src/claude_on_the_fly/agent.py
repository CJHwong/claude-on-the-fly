"""Agent dispatch and shared helpers.

Public surface: `Response`, `AgentBackend` protocol, `OllamaLauncher`,
`get_backend()` factory, `run()` facade, and prompt/format helpers used by
frontends. Claude-CLI specifics live in `claude_on_the_fly.backends.claude`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

import yaml

from claude_on_the_fly import sandbox, settings

logger = logging.getLogger(__name__)


def data_dir_from(env: Mapping[str, str]) -> Path:
    """The per-daemon data directory, from `COTF_DATA_DIR` when set.

    One daemon per directory: config.yaml, .env, cron.yaml, logs, state, and
    workspaces all hang off this path, so a second daemon on the same machine
    gets its own everything -- including its own heartbeat and pid files, which
    is what stops it fighting the first daemon for the same sockets. An empty
    value falls back to the default, like an absent one.

    It has to be a real environment variable rather than a `config.yaml` or
    `.env` setting: both files live inside this directory, so a file in the
    directory cannot point at the directory. `load_dotenv()` cannot help either
    -- DATA_DIR is resolved at import, before any `.env` is read.
    """
    return Path(env.get("COTF_DATA_DIR") or Path.home() / ".claude-on-the-fly")


DATA_DIR = data_dir_from(os.environ)


# Everything outside this set collapses to one `_`, the same shape
# `jobs.keys.safe_segment` uses, with the dot kept: Slack usernames contain dots
# and a workspace that moves loses the thread's files and its session history.
_WORKSPACE_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# `.` and `..` are the only names the filesystem reads as navigation, so they are
# the only dot forms that have to go. `..foo` is an ordinary filename.
_WORKSPACE_DOTS_ONLY = re.compile(r"\A\.+\Z")


def _workspace_segment(part: str) -> str:
    """One path component of a workspace name, reduced so it cannot navigate."""
    cleaned = _WORKSPACE_UNSAFE.sub("_", part)
    return "_" if _WORKSPACE_DOTS_ONLY.match(cleaned) else cleaned


def workspace_path(name: str, data_dir: Path) -> Path:
    """`data_dir/workspaces/<name>`, with every component of `name` reduced.

    A frontend names a workspace after the conversation, and part of that name
    is a display string somebody else chose: on Slack a trusted bot's per-message
    `username` reaches it verbatim, and Slack does not constrain that field. So
    the value is untrusted even though the frontend supplying it is not.

    The path matters more than a name usually does. It becomes `_PROJECT_DIR`,
    which both jails grant writes to, and the directory above it holds
    `cron.yaml`, whose entries the cron producer runs through a shell with the
    daemon's full environment and no jail. A single `..` would therefore turn a
    workspace name into host code execution outside the sandbox, which is why
    this is not left to the frontends to remember.

    Each component is reduced to `[A-Za-z0-9._-]`, and a component made only of
    dots becomes `_`. That second rule is the whole traversal defence: `.` and
    `..` are the only names the filesystem reads as navigation, and every other
    use of a dot is an ordinary character in an ordinary filename. The containment
    check afterwards is not redundant with it: it asserts this function's own
    arithmetic held, and it refuses rather than returning a path outside the tree.
    A symlink already standing where the workspace goes fails it too, since
    `resolve()` follows one and the target is then outside.

    The dot is kept rather than reduced with everything else because Slack
    usernames contain them. Measured against a real deployment: of 167 workspaces
    on disk, `keys.safe_segment` would have moved 8, all of the shape
    `dm-first.last-<ts>`. A moved workspace is not cosmetic. The directory holds
    the thread's files, its outbox and its persona link, and claude derives its
    session hash from the working directory, so the conversation would have lost
    its history as well. `keys.safe_segment` is left alone: job keys have no such
    names and want the tighter set.

    `data_dir` is a parameter rather than this module's `DATA_DIR` because each
    caller already holds the one its own module imported, and a frontend under
    test redirects that. A hidden read of the global here would quietly ignore the
    redirection and write into the operator's real data directory.
    """
    root = data_dir / "workspaces"
    parts = [_workspace_segment(part) for part in re.split(r"[\\/]+", name) if part]
    if not parts:
        raise ValueError(f"workspace name is empty after reduction: {name!r}")
    candidate = root.joinpath(*parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"workspace name escapes the tree: {name!r}") from exc
    return candidate


MEMORY_DIR = DATA_DIR / "memory"
MEMORY_ROOT = str(MEMORY_DIR)
KNOWLEDGE_DIR = str(MEMORY_DIR / "knowledge")
PROMPT_TEMPLATE = (Path(__file__).parent / "system_prompt.md").read_text()

# Frontends that can upload files back to the user. Single source of truth for
# both the outbox prompt instruction and the orchestrator scan/archive.
ATTACHMENT_PLATFORMS = frozenset({"slack", "telegram"})
# Frontends whose fresh sessions must NOT inherit the prior fire's transcript.
# A background job is an independent one-shot in a fresh workspace and session, so
# the cross-backend handoff preamble would drag an unrelated conversation into it.
# `cron` is deliberately absent: a keyed cron job resumes its own earlier session
# on purpose, which is the whole reason it carries a session key.
NO_HANDOFF_PLATFORMS = frozenset({"jobs"})
OUTBOX_DIRNAME = "outbox"
OUTBOX_ARCHIVE = ".sent"
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
OUTBOX_INSTRUCTION = (
    "<IMPORTANT>\n"
    "You CAN send files to the user, not only text. To deliver a file (a report, "
    "image, export, generated document, screenshot, anything), write or copy it "
    "into this exact directory before you finish (create it if missing):\n"
    "  {outbox_dir}\n"
    "Use that absolute path, not a relative `outbox/` — your shell's working "
    "directory may differ. Everything left in that directory is uploaded to the "
    "user along with your reply. When the user asks for a file, deliver it this "
    "way. Do NOT tell the user you can only send text or cannot attach files, that "
    "is false. Keep scratch and working files out of it.\n"
    "If you have several files to deliver, zip them into a single archive and drop "
    "that in instead, so the user gets one file rather than a flood of separate "
    "uploads. A lone file goes in as-is.\n"
    "</IMPORTANT>"
)


# The per-turn suggestions block the orchestrator appends to chat prompts, and
# the code-fence pair that sometimes wraps it.
SUGGESTIONS_BLOCK_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL)
SUGGESTIONS_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n\s*```\s*")


def strip_suggestions_blocks(body: str) -> str:
    """The visible text of a reply: `body` with every <suggestions> block
    (and the code-fence pair that may wrap one) removed.

    The orchestrator uses this to split a reply into text and labels. The
    backends deliberately do NOT: a body that is only a block still reached
    the end of its instructions, so it is a completed turn that said nothing
    rather than a dead one, and it is not retried.
    """
    cleaned = SUGGESTIONS_BLOCK_RE.sub("", body)
    return SUGGESTIONS_FENCE_RE.sub("", cleaned).strip()


def sender_marker(sender_id: object, display_name: object = "") -> str:
    """Render a platform-authenticated sender marker for the model prompt.

    The immutable platform id is the only identity-bearing field. Display names
    are JSON-quoted informational text, so brackets, newlines, and lookalike
    markers cannot change the prompt grammar.
    """
    identity = re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(sender_id))
    display = json.dumps(str(display_name), ensure_ascii=True)
    return f"[from-id: {identity}] [display: {display}]"


# One rendered `sender_marker`, for stripping it back out. The display half is
# always `json.dumps` output, so it is a quoted string with escaped quotes and
# nothing else can end it -- which is what makes this safe to match non-greedily.
_SENDER_MARKER_RE = re.compile(
    r'\[from-id: [A-Za-z0-9_.:@-]*\] \[display: "(?:[^"\\]|\\.)*"\]\s*'
)


def strip_sender_markers(text: str) -> str:
    """`text` without the sender markers, for showing it to a person.

    The markers are prompt grammar for the model. Quoting a message back to the
    human who wrote it (a resume nudge) has to show what they typed, not the
    scaffolding wrapped around it.
    """
    return _SENDER_MARKER_RE.sub("", text).strip()


def collect_outbox(workspace: Path) -> list[Path]:
    """Files the agent left in workspace/outbox to attach to the reply.

    Only regular files directly under outbox/ — skips the archive dir, subdirs,
    and dotfiles. Sorted by name for a deterministic order. Enforces count/size
    caps and logs every skip; nothing is dropped silently.
    """
    outbox = workspace / OUTBOX_DIRNAME
    try:
        outbox_stat = outbox.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(outbox_stat.st_mode):
        return []
    files: list[Path] = []
    for path in sorted(outbox.iterdir()):
        if path.name.startswith("."):
            continue
        try:
            path_stat = path.lstat()
        except OSError as exc:
            logger.warning("outbox: cannot inspect %s, skipping: %s", path.name, exc)
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            logger.warning("outbox: refusing non-regular file %s", path.name)
            continue
        files.append(path)
    collected: list[Path] = []
    for index, path in enumerate(files):
        if len(collected) >= MAX_ATTACHMENTS:
            dropped = ", ".join(p.name for p in files[index:])
            logger.warning(
                "outbox: %d-file cap hit, not sending %d file(s): %s",
                MAX_ATTACHMENTS,
                len(files) - index,
                dropped,
            )
            break
        try:
            size = path.lstat().st_size
        except OSError as exc:
            logger.warning("outbox: cannot stat %s, skipping: %s", path.name, exc)
            continue
        if size > MAX_ATTACHMENT_BYTES:
            logger.warning(
                "outbox: skipping %s (%d bytes exceeds %d cap)",
                path.name,
                size,
                MAX_ATTACHMENT_BYTES,
            )
            continue
        collected.append(path)
    return collected


def read_attachment(path: Path) -> bytes:
    """Read one attachment without following the final path component.

    Attachments cross from the jailed agent into an unsandboxed frontend. The
    caller may have validated the path earlier, so the validation must happen
    again on the descriptor that supplies the bytes. This also keeps frontend
    tests and non-outbox callers on the same safe read path.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        path_stat = os.fstat(fd)
        if not stat.S_ISREG(path_stat.st_mode):
            raise OSError(f"attachment is not a regular file: {path}")
        if path_stat.st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, MAX_ATTACHMENT_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {path}"
                )
            chunks.append(chunk)
    finally:
        os.close(fd)


def write_attachment(path: Path, data: bytes) -> None:
    """Atomically install downloaded bytes without following the destination.

    Replacing a destination path is intentional: repeated uploads with the same
    filename keep their existing behavior. `os.replace` replaces a symlink itself,
    rather than opening its target, so an agent-created link cannot redirect the
    write outside the workspace.
    """
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def install_download(source: Path, destination: Path) -> None:
    """Move one downloader-created file into place without following links."""
    source_stat = source.lstat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise OSError(f"download is not a regular file: {source}")
    if source_stat.st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {source}")
    os.replace(source, destination)


def archive_outbox(workspace: Path, files: list[Path]) -> None:
    """Move handed-off outbox files into outbox/.sent/<timestamp>/.

    Keeps the files on disk (never deletes) so an upload failure isn't silent
    data loss, while emptying outbox/ so they don't re-send on the next turn.
    """
    if not files:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive = workspace / OUTBOX_DIRNAME / OUTBOX_ARCHIVE / stamp
    try:
        archive.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("outbox: cannot create archive dir %s: %s", archive, exc)
        return
    for path in files:
        try:
            shutil.move(str(path), str(archive / path.name))
        except OSError as exc:
            logger.warning("outbox: failed to archive %s: %s", path.name, exc)


def _link_persona(source: Path, target: Path) -> None:
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


# Filenames each agent CLI reads as project-level persona/instructions.
PERSONA_FILENAMES = ("CLAUDE.md", "AGENTS.md")


def _persona_path(platform: str, key: str, value: object) -> Path | None:
    """One `personas:` entry as a usable file, or None with an ERROR saying why.

    Every rejection falls through to the next candidate and ultimately to the
    global persona, because a channel left with no instructions at all is a worse
    failure than one running the default -- and both need a log line, since
    neither is visible from Slack.
    """
    if not isinstance(value, str) or not value:
        logger.error(
            "%s.personas: `%s` must be a path relative to %s, got %r; ignoring it, "
            "so this chat falls through to its next key",
            platform,
            key,
            DATA_DIR,
            value,
        )
        return None
    # Containment is checked against the resolved root because DATA_DIR itself may
    # be reached through a symlink. An absolute value lands outside DATA_DIR and is
    # rejected here too: this file decides what instructions the agent runs under,
    # so it points at the operator's own data directory or nowhere.
    root = DATA_DIR.resolve()
    path = (DATA_DIR / value).resolve()
    if not path.is_relative_to(root):
        logger.error(
            "%s.personas: `%s` -> %s escapes %s; ignoring it, so this chat falls "
            "through to its next key",
            platform,
            key,
            value,
            root,
        )
        return None
    if not path.is_file():
        logger.error(
            "%s.personas: `%s` -> %s does not exist; ignoring it, so this chat falls "
            "through to its next key",
            platform,
            key,
            path,
        )
        return None
    return path


# Table key every chat falls back to. Listed explicitly rather than inferred from a
# conventional filename: reading the table is then the whole story, so nobody has to
# know that a file somewhere else in DATA_DIR is silently in charge.
PERSONA_DEFAULT_KEY = "default"


def _persona_table(platform: str) -> dict[str, object]:
    """The `personas:` mapping from one frontend's section, or {} if unusable."""
    table = settings.operator(platform).get("personas")
    if table is None:
        return {}
    if not isinstance(table, dict):
        logger.error(
            "%s.personas: must be a mapping of chat id -> file, got %s; no chat gets "
            "a per-chat persona",
            platform,
            type(table).__name__,
        )
        return {}
    return cast("dict[str, object]", table)


def persona_for(platform: str, keys: tuple[str, ...]) -> Path | None:
    """The persona file for one chat, or None for the data-root CLAUDE.md.

    The `personas:` table is consulted under `keys`, then under `default`.

    `keys` are the chat's identifiers in priority order (for Slack: channel id,
    then channel name). The frontend supplies them rather than the section because
    only it knows which of its identifiers may decide a persona -- a channel's
    sender changes per message while its workspace is per thread, so keying a
    channel on the sender would flip the persona mid-conversation.

    Whatever is returned REPLACES the data-root CLAUDE.md for that chat; the two
    do not stack.
    """
    table = _persona_table(platform)
    for key in (*keys, PERSONA_DEFAULT_KEY):
        if key not in table:
            continue
        path = _persona_path(platform, key, table[key])
        if path is not None:
            return path
    return None


def ensure_persona(workspace: Path, source: Path | None = None) -> None:
    """Symlink a persona into the workspace under every name an agent CLI might
    read (CLAUDE.md for claude, AGENTS.md for codex).

    `source` defaults to the global CLAUDE.md; a frontend passes a per-chat file
    from `persona_for`. Idempotent: no-op when the link is already correct,
    replaces wrong symlinks or pre-existing files.

    With nothing to link, links this function created before are removed. A stale
    one would otherwise outlive the config entry that asked for it, leaving a chat
    on a persona no file in DATA_DIR still names.
    """
    source = source or DATA_DIR / "CLAUDE.md"
    if not source.is_file():
        _unlink_personas(workspace)
        return
    for filename in PERSONA_FILENAMES:
        _link_persona(source, workspace / filename)


def _unlink_personas(workspace: Path) -> None:
    """Remove persona symlinks pointing into DATA_DIR. A real file at either name
    is the agent's or the operator's own and is left alone."""
    root = DATA_DIR.resolve()
    for filename in PERSONA_FILENAMES:
        target = workspace / filename
        if target.is_symlink() and target.resolve().is_relative_to(root):
            target.unlink()


STATS_MODES = ("off", "summary", "detailed")


def stats_mode(platform: str) -> str:
    """Read the reply-footer mode for a given frontend from its own config key.

    Returns one of STATS_MODES. Defaults to "summary" for unknown or unset.
    Platform "telegram" reads TELEGRAM_STATS_MODE, and so on.
    """
    env_name = f"{platform.upper()}_STATS_MODE"
    mode = settings.get(env_name, "summary").lower()
    return mode if mode in STATS_MODES else "summary"


def footer_parts(response: Response, platform: str) -> tuple[str, str]:
    """Return (stats_line, tools_line) for a reply, gated by the platform's mode.

    Either value is "" when the mode suppresses that line.
    """
    mode = stats_mode(platform)
    stats = response.format_stats() if mode != "off" and response.has_stats else ""
    tools = response.format_tools() if mode == "detailed" and response.has_tools else ""
    return stats, tools


FORMAT_HINTS = {
    "telegram": (
        "Format responses using Telegram-compatible markdown: "
        "*bold*, _italic_, `inline code`, ```code blocks```, - for lists. "
        "Do NOT use # headings or --- dividers."
    ),
    "slack": (
        "Write normal Markdown (**bold**, # headings, tables, lists, "
        "```code```). It is converted to Slack formatting on delivery."
    ),
    "cron": (
        "Output goes to this entry's log file, not a chat user. Plain markdown is "
        "fine. Tracker writes (status transitions, comments, label edits) are your "
        "responsibility — see your prompt for the tools available."
    ),
    "jobs": (
        "Write normal Markdown (**bold**, # headings, tables, lists, "
        "```code```). Your reply is delivered into the chat thread that "
        "requested this job; it is converted to that platform's formatting on "
        "delivery."
    ),
}


def build_system_prompt(
    platform: str,
    user_name: str,
    channel_context: str = "dm",
    workspace: Path | None = None,
) -> str:
    if platform in ATTACHMENT_PLATFORMS and workspace is not None:
        outbox = OUTBOX_INSTRUCTION.format(outbox_dir=workspace / OUTBOX_DIRNAME)
    else:
        outbox = ""
    prompt = PROMPT_TEMPLATE.format(
        format_hint=FORMAT_HINTS.get(platform, FORMAT_HINTS["telegram"]),
        outbox_instruction=outbox,
        user_name=user_name,
        channel_context=channel_context,
        workspace=str(workspace) if workspace is not None else "(current directory)",
        memory_root=MEMORY_ROOT,
        knowledge_dir=KNOWLEDGE_DIR,
    )
    # Backend-agnostic sandbox note (empty unless COTF_SANDBOX is on).
    guidance = sandbox.agent_guidance(workspace)
    if guidance:
        prompt = f"{prompt}\n\n{guidance}"
    return prompt


@dataclass(frozen=True)
class Compaction:
    """Outcome of a compaction turn.

    `pre_tokens`/`post_tokens` come from the transcript's `compact_boundary`
    record, which counts the *conversation* — not the billed prompt. The prompt
    also carries the system prompt and tool schemas, tens of thousands of tokens
    that compaction cannot touch, so `saved_tokens` is the ceiling on what a
    later turn actually stops paying for, never the whole prompt.
    """

    ok: bool
    pre_tokens: int = 0
    post_tokens: int = 0
    duration: float = 0
    error: str = ""

    @property
    def saved_tokens(self) -> int:
        return max(0, self.pre_tokens - self.post_tokens)

    def summary(self) -> str:
        """One line for the user. Frontend-agnostic: no mrkdwn, no emoji."""
        if not self.ok:
            # The CLI's own refusal ("Not enough messages to compact.") is more
            # informative than anything we'd write over it.
            return (
                f"Couldn't compact: {self.error}"
                if self.error
                else "Nothing to compact."
            )
        if not self.pre_tokens:
            return "Compacted the conversation."
        counts = f"{self.pre_tokens:,} → {self.post_tokens:,} tokens"
        # Only claude records how long it took; codex publishes no compaction
        # duration, and "in 0s" reads as a suspiciously fast compaction rather
        # than as a missing figure.
        if not self.duration:
            return f"Compacted the conversation: {counts}."
        return f"Compacted the conversation: {counts} in {self.duration:.0f}s."


@dataclass
class Response:
    """Structured response from the agent."""

    body: str
    attachments: list[Path] = field(default_factory=list)
    cost: float = 0
    duration: float = 0
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    tool_counts: dict[str, int] = field(default_factory=dict)
    skill_counts: dict[str, int] = field(default_factory=dict)
    # Follow-up suggestions rendered as tappable buttons under the reply.
    # Empty means no buttons (agent emitted no <suggestions> block, or the
    # reply is a notice, not an agent turn).
    suggestions: list[str] = field(default_factory=list)
    # Set only on a compaction turn, so callers can tell one from a reply.
    compaction: Compaction | None = None
    # Optional statusline-derived fields, populated in CLAUDE_MODE=pty.
    rate_limits_5h_pct: int | None = None
    rate_limits_5h_resets_at: int | None = None
    rate_limits_7d_pct: int | None = None
    rate_limits_7d_resets_at: int | None = None
    context_window_pct: int | None = None
    # Prompt size and the model's window, in tokens. Native backends populate
    # these instead of context_window_pct; the auto-compact gate also needs the
    # absolute numbers, so carry them.
    context_tokens: int | None = None
    context_window_size: int | None = None
    exceeds_200k: bool = False
    fast_mode: bool = False

    @property
    def has_stats(self) -> bool:
        return bool(self.cost or self.model)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_counts)

    def format_stats(self) -> str:
        parts = []
        if self.cost:
            parts.append(f"${self.cost:.4f}")
        if self.duration:
            parts.append(f"{self.duration:.1f}s")
        if self.tokens_in or self.tokens_out:
            parts.append(f"↑{self.tokens_in} ↓{self.tokens_out}")
        context_window_pct = self.context_window_pct
        if (
            context_window_pct is None
            and self.context_tokens is not None
            and self.context_window_size
        ):
            context_window_pct = self.context_tokens * 100 // self.context_window_size
        if context_window_pct is not None:
            parts.append(f"ctx {context_window_pct}%")
        # Only surface the 5h budget when it's actually loud; the 7d window
        # rarely matters in chat. resets_at is a Unix timestamp from pty.
        if (
            self.rate_limits_5h_pct is not None
            and self.rate_limits_5h_pct >= 50
            and self.rate_limits_5h_resets_at is not None
        ):
            reset = datetime.fromtimestamp(self.rate_limits_5h_resets_at).strftime(
                "%H:%M"
            )
            parts.append(f"5h {self.rate_limits_5h_pct}% → {reset}")
        if self.model:
            parts.append(self.model)
        return " | ".join(parts)

    def format_tools(self) -> str:
        if not self.tool_counts:
            return ""
        total = sum(self.tool_counts.values())
        items = sorted(self.tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        breakdown = " ".join(f"{name}×{count}" for name, count in items)
        return f"🔧 {total} ({breakdown})"


def _fold(
    msg: dict,
    tool_counts: dict[str, int],
    skill_counts: dict[str, int],
    compact: dict | None = None,
    last_usage: dict | None = None,
    last_text: dict | None = None,
) -> dict | None:
    """Apply one parsed stream-json message to running tallies.

    Returns the message dict if it is a `type: "result"` line, else None.
    Mutates tool_counts, skill_counts, and (when given) compact and
    last_usage in place.

    When last_usage is given, it is replaced with each assistant message's
    `usage`, so the result line ends up carrying the *final* prompt size.
    The result envelope's own top-level `usage` is the sum across every API
    call in the turn, which overstates how full the context is the moment the
    turn ends (see `_native_context_fields`); the last assistant message is
    the reading that reflects the context a compaction would actually see.

    When last_text is given, it is replaced with each assistant message's
    text — but only when that text is real content, not a lone
    `<suggestions>` block. The final message of a turn is often just the
    block the prompt asked for, and a backend that needs the last thing the
    agent actually said (a block-only reply is a completed turn that chose
    not to restate its answer) can read it back off the result line.
    """
    msg_type = msg.get("type")
    if msg_type == "assistant":
        if last_usage is not None:
            usage = msg.get("message", {}).get("usage") or {}
            last_usage.clear()
            last_usage.update(usage)
        if last_text is not None:
            text = "".join(
                block.get("text") or ""
                for block in msg.get("message", {}).get("content", [])
                if block.get("type") == "text"
            ).strip()
            if text and strip_suggestions_blocks(text).strip():
                last_text["text"] = text
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1
            if name == "Skill":
                skill = block.get("input", {}).get("skill")
                if skill:
                    skill_counts[skill] = skill_counts.get(skill, 0) + 1
    elif (
        msg_type == "system" and msg.get("subtype") == "status" and compact is not None
    ):
        # A compaction bookends itself: `status: "compacting"` on the way in,
        # then a `compact_result` on the way out. This is the only in-band signal
        # that the turn compacted rather than answered — the result line reports
        # `subtype: "success"` with an empty `result` either way, which is
        # indistinguishable from a turn that produced nothing.
        if msg.get("status") == "compacting":
            compact["started"] = True
        outcome = msg.get("compact_result")
        if outcome is not None:
            compact["result"] = outcome
            if msg.get("compact_error"):
                compact["error"] = msg["compact_error"]
    elif msg_type == "result":
        result = dict(msg)
        if last_usage:
            result["last_assistant_usage"] = dict(last_usage)
        return result
    return None


def parse_stream(stdout: bytes) -> dict:
    """Batch parser for stream-json NDJSON output from `claude -p`.

    Used by tests and smoke scripts. Runtime path uses _exec which streams
    line-by-line to avoid buffering the full output in memory.
    """
    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    compact: dict = {}
    last_usage: dict = {}
    last_text: dict = {}
    result: dict = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("parse_stream: skipping malformed line: %s", line[:120])
            continue
        r = _fold(msg, tool_counts, skill_counts, compact, last_usage, last_text)
        if r is not None:
            result = r
    if result:
        result["tool_counts"] = tool_counts
        result["skill_counts"] = skill_counts
        result["compact"] = compact
        result["last_assistant_text"] = last_text.get("text", "")
    return result


DEFAULT_TIMEOUT = 3600.0
MAX_AGENT_OUTPUT_BYTES = 8 * 1024 * 1024


class AgentOutputLimitError(RuntimeError):
    """The CLI produced more output than a supervised turn may buffer."""


# Chunk size for the drain loop below. Any value works; this one keeps the
# syscall count sane on an 8 MB cap without holding a large slice per read.
_READ_CHUNK_BYTES = 64 * 1024


async def _read_to_eof_capped(stream: asyncio.StreamReader) -> bytes:
    """Read a stream to EOF, stopping early once the cap is passed.

    `StreamReader.read(n)` with a positive `n` returns as soon as *any* byte is
    buffered — it does not wait for `n` bytes and it does not wait for EOF. So a
    single `read(cap + 1)` collects only whatever the CLI happened to flush
    first, which for a backend that streams (codex emits JSONL events over the
    life of the turn) is the opening event and nothing else. Loop until EOF
    instead.

    Returns the bytes read rather than raising, because the two callers want
    different things from an over-cap stream: one reports it, one kills the
    process group. Reading one byte past the cap before stopping is what keeps
    their `len(...) > cap` test honest.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_AGENT_OUTPUT_BYTES:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


class ClaudeUnavailableError(RuntimeError):
    """Claude CLI reports the org/account cannot use the API at all (usage limit, allocation disabled).

    Distinct from per-message errors so callers can avoid retrying or noisy reporting.
    """


# Substrings (lowercased) that indicate the API is unusable, not a per-message failure.
_UNAVAILABLE_PATTERNS = ("usage limit", "usage allocation")


def _classify(message: str) -> RuntimeError:
    low = (message or "").lower()
    if any(p in low for p in _UNAVAILABLE_PATTERNS):
        return ClaudeUnavailableError(message)
    return RuntimeError(message)


# Where this turn's mid-turn narration goes, or None to forward nothing (the
# default, and what every caller outside a chat frontend gets). A ContextVar for
# the same reason as sandbox._SESSION_ENV: the stream loop is several frames
# below the orchestrator that knows which chat this is, and threading a callback
# through would change `agent.run`, the AgentBackend Protocol, both backends'
# `run`, `_exec`, `_exec_pty`, `_consume` and `_fold`. asyncio copies the context
# when a task is created, so a value set in Orchestrator._process reaches that
# turn's stream and no other. Sync by contract: it is called from inside the
# stdout read loop, so it must not await.
_PROGRESS_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "cotf_progress_sink", default=None
)


def set_progress_sink(
    sink: Callable[[str], None],
) -> Token[Callable[[str], None] | None]:
    """Forward this turn's mid-turn text blocks to `sink`. Reset with the token."""
    return _PROGRESS_SINK.set(sink)


def reset_progress_sink(token: Token[Callable[[str], None] | None]) -> None:
    _PROGRESS_SINK.reset(token)


def progress_sink() -> Callable[[str], None] | None:
    """Return the current turn's progress sink, if one was installed."""
    return _PROGRESS_SINK.get()


class InterimRelay:
    """The main agent's mid-turn text blocks, forwarded as the turn produces them.

    A turn's final answer is a text block too, and the frontend posts that itself
    from `Response.body` — so a block is only forwarded once something proves the
    turn continued past it, which is a `tool_use`. Whatever is still pending when
    the stream ends is the answer, and is dropped rather than posted twice under a
    progress marker.

    Measured (claude 2.1.220): every assistant message carries exactly ONE
    content block, so the normal shape is `text` in one message and the `tool_use`
    in the next, and "pending across messages" is the main path rather than the
    fallback. A message holding both is rare but must still come out in order,
    which is why the flush happens at the `tool_use` block itself and not after
    the loop: only text that PRECEDED the tool call is proven to be narration, and
    text positioned after it is still a candidate for the final answer.

    Sub-agent output is not in the stream to begin with -- `--forward-subagent-text`
    is off unless asked for, and `_native_base_argv` never asks. The
    `parent_tool_use_id` check is the belt to that braces: the field is present on
    every default-stream assistant line with the value None (measured), and it is
    the field the flag's own help text says subagent forwarding sets.

    Thinking is excluded by selecting `type == "text"`. That filter is
    load-bearing, not defensive: the main agent's own thinking blocks ARE in the
    default stream (measured), each as its own message, and they carry their
    payload under `thinking` -- as `transcript.py` and `tui/session_format.py`
    both already rely on.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending: list[str] = []

    def feed(self, msg: dict) -> None:
        if msg.get("type") != "assistant" or msg.get("parent_tool_use_id"):
            return
        for block in msg.get("message", {}).get("content", []):
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if text:
                    self._pending.append(text)
            elif kind == "tool_use":
                # Inside the loop, deliberately: this flushes what came BEFORE
                # this tool call. Flushing after the loop would also release text
                # that followed it, which is not yet proven to be narration.
                self._flush()

    def _flush(self) -> None:
        for text in self._pending:
            self._emit(text)
        self._pending.clear()


async def _consume(proc: asyncio.subprocess.Process) -> dict:
    """Stream stdout, fold into result, validate returncode. Caller owns proc lifecycle."""
    assert proc.stdout is not None and proc.stderr is not None

    # Drain stderr concurrently so the subprocess can't block on a full pipe.
    # It has to run to EOF for that to hold: a drain that stops after the first
    # chunk leaves the pipe to fill exactly as if nothing were reading it.
    stderr_task = asyncio.create_task(_read_to_eof_capped(proc.stderr))

    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    compact: dict = {}
    last_usage: dict = {}
    last_text: dict = {}
    result: dict = {}
    line_count = 0
    stdout_bytes = 0
    # Read once, at the top: the sink belongs to the turn, not to a line.
    sink = _PROGRESS_SINK.get()
    relay = InterimRelay(sink) if sink is not None else None
    try:
        async for raw in proc.stdout:
            stdout_bytes += len(raw)
            if stdout_bytes > MAX_AGENT_OUTPUT_BYTES:
                raise AgentOutputLimitError(
                    f"agent stdout exceeded {MAX_AGENT_OUTPUT_BYTES} bytes"
                )
            line = raw.strip()
            if not line:
                continue
            line_count += 1
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("exec: skipping malformed line: %s", line[:120])
                continue
            r = _fold(msg, tool_counts, skill_counts, compact, last_usage, last_text)
            if relay is not None:
                try:
                    relay.feed(msg)
                except Exception:
                    # Same rule as _announce_process: a sink that raises must not
                    # take the agent run down with it. The turn is worth more than
                    # the progress line.
                    logger.exception("agent: progress relay failed")
            if r is not None:
                result = r
    except BaseException:
        stderr_task.cancel()
        raise
    finally:
        try:
            stderr_bytes = await stderr_task
        except (asyncio.CancelledError, Exception):
            stderr_bytes = b""

    if len(stderr_bytes) > MAX_AGENT_OUTPUT_BYTES:
        raise AgentOutputLimitError(
            f"agent stderr exceeded {MAX_AGENT_OUTPUT_BYTES} bytes"
        )

    await proc.wait()
    logger.debug(
        "exec: returncode=%s lines=%d stderr=%d bytes",
        proc.returncode,
        line_count,
        len(stderr_bytes),
    )

    if result:
        result["tool_counts"] = tool_counts
        result["skill_counts"] = skill_counts
        result["compact"] = compact
        result["last_assistant_text"] = last_text.get("text", "")

    if proc.returncode != 0:
        err_stderr = stderr_bytes.decode().strip()
        logger.debug(
            "exec: failed: stderr=%s parsed_result=%s",
            err_stderr[:200],
            str(result.get("result", ""))[:200],
        )
        if result.get("result"):
            raise _classify(result["result"])
        raise _classify(err_stderr or f"Exit code {proc.returncode}")
    if result.get("is_error") or result.get("subtype", "").startswith("error"):
        raise _classify(result.get("result", "Unknown error"))
    return result


async def communicate_capped(proc) -> tuple[bytes, bytes]:
    """Collect both subprocess streams without allowing unbounded buffering."""
    stdout_stream = getattr(proc, "stdout", None)
    stderr_stream = getattr(proc, "stderr", None)
    if not isinstance(stdout_stream, asyncio.StreamReader) or not isinstance(
        stderr_stream, asyncio.StreamReader
    ):
        # Small test doubles and embedders sometimes expose only communicate().
        stdout, stderr = await proc.communicate()
        if len(stdout) > MAX_AGENT_OUTPUT_BYTES or len(stderr) > MAX_AGENT_OUTPUT_BYTES:
            await _kill_process_tree(proc)
            raise AgentOutputLimitError(
                f"agent output exceeded {MAX_AGENT_OUTPUT_BYTES} bytes"
            )
        return stdout, stderr

    stdout_task = asyncio.create_task(_read_to_eof_capped(stdout_stream))
    stderr_task = asyncio.create_task(_read_to_eof_capped(stderr_stream))
    tasks = {stdout_task, stderr_task}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if any(len(task.result()) > MAX_AGENT_OUTPUT_BYTES for task in done):
        await _kill_process_tree(proc)
        await asyncio.gather(*pending, return_exceptions=True)
        raise AgentOutputLimitError(
            f"agent output exceeded {MAX_AGENT_OUTPUT_BYTES} bytes"
        )
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    if len(stdout) > MAX_AGENT_OUTPUT_BYTES or len(stderr) > MAX_AGENT_OUTPUT_BYTES:
        await _kill_process_tree(proc)
        raise AgentOutputLimitError(
            f"agent output exceeded {MAX_AGENT_OUTPUT_BYTES} bytes"
        )
    return stdout, stderr


# Notified when an agent CLI's process group starts and when it has been
# reaped, as (pgid, command, running). Every spawn site announces; the single
# reap path below un-announces.
#
# The point is survivability, not bookkeeping: a listener can write the group
# down somewhere durable *before* anything can go wrong, so a host that is
# SIGKILLed mid-run leaves a record of what it orphaned. Nothing in-process can
# do that after the fact — the CLI leads its own session, so it is unreachable
# from the parent's group and its pid dies with the parent that knew it.
ProcessListener = Callable[[int, str, bool], None]
_process_listeners: list[ProcessListener] = []


def add_process_listener(listener: ProcessListener) -> None:
    """Register a listener for agent process-group start/stop."""
    _process_listeners.append(listener)


def remove_process_listener(listener: ProcessListener) -> None:
    """Unregister a listener. Silent if it was never registered."""
    if listener in _process_listeners:
        _process_listeners.remove(listener)


def _announce_process(
    proc: asyncio.subprocess.Process, command: str, *, running: bool
) -> None:
    """Tell every listener about an agent process group.

    The pgid is `proc.pid`, not `os.getpgid(proc.pid)`: with
    start_new_session=True the child calls setsid() and so leads a group whose
    id equals its own pid, which is knowable immediately and without racing the
    child's setsid. (`_kill_process_tree` still asks the OS, because by then the
    process may be gone and the answer matters more than the timing.)

    A listener that raises must not take the agent run down with it.
    """
    for listener in list(_process_listeners):
        try:
            listener(proc.pid, command, running)
        except Exception:
            logger.exception("agent: process listener failed")


def track_agent_process(proc: asyncio.subprocess.Process, cmd: list[str]) -> None:
    """Announce a freshly spawned agent CLI. Call immediately after spawning."""
    _announce_process(proc, cmd[0] if cmd else "", running=True)


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Reap a subprocess and every descendant it spawned.

    Agent CLIs are spawned with start_new_session=True, so the child leads its
    own process group; a SIGKILL to that group reaps the CLI *and* the tool
    subprocesses it forked. A bare proc.kill() only hits the direct child and
    orphans the rest. Safe to call on an already-exited process.

    The single un-announce point for `track_agent_process`, including the
    already-exited early return — a process that ended on its own is just as
    finished as one this reaped, and a listener that never heard so would keep
    treating it as live.
    """
    try:
        # Do not return merely because the leader already exited. A CLI can
        # naturally exit while a tool child it spawned is still alive in the
        # dedicated group; returning here is the orphaning bug this helper is
        # responsible for preventing.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # The leader may have been reaped and the group may already be gone;
            # a plain kill is still useful while it remains addressable.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
    finally:
        _announce_process(proc, "", running=False)


async def _exec(workspace: Path, cmd: list[str], timeout: float | None = None) -> dict:
    cmd = sandbox.wrap(cmd, workspace)
    logger.debug(
        "exec: cwd=%s cmd=%s timeout=%s",
        workspace,
        " ".join(cmd[:6]) + "...",
        timeout,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        limit=16 * 1024 * 1024,
        start_new_session=True,
        env=sandbox.agent_env(),
    )
    track_agent_process(proc, cmd)
    try:
        if timeout is not None:
            return await asyncio.wait_for(_consume(proc), timeout=timeout)
        return await _consume(proc)
    except TimeoutError:
        logger.warning("exec: timed out after %ss", timeout)
        raise RuntimeError(f"Claude CLI timed out after {timeout}s") from None
    finally:
        await _kill_process_tree(proc)


NUDGE_PROMPT = "Please provide your final reply to the user."


def _sum_counts(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


def _merge_cli_output(first: dict, second: dict) -> dict:
    """Combine two stream-json results for a retry: body from second, usage summed."""
    merged = dict(second)
    merged["total_cost_usd"] = first.get("total_cost_usd", 0) + second.get(
        "total_cost_usd", 0
    )
    merged["duration_ms"] = first.get("duration_ms", 0) + second.get("duration_ms", 0)

    fu = first.get("usage") or {}
    su = second.get("usage") or {}
    merged["usage"] = {
        "input_tokens": fu.get("input_tokens", 0) + su.get("input_tokens", 0),
        "cache_read_input_tokens": fu.get("cache_read_input_tokens", 0)
        + su.get("cache_read_input_tokens", 0),
        "output_tokens": fu.get("output_tokens", 0) + su.get("output_tokens", 0),
    }

    merged["modelUsage"] = {
        **(first.get("modelUsage") or {}),
        **(second.get("modelUsage") or {}),
    }
    merged["tool_counts"] = _sum_counts(
        first.get("tool_counts"), second.get("tool_counts")
    )
    merged["skill_counts"] = _sum_counts(
        first.get("skill_counts"), second.get("skill_counts")
    )
    # `last_assistant_usage` is deliberately NOT summed: `merged = dict(second)`
    # already keeps the retry's reading, which resumes the session and so is
    # more current than anything the first run produced. Summing the two would
    # over-read the context exactly like the top-level `usage` does.
    # `last_assistant_text` follows the same rule, but a retry that produced no
    # real text must not erase the first run's — the fallback is better than
    # nothing, and the retry's own body is what the merged result posts anyway.
    merged["last_assistant_text"] = second.get("last_assistant_text") or first.get(
        "last_assistant_text"
    )
    return merged


_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict:
    """Parse a leading YAML front-matter block into a dict (empty if none).

    Uses a real YAML parse so block scalars (`description: |`) and quoting are
    handled. Shared by backends that read skill/prompt metadata files.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


class AgentBackend(Protocol):
    """Minimal contract every backend implements."""

    async def run(
        self,
        workspace: Path,
        session_uuid: str,
        prompt: str,
        platform: str,
        user_name: str = "unknown",
        channel_context: str = "dm",
        timeout: float | None = DEFAULT_TIMEOUT,
        nudge_prompt: str | None = None,
    ) -> Response: ...

    async def compact(
        self,
        workspace: Path,
        session_uuid: str,
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Compaction | None:
        """Summarize the session's history in place, shrinking the next prompt.

        A separate method rather than a `run()` prompt because a compaction is
        not a turn: it produces no assistant message, so every "did the turn
        answer?" rule downstream reads it as a dead run. Returns None when the
        backend has no compaction of its own — callers must treat that as "not
        supported" and leave the session alone.
        """
        ...

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """Return the interactive resume command for an existing session, or None.

        Returns the bare CLI invocation (e.g. `claude --resume <uuid>`); callers
        compose the full `cd <workspace> && <cmd>` one-liner. None signals that
        no session has been created yet for this workspace + uuid.
        """
        ...

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """Return the live JSONL path appended to as the session runs, or None.

        Used to tail per-turn events. None signals
        either no session yet, or the backend doesn't expose a streamable log.
        """
        ...

    async def list_skills(self) -> list[tuple[str, str]]:
        """Return the backend's skills as (name, description) for the picker.

        Description is "" when unavailable, and the list is empty when the
        backend can't enumerate. Uncached and may spawn the CLI or read files;
        callers should go through `cached_skills()` for the TTL disk cache.
        """
        ...


@dataclass(frozen=True)
class OllamaLauncher:
    """Wraps an agent CLI invocation in `ollama launch <agent> --model <X> --yes --`."""

    model: str

    def prefix(self, agent_name: str) -> list[str]:
        return ["ollama", "launch", agent_name, "--model", self.model, "--yes", "--"]


# Skill list changes only when plugins/prompts change, and probing spawns the
# CLI (~0.8s), so cache it with a TTL (default 1h). Set <= 0 to disable the
# cache and probe every query.
DEFAULT_SKILLS_CACHE_TTL = 3600.0


def skills_cache_ttl() -> float:
    """The skill-cache TTL, read per call rather than bound at import.

    It used to be a module constant, which could not see a value `load_dotenv()`
    put in the environment after this module was imported, and cannot see a
    config-file edit at all. A junk value falls back to the default: this is a
    latency optimisation, not something worth refusing to start over.
    """
    raw = settings.get("SKILLS_CACHE_TTL_SECONDS").strip()
    if not raw:
        return DEFAULT_SKILLS_CACHE_TTL
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "SKILLS_CACHE_TTL_SECONDS=%r is not a number; using %.0fs",
            raw,
            DEFAULT_SKILLS_CACHE_TTL,
        )
        return DEFAULT_SKILLS_CACHE_TTL


_skills_mem: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_skills_cache_lock = asyncio.Lock()


async def cached_skills(
    backend: AgentBackend, *, force: bool = False
) -> list[tuple[str, str]]:
    """Return `backend.list_skills()` behind a TTL cache shared by all queries.

    Two layers, both TTL-governed by `skills_cache_ttl()`: an in-memory entry (so
    per-keystroke picker queries don't touch disk) and a JSON file under
    DATA_DIR/cache (so a picker opened before startup warm finishes is still
    instant rather than paying the cold CLI probe). A single lock collapses
    concurrent misses into one recompute.

    `force=True` skips both cache layers and re-probes, then overwrites them.
    Startup warm uses it so a daemon restart always picks up newly
    installed/updated skills (the cache alone would otherwise mask changes for
    up to the TTL, even across restarts).
    """
    ttl = skills_cache_ttl()
    if ttl <= 0:  # cache disabled: probe every query
        return await backend.list_skills()
    key = settings.get("AGENT_BACKEND", "claude").lower()
    path = DATA_DIR / "cache" / f"skills-{key}.json"
    now = time.time()
    if not force:
        entry = _skills_mem.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
    async with _skills_cache_lock:
        if not force:
            entry = _skills_mem.get(key)
            if entry and time.time() - entry[0] < ttl:
                return entry[1]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stamp = float(data["cached_at"])
                if time.time() - stamp < ttl:
                    skills = [(str(n), str(d)) for n, d in data["skills"]]
                    _skills_mem[key] = (stamp, skills)
                    return skills
            except (OSError, ValueError, KeyError, TypeError):
                pass
        skills = await backend.list_skills()
        stamp = time.time()
        _skills_mem[key] = (stamp, skills)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"cached_at": stamp, "skills": [list(s) for s in skills]}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("skills cache: write failed: %s", exc)
        return skills


def get_backend() -> AgentBackend:
    """Pick a backend from the `agent:` config. Raises ValueError on misconfiguration."""
    name = settings.get("AGENT_BACKEND", "claude").lower()
    if name == "claude":
        return _build_claude_backend()
    if name == "codex":
        return _build_codex_backend()
    raise ValueError(f"Unknown AGENT_BACKEND: {name!r} (supported: claude, codex)")


def resolve_session_log(workspace: Path, session_uuid: str) -> Path | None:
    """Locate a job's session JSONL across every backend, not just the current
    one.

    The process viewing the log (the TUI) isn't necessarily configured for the
    backend that ran the job: the daemon may run codex while the dashboard's
    shell is claude:native. Each backend stores logs in its own tree, and session
    UUIDs are seeded per backend, so a given (workspace, uuid) exists in exactly
    one store — the first hit is unambiguous. Each backend's session_log_path
    only depends on its store location, so the bare constructor is enough.

    Codex is tried last on purpose: claude resolves with a single path stat, but
    codex's no-mapping fallback scans the rollout tree, so we only pay for it
    when the cheap backend misses (a real codex session, or none yet).
    """
    from claude_on_the_fly.backends.claude import ClaudeBackend
    from claude_on_the_fly.backends.codex import CodexBackend

    for build in (ClaudeBackend, CodexBackend):
        try:
            path = build().session_log_path(workspace, session_uuid)
        except Exception:
            continue
        if path is not None:
            return path
    return None


def current_backend_key() -> str:
    """Canonical `backend:mode:model` string for the currently-configured agent.

    Folded into session UUID seeds so each (backend, mode, model) combo gets
    its own session JSONL. Switching between e.g. claude-native and
    claude-via-ollama no longer poisons the saved session — the new combo
    starts a fresh thread and picks up prior context via the cross-backend
    handoff path in `transcript`.

    Raises ValueError on the same misconfigurations as `get_backend()` so
    callers fail loudly instead of silently colliding sessions.
    """
    name = settings.get("AGENT_BACKEND", "claude").lower()
    if name == "claude":
        mode = settings.get("CLAUDE_MODE", "native").lower()
        if mode == "native":
            return f"claude:native:{settings.get('CLAUDE_MODEL').strip()}"
        if mode == "ollama":
            model = settings.get("OLLAMA_MODEL").strip()
            if not model:
                raise ValueError("CLAUDE_MODE=ollama requires OLLAMA_MODEL to be set")
            return f"claude:ollama:{model}"
        if mode == "pty":
            return f"claude:pty:{settings.get('CLAUDE_MODEL').strip()}"
        raise ValueError(
            f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, pty)"
        )
    if name == "codex":
        mode = settings.get("CODEX_MODE", "native").lower()
        if mode == "native":
            return f"codex:native:{settings.get('CODEX_MODEL').strip() or 'default'}"
        if mode == "ollama":
            model = settings.get("OLLAMA_MODEL").strip()
            if not model:
                raise ValueError("CODEX_MODE=ollama requires OLLAMA_MODEL to be set")
            return f"codex:ollama:{model}"
        raise ValueError(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")
    raise ValueError(f"Unknown AGENT_BACKEND: {name!r} (supported: claude, codex)")


def _ollama_context_window() -> int | None:
    """Resolve the operator-declared context window for ollama mode.

    None for unset or unusable values, which leaves ollama reporting no reading
    at all — the behaviour before this setting existed. A junk value disables
    the reading rather than taking the daemon down, and is logged so a typo does
    not look like a silently working setting.
    """
    raw = settings.get("OLLAMA_CONTEXT_WINDOW").strip()
    if not raw:
        return None
    try:
        window = int(raw)
    except ValueError:
        logger.warning(
            "OLLAMA_CONTEXT_WINDOW=%r is not a number, ollama reports no context",
            raw,
        )
        return None
    if window <= 0:
        logger.warning(
            "OLLAMA_CONTEXT_WINDOW=%d is not positive, ollama reports no context",
            window,
        )
        return None
    return window


def _build_claude_backend() -> AgentBackend:
    from claude_on_the_fly.backends.claude import ClaudeBackend

    mode = settings.get("CLAUDE_MODE", "native").lower()
    if mode == "native":
        return ClaudeBackend()
    if mode == "ollama":
        model = settings.get("OLLAMA_MODEL").strip()
        if not model:
            raise ValueError("CLAUDE_MODE=ollama requires OLLAMA_MODEL to be set")
        return ClaudeBackend(
            launcher=OllamaLauncher(model=model),
            ollama_context_window=_ollama_context_window(),
        )
    if mode == "pty":
        return ClaudeBackend(pty=True)
    raise ValueError(f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, pty)")


def _build_codex_backend() -> AgentBackend:
    from claude_on_the_fly.backends.codex import CodexBackend

    mode = settings.get("CODEX_MODE", "native").lower()
    if mode == "native":
        return CodexBackend()
    if mode == "ollama":
        model = settings.get("OLLAMA_MODEL").strip()
        if not model:
            raise ValueError("CODEX_MODE=ollama requires OLLAMA_MODEL to be set")
        return CodexBackend(launcher=OllamaLauncher(model=model))
    raise ValueError(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")


async def run(
    workspace: Path,
    session_uuid: str,
    prompt: str,
    platform: str,
    user_name: str = "unknown",
    channel_context: str = "dm",
    timeout: float | None = DEFAULT_TIMEOUT,
    nudge_prompt: str | None = None,
) -> Response:
    return await get_backend().run(
        workspace,
        session_uuid,
        prompt,
        platform,
        user_name=user_name,
        channel_context=channel_context,
        timeout=timeout,
        nudge_prompt=nudge_prompt,
    )


async def compact(
    workspace: Path,
    session_uuid: str,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> Compaction | None:
    """Compact a session's history. None when the backend doesn't support it."""
    backend = get_backend()
    compactor = getattr(backend, "compact", None)
    if compactor is None:
        return None
    return await compactor(workspace, session_uuid, timeout=timeout)
