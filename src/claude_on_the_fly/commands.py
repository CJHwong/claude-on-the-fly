"""Run credentialed CLIs outside the sandbox so the agent never holds the secret.

Env curation (sandbox.agent_env) strips secrets that live in the environment. It
does nothing for credentials that live on disk under `$HOME`, which is where
`gh`, `aws`, `kubectl`, `acli`, and friends keep theirs. The profile's answer was
a read denylist, and that failed twice in observable ways:

  1. Denying the credential breaks the tool, and the agent routes around it.
     Asked to list reviewed PRs, `gh` died on `~/.config/gh/hosts.yml`; the agent
     then reached the same private repos through the model provider's own GitHub
     integration, over an already-approved host, with a credential this project
     never holds. The egress log for that window recorded zero GitHub CONNECTs.
  2. The denylist is enumerate-the-bad on a moving target. Credential stores
     adopted after it was written were readable by default.

So: a shim inside the sandbox forwards the invocation here, this broker runs the
real binary with the real credential, and only the output crosses back. The
credential stays outside, every invocation is logged, and stopping the broker
revokes the capability.

**No action policy, deliberately.** This does not parse subcommands to
allow/deny/ask. `gh api --method DELETE /repos/o/r` defeats any subcommand
denylist, so a parser here would be a security boundary that can be walked
around, plus a second enumerate-the-bad list. The scope lever is the *token*: a
fine-grained PAT limited to what the agent needs bounds it far more reliably than
argv inspection. What a hijacked agent can do for the life of a session is the
accepted trade; what it cannot do is keep the credential afterwards.

The one thing that *is* refused is **credential readback** — a command whose
output is the secret itself. That is not policy, it is closing the door this
broker opens: forwarding `gh auth token` would place the token straight into the
sandbox and defeat the entire design.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from claude_on_the_fly import logs

logger = logging.getLogger(__name__)

# Streams are truncated at this size each. A command broker is not a file
# transfer; an agent that wants a big artifact should write it to the workspace.
MAX_STREAM_BYTES = 1 << 20

# Request bodies are small (argv plus a little stdin). Anything larger is a
# mistake or an attempt to wedge the broker.
_MAX_REQUEST_BYTES = 1 << 20

_RUN_TIMEOUT_SECONDS = 120.0

# How long a shim waits for piped stdin before deciding there is none. Long
# enough for a producer that is already writing, short enough to be invisible on
# the far commoner case of an inherited, idle pipe.
_STDIN_WAIT_SECONDS = 0.25

# Env var carrying the broker's endpoint to the sandbox. Ends up in the agent's
# environment via sandbox._PASSTHROUGH, and the generated shims read it.
ENDPOINT_ENV = "COTF_CMD_ENDPOINT"

_REFUSAL_TEXT = (
    "[sandbox] this command returns a credential, which is exactly what the "
    "command broker exists to keep out of the sandbox. The tool itself works "
    "normally; you do not need the token to use it. Do not look for the "
    "credential anywhere else."
)


@dataclass(frozen=True)
class ShimmedTool:
    """A CLI the agent invokes through the broker instead of directly.

    :param name: The command name shimmed onto PATH, e.g. "gh".
    :param readback: argv prefixes whose output *is* the credential, matched
        against the leading non-flag tokens. `("auth", "token")` refuses
        `gh auth token` and `gh auth token --hostname x`.
    :param readback_flags: flags that make any command print the secret.
    :param env_passthrough: extra parent env names the real binary needs beyond
        the shared essentials. Kept narrow so the subprocess does not inherit
        every secret the daemon happens to hold.
    """

    name: str
    readback: frozenset[tuple[str, ...]] = frozenset()
    readback_flags: frozenset[str] = frozenset()
    env_passthrough: frozenset[str] = frozenset()


# Vetted defaults, shipped in the package next to the seatbelt profiles.
BUNDLED_CONFIG = Path(__file__).parent / "commands.yaml"


def operator_config() -> Path:
    """Where an operator's own tool list lives, if they wrote one.

    Under DATA_DIR, which is deliberately not in the seatbelt write allowlist:
    this file decides what runs outside the sandbox with real credentials, so the
    agent must not be able to add itself a tool or drop a readback refusal.
    Resolved per call rather than bound at import so tests can redirect DATA_DIR.
    """
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / "commands.yaml"


def _tool_from_entry(entry: dict[str, Any]) -> ShimmedTool:
    """Build one ShimmedTool from a config entry. Raises ValueError if malformed.

    `readback` entries are written as the leading words of a command ("auth
    token") rather than as YAML lists of lists, which is unreadable and easy to
    get subtly wrong in a file whose whole job is refusing the right commands.
    """
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError("a tool entry has no name")
    if any(character.isspace() for character in name):
        raise ValueError(f"tool name {name!r} contains whitespace")

    def words(value: object, field: str) -> tuple[tuple[str, ...], ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ValueError(
                f"{name}.{field} must be a list, got {type(value).__name__}"
            )
        prefixes = []
        for item in value:
            parts = tuple(str(item).split())
            if not parts:
                raise ValueError(f"{name}.{field} has an empty entry")
            prefixes.append(parts)
        return tuple(prefixes)

    def names(value: object, field: str) -> frozenset[str]:
        if value is None:
            return frozenset()
        if not isinstance(value, list):
            raise ValueError(
                f"{name}.{field} must be a list, got {type(value).__name__}"
            )
        return frozenset(str(item) for item in value)

    return ShimmedTool(
        name=name,
        readback=frozenset(words(entry.get("readback"), "readback")),
        readback_flags=names(entry.get("readback_flags"), "readback_flags"),
        env_passthrough=names(entry.get("env_passthrough"), "env_passthrough"),
    )


def parse_tools(raw: object, *, source: str) -> tuple[ShimmedTool, ...]:
    """Parse a loaded config document. Raises ValueError if malformed."""
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: top level must be a mapping with a 'tools' key")
    # cast, not annotate: dict is invariant in its key type, so a narrowed
    # dict[Unknown, Unknown] will not assign to dict[str, Any].
    document = cast("dict[str, Any]", raw)
    entries = document.get("tools")
    if entries is None:
        raise ValueError(f"{source}: no 'tools' key")
    if not isinstance(entries, list):
        raise ValueError(f"{source}: 'tools' must be a list")
    tools = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: every tool entry must be a mapping")
        try:
            tools.append(_tool_from_entry(entry))
        except ValueError as exc:
            raise ValueError(f"{source}: {exc}") from None
    return tuple(tools)


def _read_config(path: Path) -> tuple[ShimmedTool, ...]:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # Normalised to ValueError so callers have one exception type to handle.
        # A YAMLError is not a ValueError, so without this an unparseable operator
        # file took the daemon down at startup instead of falling back.
        raise ValueError(f"{path}: not valid YAML ({exc.__class__.__name__})") from None
    return parse_tools(raw, source=str(path))


def load_tools(override: Path | None = None) -> tuple[ShimmedTool, ...]:
    """Bundled tools, with an operator file merged over them by name.

    Merge rather than replace so an operator adding one tool keeps the vetted
    readback refusals for the others. An override that drops a refusal the
    bundled entry had is legal but warned about loudly, because that is the one
    edit here that hands the agent a credential.

    A malformed operator file falls back to the bundled defaults and logs at
    ERROR. The failure mode of ignoring it entirely would be silent loss of every
    shim, which sends the agent looking for another route to the same capability
    (see the module docstring); the failure mode of falling back is that an
    operator's *additions* are missing, which the error message names.
    """
    tools = {tool.name: tool for tool in _read_config(BUNDLED_CONFIG)}
    path = override if override is not None else operator_config()
    if not path.is_file():
        return tuple(tools.values())
    try:
        extra = _read_config(path)
    except (ValueError, OSError) as exc:
        logger.error(
            "commands: ignoring %s (%s); using bundled tools only, so any tool you "
            "added there is unavailable",
            path,
            exc,
        )
        return tuple(tools.values())
    for tool in extra:
        previous = tools.get(tool.name)
        if previous is None:
            logger.info("commands: %s adds tool %r", path, tool.name)
        else:
            lost = (previous.readback - tool.readback) | frozenset(
                (flag,) for flag in previous.readback_flags - tool.readback_flags
            )
            level = logger.warning if lost else logger.info
            level(
                "commands: %s overrides bundled tool %r%s",
                path,
                tool.name,
                f"; it no longer refuses {sorted(lost)}" if lost else "",
            )
        tools[tool.name] = tool
    return tuple(tools.values())


# Env the real binary always gets. Deliberately short: the broker runs unjailed
# with the full daemon environment available, so anything not listed here stays
# out of the subprocess.
_BASE_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL")

# The shim: stdlib only, no third-party imports, because it runs inside the
# sandbox against whatever interpreter the daemon resolved at generation time.
_SHIM_SOURCE = '''#!{interpreter}
"""Generated by claude_on_the_fly.commands. Do not edit; regenerated at startup.

Forwards this invocation to the command broker outside the sandbox, which holds
the credential. Exits with whatever the real command exited with.
"""
import json
import os
import select
import sys
import urllib.error
import urllib.request

TOOL = {tool!r}
endpoint = os.environ.get({endpoint_env!r}, "")
if not endpoint:
    sys.stderr.write(
        "[sandbox] {tool} runs through the command broker, which is not "
        "reachable from here ({endpoint_env} is unset). Tell the user; this is "
        "configuration, not something you can work around.\\n"
    )
    raise SystemExit(127)

# Read piped stdin, but never block on an idle one. A child of an agent harness
# inherits a pipe that is open and silent, and a plain read() on that waits for an
# EOF that never comes: the command hangs forever with no output and no log line,
# which reads as the broker being broken. select() with a short deadline
# distinguishes the three cases -- data waiting, already at EOF (also ready, read
# returns b""), and idle (skipped).
stdin = b""
if not sys.stdin.isatty():
    try:
        ready, _, _ = select.select([sys.stdin.buffer], [], [], {stdin_wait})
        if ready:
            stdin = sys.stdin.buffer.read()
    except (OSError, ValueError):
        stdin = b""

body = json.dumps(
    {{
        "tool": TOOL,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        # How this shim was found. "gh" means PATH resolution worked; an absolute
        # path means the agent named the shim directly. Either is fine, but the
        # broker logs it, because it is the only evidence available on the parent
        # side that the invocation came through the shim at all.
        "argv0": sys.argv[0],
        "stdin": stdin.decode("utf-8", "replace"),
    }}
).encode()

request = urllib.request.Request(
    endpoint.rstrip("/") + "/run",
    data=body,
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout={timeout}) as response:
        payload = json.loads(response.read())
except urllib.error.URLError as exc:
    sys.stderr.write(f"[sandbox] command broker unreachable: {{exc}}\\n")
    raise SystemExit(127) from None

sys.stdout.write(payload.get("stdout", ""))
sys.stderr.write(payload.get("stderr", ""))
raise SystemExit(int(payload.get("rc", 1)))
'''


def leading_tokens(
    argv: list[str], *, flags_take_values: bool = True
) -> tuple[str, ...]:
    """The subcommand path: leading non-flag tokens, stopping at the first flag.

    `["pr", "view", "--json", "x"]` -> `("pr", "view")`. A flag's *value* is not
    a subcommand either, so `["--repo", "o/r", "auth", "token"]` -> `("auth",
    "token")` rather than `("o/r", "auth", "token")` — otherwise a readback match
    could be dodged by putting a global flag first.

    Whether a bare flag consumes the next token cannot be known without the
    tool's own flag table, so `flags_take_values=False` gives the other reading,
    where every non-flag token is a subcommand candidate. `refuses_readback`
    checks both, because assuming one of them is what lets `--verbose auth
    logout` walk past a refusal for `auth logout`.
    """
    tokens: list[str] = []
    skip_value = False
    for item in argv:
        if skip_value:
            skip_value = False
            continue
        if item.startswith("-"):
            # `--flag=value` carries its value; a bare flag may take the next arg.
            skip_value = flags_take_values and "=" not in item and item != "--"
            continue
        tokens.append(item)
    return tuple(tokens)


def refuses_readback(tool: ShimmedTool, argv: list[str]) -> bool:
    """True if this invocation would hand the credential back to the caller.

    Both flag readings are tested and either one matching refuses. A boolean
    global flag makes the value-consuming reading swallow the real subcommand
    (`gh --help auth token` looked like `gh token`), so a single reading is a
    bypass of the one refusal this broker has. Over-refusing the mirror case
    costs an invocation whose flag value happens to spell a refused subcommand,
    which is not a command anyone runs on purpose.
    """
    if any(flag in tool.readback_flags for flag in argv):
        return True
    if not tool.readback:
        return False
    readings = (
        leading_tokens(argv),
        leading_tokens(argv, flags_take_values=False),
    )
    return any(
        tokens[: len(prefix)] == prefix
        for tokens in readings
        for prefix in tool.readback
    )


def _subprocess_env(tool: ShimmedTool) -> dict[str, str]:
    keys = (*_BASE_ENV_KEYS, *tool.env_passthrough)
    return {key: os.environ[key] for key in keys if key in os.environ}


async def _read_capped(stream) -> tuple[bytes, bool]:
    """Read up to MAX_STREAM_BYTES. Returns (data, hit_the_cap)."""
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    while total < MAX_STREAM_BYTES:
        chunk = await stream.read(min(1 << 16, MAX_STREAM_BYTES - total))
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), True


async def _terminate(proc) -> None:
    """Kill and reap, so a killed child never becomes a zombie or a hung wait.

    The pipe transports are closed *before* the wait, not after. Killing a child
    that is still writing leaves its stdout transport holding buffered data, and
    asyncio's `wait` does not return until every pipe connection is lost as well
    as the process being reaped. Waiting first therefore always burned the full
    timeout and logged "child did not exit after kill" for a child the kernel had
    already reaped. Closing first also keeps the transport from being collected
    during loop teardown, which surfaces as a stray "Event loop is closed"
    unraisable rather than anything actionable.
    """
    if proc.returncode is None:
        proc.kill()
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:  # pragma: no cover - the kernel has not reaped it yet
        logger.warning("commands: child did not exit after kill")


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    rc: int = 0
    refused: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "stdout": self.stdout[:MAX_STREAM_BYTES],
            "stderr": self.stderr[:MAX_STREAM_BYTES],
            "rc": self.rc,
            "refused": self.refused,
        }


class CommandBroker:
    """Loopback HTTP endpoint that runs shimmed CLIs with real credentials.

    Lifecycle mirrors `broker.Broker` and `egress.EgressProxy`: `start()` binds a
    loopback port and returns it, `stop()` tears it down. Stopping revokes every
    shimmed capability at once.

    Transport is loopback TCP rather than a unix socket, which was the first
    choice. Verified against real `sandbox-exec` runs: of every candidate SBPL
    filter, only `(remote unix)` permits a unix-socket connect, and it is not
    path-scoped — it would open every socket on the machine including the Docker
    socket and the ssh-agent. A loopback port can be scoped to exactly one
    endpoint (see COTF_SANDBOX_BROKER_ONLY_LOOPBACK); a unix socket allow cannot.

    Loopback carries no authentication, and that is not a new exposure: the
    credential files this broker reads are already readable by any same-UID
    process on the host. The sandbox is the only thing being constrained, so a
    token here would be theatre rather than a boundary.
    """

    def __init__(
        self,
        shim_dir: Path,
        tools: tuple[ShimmedTool, ...] | None = None,
        *,
        run_timeout: float = _RUN_TIMEOUT_SECONDS,
    ) -> None:
        # None means "read the config", resolved here rather than as a default
        # argument so the file is read per instance instead of once at import.
        if tools is None:
            tools = load_tools()
        self._shim_dir = shim_dir
        # Only tools actually installed are shimmed; shimming an absent binary
        # would turn "command not found" into a confusing broker error.
        self._tools = {
            tool.name: tool for tool in tools if shutil.which(tool.name) is not None
        }
        self._absent = [tool.name for tool in tools if tool.name not in self._tools]
        self._run_timeout = run_timeout
        # aiohttp is imported lazily inside start() so this module stays importable
        # from the shim-generation path without pulling the web stack in.
        self._runner: Any = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("command broker not started")
        return self._port

    @property
    def shimmed(self) -> list[str]:
        return sorted(self._tools)

    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def agent_env(self) -> dict[str, str]:
        """Env pointing the sandbox at this broker and at the generated shims."""
        return {ENDPOINT_ENV: self.endpoint()}

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        from aiohttp import web

        self.write_shims()
        app = web.Application(client_max_size=_MAX_REQUEST_BYTES)
        app.router.add_post("/run", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._runner = runner
        self._port = runner.addresses[0][1]
        logger.info(
            "commands: broker on %s:%d shimming %s",
            host,
            self._port,
            ", ".join(self.shimmed) or "nothing",
        )
        if self._absent:
            logger.info(
                "commands: not shimming %s (not on PATH)", ", ".join(self._absent)
            )
        return self._port

    async def stop(self) -> None:
        if self._runner is None:
            return
        runner, self._runner = self._runner, None
        await runner.cleanup()
        self._port = None

    def write_shims(self) -> None:
        """(Re)generate one executable shim per installed tool.

        Generated rather than committed so the interpreter and endpoint are
        resolved at runtime and so no exec bit has to survive a wheel build. The
        directory lives under DATA_DIR, which is not in the sandbox's write
        allowlist, so the agent can read and exec these but not rewrite them.
        """
        self._shim_dir.mkdir(parents=True, exist_ok=True)
        # Stale shims are removed, not just left alone. This directory is on the
        # agent's PATH ahead of the real binaries (sandbox._with_shims_on_path),
        # so a shim for a tool that has since been dropped from commands.yaml or
        # uninstalled does not fail over to the real binary -- it shadows it and
        # answers "not brokered" with rc 127, permanently, until someone notices
        # the file.
        for stale in self._shim_dir.iterdir():
            if stale.is_file() and stale.name not in self._tools:
                logger.info("commands: removing stale shim %s", stale.name)
                stale.unlink()
        for name in self._tools:
            path = self._shim_dir / name
            path.write_text(
                _SHIM_SOURCE.format(
                    interpreter=sys.executable,
                    tool=name,
                    endpoint_env=ENDPOINT_ENV,
                    timeout=int(self._run_timeout),
                    stdin_wait=_STDIN_WAIT_SECONDS,
                )
            )
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    async def _handle(self, request):
        from aiohttp import web

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "malformed request"}, status=400)
        name = str(body.get("tool", ""))
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("commands: deny %r (not a shimmed tool)", name)
            return web.json_response(
                CommandResult(
                    stderr=f"[sandbox] {name!r} is not brokered.\n", rc=127
                ).as_payload()
            )
        argv = [str(item) for item in body.get("argv", [])]
        # Only a shim ever reaches this endpoint, so an arrival here *is* the
        # proof the shim was used. argv0 says how it was found: the bare name
        # means PATH resolution worked, an absolute path means the agent named
        # the shim directly. Silence proves nothing either way, which is the
        # limit worth knowing (see docs/agent/broker.md).
        logger.debug(
            "commands: shim invocation %s argv0=%r cwd=%r",
            name,
            str(body.get("argv0", "")),
            str(body.get("cwd", "")),
        )
        result = await self._run(tool, argv, body)
        return web.json_response(result.as_payload())

    async def _run(
        self, tool: ShimmedTool, argv: list[str], body: dict
    ) -> CommandResult:
        if refuses_readback(tool, argv):
            logger.warning(
                "commands: REFUSE %s %s (credential readback, cwd=%s)",
                tool.name,
                logs.redact_argv(argv),
                body.get("cwd", ""),
            )
            return CommandResult(stderr=_REFUSAL_TEXT + "\n", rc=1, refused=True)

        binary = shutil.which(tool.name)
        if binary is None:  # pragma: no cover - filtered at construction
            return CommandResult(stderr=f"[sandbox] {tool.name} not found\n", rc=127)

        cwd = str(body.get("cwd") or Path.cwd())
        if not Path(cwd).is_dir():
            cwd = str(Path.cwd())
        # The full argv is the audit record, and it is deliberately at WARNING:
        # every brokered command runs with a real credential, so it should be
        # visible without turning debug logging on.
        logger.warning(
            "commands: RUN %s %s (cwd=%s)", tool.name, logs.redact_argv(argv), cwd
        )
        subprocess_env = _subprocess_env(tool)
        # Names only. Diagnosing "gh behaved differently outside the jail" almost
        # always comes down to which of GH_HOST / GH_REPO leaked in from the
        # daemon, and that question needs the key set, not the values.
        logger.debug(
            "commands: %s runs %s with env %s",
            tool.name,
            binary,
            sorted(subprocess_env),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *argv,
                cwd=cwd,
                env=subprocess_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(
                stderr=f"[sandbox] cannot run {tool.name}: {exc}\n", rc=127
            )

        stdin_bytes = str(body.get("stdin") or "").encode()
        try:
            out, err, truncated = await asyncio.wait_for(
                self._collect(proc, stdin_bytes), timeout=self._run_timeout
            )
        except TimeoutError:
            await _terminate(proc)
            logger.warning(
                "commands: %s timed out after %.0fs", tool.name, self._run_timeout
            )
            return CommandResult(
                stderr=f"[sandbox] {tool.name} timed out after "
                f"{self._run_timeout:.0f}s\n",
                rc=124,
            )
        rc = proc.returncode or 0
        note = ""
        if truncated:
            note = (
                f"\n[sandbox] output truncated at {MAX_STREAM_BYTES} bytes. Narrow "
                "the command, or write the full result to a file in the workspace.\n"
            )
            logger.warning("commands: %s output truncated at the cap", tool.name)
        logger.info(
            "commands: %s exited %d (%d B stdout, %d B stderr)",
            tool.name,
            rc,
            len(out),
            len(err),
        )
        return CommandResult(
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace") + note,
            rc=rc,
        )

    @staticmethod
    async def _collect(proc, stdin_bytes: bytes) -> tuple[bytes, bytes, bool]:
        """Feed stdin, then read both streams under a byte cap.

        Capping *while* reading rather than truncating afterwards: a command like
        `yes` produces without bound, and buffering it all before applying the cap
        turns any chatty command into a daemon memory bomb (it hung a test before
        this was fixed).

        Both streams are read concurrently. Draining stdout to the cap first would
        let stderr fill its pipe buffer and block the child forever.

        The child is killed as soon as *either* stream caps, before the other is
        awaited. Waiting for both to finish first deadlocks: the capped stream
        stops being read, the child blocks writing into a full pipe, and so it
        never exits and never closes the stream still being awaited. That turned
        every over-long output into a `run_timeout` expiry, which reports a
        timeout the command did not actually have.
        """
        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            proc.stdin.close()
        reads = [
            asyncio.ensure_future(_read_capped(proc.stdout)),
            asyncio.ensure_future(_read_capped(proc.stderr)),
        ]
        pending = set(reads)
        capped = False
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            if any(task.result()[1] for task in done):
                capped = True
                # Frees the child from its full pipe, so the other read reaches
                # EOF instead of waiting on an exit that will never come.
                await _terminate(proc)
        out, err = (task.result() for task in reads)
        if not capped:
            await proc.wait()
        return out[0], err[0], capped
