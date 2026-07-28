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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import yaml

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
MEMORY_DIR = DATA_DIR / "memory"
MEMORY_ROOT = str(MEMORY_DIR)
KNOWLEDGE_DIR = str(MEMORY_DIR / "knowledge")
PROMPT_TEMPLATE = (Path(__file__).parent / "system_prompt.md").read_text()

# Frontends that can upload files back to the user. Single source of truth for
# both the outbox prompt instruction and the orchestrator scan/archive.
ATTACHMENT_PLATFORMS = frozenset({"slack", "telegram"})
# Frontends whose fresh sessions must NOT inherit the prior fire's transcript.
# The scheduler mints a new session UUID per fire on purpose — each cron run is
# independent — so the cross-backend handoff preamble would drag the last run's
# conversation into a run that's meant to start clean. Background jobs are the
# same shape: each job is an independent one-shot in a fresh workspace/session.
NO_HANDOFF_PLATFORMS = frozenset({"schedule", "jobs"})
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


def collect_outbox(workspace: Path) -> list[Path]:
    """Files the agent left in workspace/outbox to attach to the reply.

    Only regular files directly under outbox/ — skips the archive dir, subdirs,
    and dotfiles. Sorted by name for a deterministic order. Enforces count/size
    caps and logs every skip; nothing is dropped silently.
    """
    outbox = workspace / OUTBOX_DIRNAME
    if not outbox.is_dir():
        return []
    files = sorted(
        p for p in outbox.iterdir() if p.is_file() and not p.name.startswith(".")
    )
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
            size = path.stat().st_size
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


def ensure_persona(workspace: Path) -> None:
    """Symlink the global CLAUDE.md persona into the workspace under every
    name an agent CLI might read (CLAUDE.md for claude, AGENTS.md for codex).

    Idempotent: no-op when source is missing, no-op when the link is already
    correct, replaces wrong symlinks or pre-existing files.
    """
    source = DATA_DIR / "CLAUDE.md"
    if not source.is_file():
        return
    for filename in PERSONA_FILENAMES:
        _link_persona(source, workspace / filename)


STATS_MODES = ("off", "summary", "detailed")


def stats_mode(platform: str) -> str:
    """Read the reply-footer mode for a given frontend from its env var.

    Returns one of STATS_MODES. Defaults to "summary" for unknown or unset.
    Platform "telegram" reads TELEGRAM_STATS_MODE, and so on.
    """
    env_name = f"{platform.upper()}_STATS_MODE"
    mode = os.environ.get(env_name, "summary").lower()
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
    "symphony": (
        "Output goes to daemon logs, not a chat user. Plain markdown is fine. "
        "Tracker writes (status transitions, comments, label edits) are your "
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
    return PROMPT_TEMPLATE.format(
        format_hint=FORMAT_HINTS.get(platform, FORMAT_HINTS["telegram"]),
        outbox_instruction=outbox,
        user_name=user_name,
        channel_context=channel_context,
        workspace=str(workspace) if workspace is not None else "(current directory)",
        memory_root=MEMORY_ROOT,
        knowledge_dir=KNOWLEDGE_DIR,
    )


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
    # Optional statusline-derived fields, only populated in CLAUDE_MODE=pty.
    rate_limits_5h_pct: int | None = None
    rate_limits_5h_resets_at: int | None = None
    rate_limits_7d_pct: int | None = None
    rate_limits_7d_resets_at: int | None = None
    context_window_pct: int | None = None
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
        if self.context_window_pct is not None:
            parts.append(f"ctx {self.context_window_pct}%")
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
    msg: dict, tool_counts: dict[str, int], skill_counts: dict[str, int]
) -> dict | None:
    """Apply one parsed stream-json message to running tallies.

    Returns the message dict if it is a `type: "result"` line, else None.
    Mutates tool_counts and skill_counts in place.
    """
    msg_type = msg.get("type")
    if msg_type == "assistant":
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1
            if name == "Skill":
                skill = block.get("input", {}).get("skill")
                if skill:
                    skill_counts[skill] = skill_counts.get(skill, 0) + 1
    elif msg_type == "result":
        return dict(msg)
    return None


def parse_stream(stdout: bytes) -> dict:
    """Batch parser for stream-json NDJSON output from `claude -p`.

    Used by tests and smoke scripts. Runtime path uses _exec which streams
    line-by-line to avoid buffering the full output in memory.
    """
    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
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
        r = _fold(msg, tool_counts, skill_counts)
        if r is not None:
            result = r
    if result:
        result["tool_counts"] = tool_counts
        result["skill_counts"] = skill_counts
    return result


DEFAULT_TIMEOUT = 3600.0


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


async def _consume(proc: asyncio.subprocess.Process) -> dict:
    """Stream stdout, fold into result, validate returncode. Caller owns proc lifecycle."""
    assert proc.stdout is not None and proc.stderr is not None

    # Drain stderr concurrently so the subprocess can't block on a full pipe.
    stderr_task = asyncio.create_task(proc.stderr.read())

    tool_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    result: dict = {}
    line_count = 0
    try:
        async for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            line_count += 1
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("exec: skipping malformed line: %s", line[:120])
                continue
            r = _fold(msg, tool_counts, skill_counts)
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
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # getpgid/killpg can race with a natural exit; fall back to a plain kill.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
    finally:
        _announce_process(proc, "", running=False)


async def _exec(workspace: Path, cmd: list[str], timeout: float | None = None) -> dict:
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
    ) -> Response: ...

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """Return the interactive resume command for an existing session, or None.

        Returns the bare CLI invocation (e.g. `claude --resume <uuid>`); callers
        compose the full `cd <workspace> && <cmd>` one-liner. None signals that
        no session has been created yet for this workspace + uuid.
        """
        ...

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """Return the live JSONL path appended to as the session runs, or None.

        Used by `claude-symphony watch` to tail per-turn events. None signals
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
# cache and probe every query. Overridable via env.
SKILLS_CACHE_TTL = float(os.environ.get("SKILLS_CACHE_TTL_SECONDS", "3600"))
_skills_mem: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_skills_cache_lock = asyncio.Lock()


async def cached_skills(
    backend: AgentBackend, *, force: bool = False
) -> list[tuple[str, str]]:
    """Return `backend.list_skills()` behind a TTL cache shared by all queries.

    Two layers, both TTL-governed by SKILLS_CACHE_TTL: an in-memory entry (so
    per-keystroke picker queries don't touch disk) and a JSON file under
    DATA_DIR/cache (so a picker opened before startup warm finishes is still
    instant rather than paying the cold CLI probe). A single lock collapses
    concurrent misses into one recompute.

    `force=True` skips both cache layers and re-probes, then overwrites them.
    Startup warm uses it so a daemon restart always picks up newly
    installed/updated skills (the cache alone would otherwise mask changes for
    up to the TTL, even across restarts).
    """
    if SKILLS_CACHE_TTL <= 0:  # cache disabled: probe every query
        return await backend.list_skills()
    key = os.environ.get("AGENT_BACKEND", "claude").lower()
    path = DATA_DIR / "cache" / f"skills-{key}.json"
    now = time.time()
    if not force:
        entry = _skills_mem.get(key)
        if entry and now - entry[0] < SKILLS_CACHE_TTL:
            return entry[1]
    async with _skills_cache_lock:
        if not force:
            entry = _skills_mem.get(key)
            if entry and time.time() - entry[0] < SKILLS_CACHE_TTL:
                return entry[1]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stamp = float(data["cached_at"])
                if time.time() - stamp < SKILLS_CACHE_TTL:
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
    """Pick a backend from env vars. Raises ValueError on misconfiguration."""
    name = os.environ.get("AGENT_BACKEND", "claude").lower()
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
    name = os.environ.get("AGENT_BACKEND", "claude").lower()
    if name == "claude":
        mode = os.environ.get("CLAUDE_MODE", "native").lower()
        if mode == "native":
            return f"claude:native:{os.environ.get('CLAUDE_MODEL', '').strip()}"
        if mode == "ollama":
            model = os.environ.get("OLLAMA_MODEL", "").strip()
            if not model:
                raise ValueError("CLAUDE_MODE=ollama requires OLLAMA_MODEL to be set")
            return f"claude:ollama:{model}"
        if mode == "pty":
            return f"claude:pty:{os.environ.get('CLAUDE_MODEL', '').strip()}"
        raise ValueError(
            f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, pty)"
        )
    if name == "codex":
        mode = os.environ.get("CODEX_MODE", "native").lower()
        if mode == "native":
            return (
                f"codex:native:{os.environ.get('CODEX_MODEL', '').strip() or 'default'}"
            )
        if mode == "ollama":
            model = os.environ.get("OLLAMA_MODEL", "").strip()
            if not model:
                raise ValueError("CODEX_MODE=ollama requires OLLAMA_MODEL to be set")
            return f"codex:ollama:{model}"
        raise ValueError(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")
    raise ValueError(f"Unknown AGENT_BACKEND: {name!r} (supported: claude, codex)")


def _build_claude_backend() -> AgentBackend:
    from claude_on_the_fly.backends.claude import ClaudeBackend

    mode = os.environ.get("CLAUDE_MODE", "native").lower()
    if mode == "native":
        return ClaudeBackend()
    if mode == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "").strip()
        if not model:
            raise ValueError("CLAUDE_MODE=ollama requires OLLAMA_MODEL to be set")
        return ClaudeBackend(launcher=OllamaLauncher(model=model))
    if mode == "pty":
        return ClaudeBackend(pty=True)
    raise ValueError(f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, pty)")


def _build_codex_backend() -> AgentBackend:
    from claude_on_the_fly.backends.codex import CodexBackend

    mode = os.environ.get("CODEX_MODE", "native").lower()
    if mode == "native":
        return CodexBackend()
    if mode == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "").strip()
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
) -> Response:
    return await get_backend().run(
        workspace,
        session_uuid,
        prompt,
        platform,
        user_name=user_name,
        channel_context=channel_context,
        timeout=timeout,
    )
