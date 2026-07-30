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
from typing import Any

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


# Only `gh` for now. Adding a tool is one entry plus denying its credential in
# the seatbelt profile; no new machinery. See docs/agent/broker.md.
SHIMMED_TOOLS: tuple[ShimmedTool, ...] = (
    ShimmedTool(
        name="gh",
        readback=frozenset({("auth", "token")}),
        readback_flags=frozenset({"--show-token"}),
        env_passthrough=frozenset({"GH_HOST", "GH_REPO", "GH_PAGER", "NO_COLOR"}),
    ),
)

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


def leading_tokens(argv: list[str]) -> tuple[str, ...]:
    """The subcommand path: leading non-flag tokens, stopping at the first flag.

    `["pr", "view", "--json", "x"]` -> `("pr", "view")`. A flag's *value* is not
    a subcommand either, so `["--repo", "o/r", "auth", "token"]` -> `("auth",
    "token")` rather than `("o/r", "auth", "token")` — otherwise a readback match
    could be dodged by putting a global flag first.
    """
    tokens: list[str] = []
    skip_value = False
    for item in argv:
        if skip_value:
            skip_value = False
            continue
        if item.startswith("-"):
            # `--flag=value` carries its value; a bare flag may take the next arg.
            skip_value = "=" not in item and item != "--"
            continue
        tokens.append(item)
    return tuple(tokens)


def refuses_readback(tool: ShimmedTool, argv: list[str]) -> bool:
    """True if this invocation would hand the credential back to the caller."""
    if any(flag in tool.readback_flags for flag in argv):
        return True
    tokens = leading_tokens(argv)
    return any(tokens[: len(prefix)] == prefix for prefix in tool.readback)


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

    The pipe transports are closed explicitly afterwards. Killing a child that is
    still writing leaves its stdout transport holding buffered data, and asyncio
    then closes it during loop teardown — which surfaces as a stray
    "Event loop is closed" unraisable rather than anything actionable.
    """
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - the kernel has not reaped it yet
            logger.warning("commands: child did not exit after kill")
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


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
        tools: tuple[ShimmedTool, ...] = SHIMMED_TOOLS,
        *,
        run_timeout: float = _RUN_TIMEOUT_SECONDS,
    ) -> None:
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
        """
        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            proc.stdin.close()
        out, err = await asyncio.gather(
            _read_capped(proc.stdout), _read_capped(proc.stderr)
        )
        # Past the cap the child may still be writing, so stop it rather than
        # waiting on an exit that will not come.
        if out[1] or err[1]:
            await _terminate(proc)
        else:
            await proc.wait()
        return out[0], err[0], out[1] or err[1]
