"""Runtime tool-permission approvals: config, and what each backend can offer.

`approvals.py` already owns the general machinery for asking an operator a
question and remembering the answer. This module owns the *permission* case: the
settings that switch it on, and the fact that the three backends cannot offer the
same thing.

**The asymmetry is the whole design constraint.** Measured, not assumed:

- claude native accepts `--permission-prompt-tool`, and calls it for exactly the
  calls it would otherwise have prompted a human about. cotf forwards the
  question and classifies nothing.
- claude under pty ignores that flag and draws its own terminal dialog instead,
  but announces it through a `Notification` hook, so the question is still
  claude's and still forwardable.
- `codex exec` never asks. Its `approval_policy` does nothing on its own, its
  `PermissionRequest` hook is observe-only and fires *after* the only hook that
  can block, and `approvals_reviewer=user` emits no event at all. So for codex
  there is no question to forward, and cotf has to decide what is worth
  interrupting an operator for. That is `worth_asking` below, and it is a
  convenience filter rather than a boundary -- see its docstring.

**Why `bypassPermissions` is refused rather than warned about.** Under it, claude
asks nothing, so the prompt tool is never called: measured at zero invocations.
An operator who sets `mode: ask` and leaves the CLI on bypass would get a daemon
that reports approvals as enabled and gates nothing. Same for `dontAsk`. A
config that quietly means the opposite of what it says is worse than a refusal.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from claude_on_the_fly import settings
from claude_on_the_fly.approvals import ApprovalBroker, ApprovalRequest

logger = logging.getLogger(__name__)

# `off` gates nothing and is byte-identical to the pre-feature behaviour.
MODES = ("off", "ask")

# --permission-mode values that are coherent with `ask`. `plan` is absent because
# its interaction with a gate has never been measured, and `bypassPermissions`
# and `dontAsk` are absent because both were measured at zero prompts, which
# would make the whole feature a no-op.
CLAUDE_MODES = ("default", "acceptEdits", "manual", "auto")

# Modes that silence the CLI entirely. Named separately from "not in
# CLAUDE_MODES" so the error can say *why* they are refused rather than just
# listing what is allowed.
SILENT_CLAUDE_MODES = ("bypassPermissions", "dontAsk")

DEFAULT_TTL_SECONDS = 1800.0
DEFAULT_TIMEOUT_SECONDS = 300.0


class ConfigError(ValueError):
    """The operator's `permissions:` block cannot be honoured as written."""


@dataclass(frozen=True)
class Permissions:
    """Resolved `permissions:` config.

    :param mode: "off" or "ask".
    :param claude_mode: --permission-mode for the claude backends under "ask".
    :param ttl_seconds: how long one approval lasts.
    :param timeout_seconds: how long the operator has to answer.
    """

    mode: str = "off"
    claude_mode: str = "default"
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return self.mode == "ask"


def _mode(value: object) -> str:
    """Normalise the `mode:` value, accepting YAML's boolean reading of `off`.

    An operator writing a bare `off` gets `False` from the YAML loader, and
    refusing that would be pedantry about quoting. `on`/`true` is refused rather
    than read as `ask`, because "enabled" is not one of the two names and
    guessing which one they meant is how a security feature ends up in a state
    nobody chose.
    """
    if value is False:
        return "off"
    if value is True:
        raise ConfigError(
            "`mode: on` is not a mode. Write `ask` to enable approvals, or "
            '`"off"` to disable them.'
        )
    if not isinstance(value, str):
        raise ConfigError(f"`mode` must be one of {list(MODES)}, got {value!r}")
    lowered = value.strip().lower()
    if lowered not in MODES:
        raise ConfigError(f"`mode: {value}` is not one of {list(MODES)}")
    return lowered


def _claude_mode(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"`claude_mode` must be one of {list(CLAUDE_MODES)}, got {value!r}"
        )
    stripped = value.strip()
    if stripped in SILENT_CLAUDE_MODES:
        raise ConfigError(
            f"`claude_mode: {stripped}` never prompts for anything, so pairing it "
            "with `mode: ask` would report approvals as on while gating nothing. "
            f'Use one of {list(CLAUDE_MODES)}, or set `mode: "off"`.'
        )
    if stripped not in CLAUDE_MODES:
        raise ConfigError(
            f"`claude_mode: {stripped}` is not recognised; expected one of "
            f"{list(CLAUDE_MODES)}"
        )
    return stripped


def _seconds(section: dict[str, object], key: str, fallback: float) -> float:
    value = section.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"`{key}` must be a number of seconds, got {value!r}")
    if value <= 0:
        raise ConfigError(f"`{key}` must be greater than zero, got {value!r}")
    return float(value)


def parse(section: dict[str, object]) -> Permissions:
    """Build a Permissions from one `permissions:` mapping. Raises ConfigError.

    Validation is strict on purpose: every field here either widens what the
    agent may do without asking, or decides whether anyone is asked at all.
    """
    mode = _mode(section.get("mode", "off"))
    claude_mode = _claude_mode(section.get("claude_mode", "default"))
    return Permissions(
        mode=mode,
        claude_mode=claude_mode,
        ttl_seconds=_seconds(section, "ttl_seconds", DEFAULT_TTL_SECONDS),
        timeout_seconds=_seconds(section, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )


def configured() -> Permissions:
    """Resolved permissions config: the operator's file over bundled defaults.

    Falls back to the bundled defaults (which are `off`) when the operator's
    block is unusable, and logs why. Failing *closed* here would mean a typo in a
    timeout could gate every tool call in a deployment that never asked for it,
    so the fallback direction is deliberately "behave as before".
    """
    merged = {**settings.bundled("permissions"), **settings.operator("permissions")}
    try:
        return parse(merged)
    except ConfigError as exc:
        logger.error(
            "permissions: ignoring the `permissions:` block of %s (%s); approvals "
            "are OFF and tool calls are not gated",
            settings.operator_settings(),
            exc,
        )
        return Permissions()


def check() -> None:
    """Report the resolved permission posture once, at startup.

    Separate from `configured()` because the loaders run at first use, which is a
    long way from the edit that broke them, and because an operator who switched
    approvals on deserves one line confirming it took effect.
    """
    resolved = configured()
    if not resolved.enabled:
        logger.info("permissions: approvals off; tool calls are not gated")
        return
    logger.warning(
        "permissions: approvals ON (claude_mode=%s, grant %.0fs, answer window "
        "%.0fs). Cron and the job queue stay ungated.",
        resolved.claude_mode,
        resolved.ttl_seconds,
        resolved.timeout_seconds,
    )
    if resolved.claude_mode == "auto":
        logger.warning(
            "permissions: claude_mode=auto delegates the decision to a model, not "
            "to you. Measured on sonnet 5 it approved sudo, a write into /etc, "
            "chmod -R 777, find -delete and ls ~/.ssh without asking once."
        )


# --------------------------------------------------------------------------
# Turning a tool call into a question
# --------------------------------------------------------------------------

# How much of a tool's own input an operator is shown. Tool input is entirely
# agent-authored -- unlike an egress CONNECT, where the proxy observed the host
# itself -- so this is attacker-shaped text by default and needs a bound. The cap
# is announced when it bites, because a silent truncation is how an operator ends
# up approving the half of a command they were not shown.
DETAIL_LIMIT = 400

# Characters that make a shell command more than one command. Any of them and the
# argv-prefix reasoning below stops holding, so the call is never granted by
# program name and is never auto-allowed. `$` alone is absent on purpose: `$(`
# is caught by the paren, while plain `$VAR` in `cat "$f"` is ordinary usage and
# excluding it would empty the allowlist of anything real.
_SHELL_OPERATORS = frozenset("|&;()<>`\n\r")

# Programs that only read. A grant for one of these is worth handing out because
# the tap it saves is real and the capability it confers is small. Deliberately
# short: every addition is a program an operator stops seeing.
_READ_ONLY_PROGRAMS = frozenset(
    {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "grep", "jq", "file", "stat"}
)

# git is two tools wearing one name. These subcommands report; the rest can
# rewrite history, move refs, or run a pager of the repo's choosing.
_READ_ONLY_GIT = frozenset({"status", "log", "diff", "show", "branch", "describe"})

# Tools whose input names a path to be written.
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "apply_patch"})

# Tools whose input names a URL.
_FETCH_TOOLS = frozenset({"WebFetch", "WebSearch"})


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation an agent wants to make.

    :param name: the tool name as the backend reports it, e.g. "Bash".
    :param input: the tool's own arguments. Agent-authored; never trusted.
    :param tool_use_id: the backend's id for this call, when it supplies one.
    """

    name: str
    input: dict[str, object] = field(default_factory=dict)
    tool_use_id: str = ""

    def text(self, key: str) -> str:
        """One input field as a string, or "" when absent or not a string."""
        value = self.input.get(key)
        return value if isinstance(value, str) else ""


def _flatten(text: str) -> str:
    """Collapse a tool input to one line of printable text.

    Newlines are structural in every frontend this reaches, so an agent that puts
    them in a command or a file body could otherwise draw extra lines into the
    operator's approval card -- a fake verdict line underneath the real one is the
    obvious use. Markdown escaping is the frontend's job and happens after this;
    flattening is this module's, because only here is the untrusted string still
    whole.
    """
    return " ".join(text.split())


def _capped(text: str) -> str:
    """`text` bounded to DETAIL_LIMIT, saying so when it had to cut."""
    if len(text) <= DETAIL_LIMIT:
        return text
    return f"{text[:DETAIL_LIMIT]}… [cut, {len(text)} chars total]"


def _segments(command: str) -> list[str] | None:
    """argv of a command that is exactly one simple command, else None.

    None means "do not reason about this by program name": either it contains
    shell control characters, or it does not lex at all. Both are cases where the
    first word stops predicting what runs.
    """
    if any(char in _SHELL_OPERATORS for char in command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv or None


def _bash_subject(command: str) -> str:
    """Grant scope for a shell command.

    A simple command is scoped to its program, so approving `git` once covers a
    turn of git work. Anything else is scoped to the exact string, because
    `ls && curl evil.example` must not be covered by a grant an operator gave to
    `ls`. That is the whole reason `_segments` returns None rather than the first
    word it can find.
    """
    argv = _segments(command)
    if argv is None:
        return f"bash-exact:{_flatten(command)}"
    program = Path(argv[0]).name
    if program == "git" and len(argv) > 1:
        return f"bash:git {argv[1]}"
    return f"bash:{program}"


def _write_subject(path_text: str, workspace: Path) -> str:
    """Grant scope for a write, distinguishing inside the workspace from outside.

    Escapes get the absolute path rather than one shared "outside" bucket. A
    single bucket would mean approving one write to ~/.zshrc also authorises the
    next write to ~/.ssh/config, which is not what the operator agreed to.
    """
    if not path_text:
        return "write:<no path>"
    target = Path(path_text)
    try:
        return f"write:{target.resolve().relative_to(workspace.resolve())}"
    except (ValueError, OSError):
        return f"write-outside:{target}"


def subject_for(call: ToolCall, workspace: Path) -> str:
    """The grant key for `call`: the precise scope an approval would cover.

    Derived here, from the payload the backend handed us, and never from anything
    the agent wrote *about* its own call. An agent under injection controls its
    tool input, so it can influence what it asks for; it must not be able to
    influence how narrowly the answer is remembered.
    """
    if call.name == "Bash":
        return _bash_subject(call.text("command"))
    if call.name in _WRITE_TOOLS:
        # apply_patch carries the patch in `command`; the claude tools use
        # `file_path`. Same question either way: what is being written, and where.
        if call.name == "apply_patch":
            return f"patch-exact:{_flatten(call.text('command'))[:120]}"
        return _write_subject(call.text("file_path"), workspace)
    if call.name in _FETCH_TOOLS:
        host = urlsplit(call.text("url")).hostname or "<no host>"
        return f"fetch:{host}"
    return f"tool:{call.name}"


def detail_for(call: ToolCall, *, asked_by: str) -> str:
    """Operator-facing description of `call`. Flattened and capped; not escaped.

    :param asked_by: who raised the question. "claude" when the CLI's own
        permission system did and cotf is only forwarding it, "cotf" when cotf
        decided this was worth interrupting for. The two mean different things
        about how much thought went into the question, so the operator sees which.
    """
    if call.name == "Bash":
        body = _flatten(call.text("command"))
    elif call.name in _WRITE_TOOLS:
        body = _flatten(call.text("file_path") or call.text("command"))
    elif call.name in _FETCH_TOOLS:
        body = _flatten(call.text("url"))
    else:
        body = _flatten(str(call.input)) if call.input else ""
    described = _flatten(call.text("description"))
    parts = [f"{asked_by} asked: {call.name}"]
    if body:
        parts.append(_capped(body))
    if described:
        parts.append(f"({_capped(described)})")
    return " ".join(parts)


def request_for(
    call: ToolCall,
    workspace: Path,
    *,
    asked_by: str,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> ApprovalRequest:
    """The ApprovalRequest an operator will be shown for `call`."""
    return ApprovalRequest(
        kind="tool",
        subject=subject_for(call, workspace),
        detail=detail_for(call, asked_by=asked_by),
        ttl_seconds=ttl_seconds,
    )


# --------------------------------------------------------------------------
# Deciding what is worth asking about (codex only)
# --------------------------------------------------------------------------


def worth_asking(call: ToolCall) -> bool:
    """True if `call` should reach the operator. Used for codex and nothing else.

    **This is a convenience filter, not a security boundary, and it must never be
    described as one.** It answers "would interrupting a human here be worth their
    attention", so that a turn of `ls` and `git status` does not cost twenty taps.
    It is defeated by anything that makes the first word stop predicting what
    runs, which is why `_segments` refuses compound commands outright rather than
    trying to parse them -- the same reason `sandbox.yaml` declines to police
    `gh api --method DELETE` by argv and says to scope the token instead.

    The boundary is elsewhere and unchanged: the seatbelt profile, the CONNECT
    proxy, and the credential broker. Every one of them holds whether this
    function is right or wrong.

    Unknown tools and unparseable commands return True. Costing an operator a tap
    is recoverable; not asking is not.
    """
    if call.name != "Bash":
        return True
    argv = _segments(call.text("command"))
    if argv is None:
        return True
    program = Path(argv[0]).name
    if program == "git":
        return not (len(argv) > 1 and argv[1] in _READ_ONLY_GIT)
    return program not in _READ_ONLY_PROGRAMS


# --------------------------------------------------------------------------
# The loopback service the backends ask
# --------------------------------------------------------------------------

# Where a decision request lands. One path, one verb: every backend asks the same
# question and the service decides who filtered it.
DECIDE_PATH = "/decide"

# Who raised the question, which decides whether cotf filters it.
#
#   "claude"  the CLI's own permission system did, and it only calls out for the
#             calls it would have prompted a human about. Forward all of them.
#   "cotf"    nothing asked; cotf is interposing. `worth_asking` filters first,
#             or a turn of `ls` and `git status` costs twenty taps.
SOURCE_CLAUDE = "claude"
SOURCE_COTF = "cotf"


@dataclass
class PermissionService:
    """Answers "may the agent make this tool call" over loopback, per session.

    Per session rather than daemon-wide for the same two reasons `SessionEgress`
    is: a grant must not leak between chats, and the prompt has to land in the
    conversation that caused it.

    Deliberately not an MCP server. MCP framing lives in the shim the backends
    actually spawn, because a hook cannot speak MCP at all and would have needed a
    second door anyway. One HTTP endpoint, two shim modes.
    """

    broker: ApprovalBroker
    workspace: Path
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    label: str = ""
    # Every decision this service was asked for, ever. Load-bearing rather than
    # telemetry: codex silently skips an untrusted hook and runs the command
    # anyway, so a turn that made tool calls and asked nothing is the signature of
    # a gate that is not attached. Nothing else can see that.
    requests_seen: int = 0
    _runner: object = field(default=None, repr=False)
    _port: int | None = field(default=None, repr=False)

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("permission service not started")
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def decide(self, call: ToolCall, source: str) -> tuple[bool, str]:
        """(allowed, message). The message is only meaningful on a denial.

        A `cotf`-sourced call that `worth_asking` declines is allowed without
        anyone being asked, and says so in the message so the log distinguishes
        "nobody minded" from "the operator approved it".
        """
        self.requests_seen += 1
        if source == SOURCE_COTF and not worth_asking(call):
            logger.debug(
                "permissions[%s]: %s not worth asking about", self.label, call.name
            )
            return True, "below the ask threshold"
        request = request_for(
            call, self.workspace, asked_by=source, ttl_seconds=self.ttl_seconds
        )
        granted = await self.broker.check(request)
        if granted:
            return True, "approved"
        # Deliberately not "denied by policy": the agent cannot tell the difference
        # between a refusal, a timeout and a rate limit, and guessing which one
        # would put words in the operator's mouth.
        return False, (
            "The operator did not approve this. Do not retry it. Say what you "
            "would need instead, or continue with the rest of the task."
        )

    async def _handle(self, request: object) -> object:
        from aiohttp import web

        assert isinstance(request, web.Request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        raw_input = body.get("input")
        call = ToolCall(
            name=str(body.get("tool_name") or ""),
            input=raw_input if isinstance(raw_input, dict) else {},
            tool_use_id=str(body.get("tool_use_id") or ""),
        )
        source = SOURCE_CLAUDE if body.get("source") == SOURCE_CLAUDE else SOURCE_COTF
        if not call.name:
            # Fail closed: an unnamed call cannot be classified, and allowing it
            # would make a malformed request the way around the gate.
            return web.json_response({"behavior": "deny", "message": "no tool_name"})
        allowed, message = await self.decide(call, source)
        return web.json_response(
            {"behavior": "allow" if allowed else "deny", "message": message}
        )

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        from aiohttp import web

        app = web.Application()
        app.router.add_post(DECIDE_PATH, self._handle)  # ty: ignore[invalid-argument-type]
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._runner = runner
        self._port = runner.addresses[0][1]
        logger.info("permissions[%s]: deciding on %s:%d", self.label, host, self._port)
        return self._port

    async def stop(self) -> None:
        from aiohttp import web

        if isinstance(self._runner, web.AppRunner):
            await self._runner.cleanup()
        self._runner = None
        self._port = None


# --------------------------------------------------------------------------
# Wiring a backend up to the service
# --------------------------------------------------------------------------

# Reserved shim name, matching commands.RESERVED_SHIM_NAMES. It shares the command
# broker's shim directory because fs-deny-most.sb re-grants reads there and nowhere
# else under DATA_DIR, so it is the only place a sandboxed agent can exec a
# generated helper from.
SHIM_NAME = "cotf-approve"

# What claude is told to call. MCP namespaces a tool as mcp__<server>__<tool>, and
# claude validates the name at startup and refuses to run if it does not resolve,
# which is the failure mode we want: loud, before any tokens are spent.
MCP_SERVER_NAME = "cotf"
PROMPT_TOOL = f"mcp__{MCP_SERVER_NAME}__approve"

_SHIM_SOURCE = '''#!{interpreter}
"""Generated by claude_on_the_fly.permissions. Do not edit; rewritten at startup."""
import sys

from claude_on_the_fly.cotf_approve import main

raise SystemExit(main())
'''


def shim_path() -> Path:
    from claude_on_the_fly import sandbox

    return sandbox.shim_dir() / SHIM_NAME


def write_shim() -> Path:
    """(Re)generate the approval shim and return its path.

    Generated rather than committed for the same reasons the command shims are:
    the interpreter is resolved at runtime, and no exec bit has to survive a wheel
    build. DATA_DIR is not in the sandbox's write allowlist, so the agent can read
    and exec this but not rewrite it.
    """
    import stat
    import sys

    path = shim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SHIM_SOURCE.format(interpreter=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


def mcp_config_path() -> Path:
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / "approve-mcp.json"


def write_mcp_config() -> Path:
    """The --mcp-config file naming the shim as an MCP server.

    The endpoint is not written in here. It arrives as COTF_APPROVE_URL in the
    spawn environment, which is per session, and baking a port into a file shared
    by every session would point one chat's approvals at another chat's grant
    store.
    """
    import json

    path = mcp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    MCP_SERVER_NAME: {
                        "command": str(shim_path()),
                        "args": ["mcp"],
                    }
                }
            }
        )
    )
    return path


def claude_argv(resolved: Permissions | None = None, *, pty: bool = False) -> list[str]:
    """The claude flags that switch tool permissions on, or none when off.

    Returns the permission mode too, not just the prompt tool, because the two are
    only correct together: under bypassPermissions claude asks nothing and the
    prompt tool is never called, which is why `claude_mode` refuses that value.

    **pty stays ungated, deliberately.** Interactive claude accepts
    --permission-prompt-tool, resolves it, connects the server, and then never
    calls it: it draws its own terminal dialog instead. Verified by running one to
    the timeout. Nobody is attached to that pane, so putting pty into an asking
    mode would hang every turn that touched a gated tool rather than gating it. The
    relay that fixes this needs a Notification hook inside claude-pty, which is a
    different repository. Until then pty keeps the old flags and says so, because
    an operator who enabled approvals must not be left assuming pty honours them.
    """
    resolved = configured() if resolved is None else resolved
    if resolved.enabled and pty:
        logger.warning(
            "permissions: approvals are ON but claude-pty cannot forward them; "
            "interactive claude ignores --permission-prompt-tool and draws its own "
            "dialog, which would hang an unattended pane. This pty session runs "
            "UNGATED. Use the native backend for gated turns."
        )
        return ["--permission-mode", "bypassPermissions"]
    if not resolved.enabled:
        return ["--permission-mode", "bypassPermissions"]
    return [
        "--permission-mode",
        resolved.claude_mode,
        "--mcp-config",
        str(mcp_config_path()),
        "--permission-prompt-tool",
        PROMPT_TOOL,
    ]


def codex_argv(resolved: Permissions | None = None) -> list[str]:
    """The codex flags that switch tool permissions on, or none when off.

    Two flags that have to travel together. The hook is injected via `-c` because
    that merges with the operator's own ~/.codex/hooks.json rather than replacing
    it, and the trust bypass is required because codex persists no trust entry for
    an inline hook -- without it the hook is silently skipped and the command runs
    anyway, which is the one failure this whole feature exists to prevent.

    The matching seatbelt denies on ~/.codex/{hooks.json,config.toml,rules} are
    what make the bypass safe; without them an agent could write itself a hook that
    codex would then run unreviewed.
    """
    resolved = configured() if resolved is None else resolved
    if not resolved.enabled:
        return []
    hook = (
        'hooks.PreToolUse=[{hooks=[{type="command",'
        f'command="{shim_path()} hook",'
        f"timeout={int(resolved.timeout_seconds)}}}]}}]"
    )
    return ["--dangerously-bypass-hook-trust", "-c", hook]


def warn_if_ungated(tool_calls: int, requests_seen: int, *, backend: str) -> bool:
    """True when a turn used tools but never reached the gate. Logs when it does.

    This is the substitute for a startup self-test, and a better one. codex treats
    an untrusted or crashed PreToolUse hook as no opinion and runs the command, so
    the failure is silent by construction: the operator sees a normal turn and
    believes it was supervised. Probing at startup would cost a model call every
    boot and still only prove the hook worked *then*; counting per turn costs
    nothing and catches it whenever it breaks.

    Not raised, because the turn has already happened by the time this can be
    known. The only useful move left is to say so loudly.
    """
    if tool_calls <= 0 or requests_seen > 0:
        return False
    logger.error(
        "permissions: %s made %d tool call(s) this turn and the approval gate was "
        "never asked. Approvals are enabled, so this turn ran UNSUPERVISED. Check "
        "that the hook or prompt tool is still wired for this backend.",
        backend,
        tool_calls,
    )
    return True
