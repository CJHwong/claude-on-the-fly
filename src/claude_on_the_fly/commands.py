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

**Action access is an explicit positive allowlist.** Each configured tool may
declare leading subcommand prefixes such as `pr list` or `repo view`; a missing
or empty list denies every invocation. This is intentionally a conservative
prefix gate, not a parser for a CLI's full API semantics: arguments and flags
after an allowed prefix are passed through, while generic operations such as
`gh api` remain unavailable unless an operator explicitly opts them in. The
provider credential should still have the smallest possible scope because no
argv policy can understand every future CLI flag safely.

The one thing that *is* refused is **credential readback** — a command whose
output is the secret itself. That is not policy, it is closing the door this
broker opens: forwarding `gh auth token` would place the token straight into the
sandbox and defeat the entire design.

Which tools are shimmed comes from the `commands:` section of the sandbox policy
file; see `settings.py` for where that lives and why.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from claude_on_the_fly import logs, settings

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
TOKEN_ENV = "COTF_CMD_TOKEN"
TOKEN_HEADER = "X-COTF-Command-Token"

# Files in the shim directory that are not command shims and must survive the
# stale sweep. The permission-approval shim shares the directory because
# fs-deny-most.sb re-grants reads there and nowhere else under DATA_DIR; without
# this the sweep would delete it on the next startup and every gated tool call
# would fail on a missing interpreter.
RESERVED_SHIM_NAMES = frozenset({"cotf-approve"})

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
    :param allow: leading subcommand paths that may run. An empty list denies
        every invocation; the command broker is deliberately not a general
        shell for a credentialed binary.
    :param env_passthrough: extra parent env names the real binary needs beyond
        the shared essentials. Kept narrow so the subprocess does not inherit
        every secret the daemon happens to hold.
    :param allow_read_only: prefixes that may run only when the invocation asks
        the server to read. For a subcommand that is a whole REST API behind one
        word, `allow` is all-or-nothing: `gh api` covers reading a file and
        rewriting a repository's settings. Listing it here admits the reads and
        refuses the writes. An empty tuple keeps the previous behaviour exactly,
        so this is opt-in per tool.
    :param allow_paths: trees outside the session workspace this tool may be
        given absolute path arguments for. The guard otherwise refuses every
        absolute path, which is right for a credentialed CLI and wrong for one
        whose job is to read a file the agent names. An entry reaching a
        credential store is refused; see `allowed_roots`. Empty by default.
    :param boolean_flags: flags that never take the next token as their value.
        Without a flag table the allowlist reads every bare flag as taking one,
        so `systemctl --user status x` looked like `x` and was refused. Listing
        `--user` here fixes the reading for that flag only. Empty by default.
    """

    name: str
    readback: frozenset[tuple[str, ...]] = frozenset()
    readback_flags: frozenset[str] = frozenset()
    env_passthrough: frozenset[str] = frozenset()
    allow: tuple[tuple[str, ...], ...] = ()
    allow_read_only: tuple[tuple[str, ...], ...] = ()
    allow_paths: frozenset[str] = frozenset()
    boolean_flags: frozenset[str] = frozenset()


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
        allow=words(entry.get("allow"), "allow"),
        allow_read_only=words(entry.get("allow_read_only"), "allow_read_only"),
        allow_paths=names(entry.get("allow_paths"), "allow_paths"),
        boolean_flags=names(entry.get("boolean_flags"), "boolean_flags"),
    )


def parse_tools(raw: object, *, source: str) -> tuple[ShimmedTool, ...]:
    """Parse a `commands:` section. Raises ValueError if malformed."""
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: the commands section must be a mapping")
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


def load_tools() -> tuple[ShimmedTool, ...]:
    """Bundled tools, with the operator's `commands:` section merged over by name.

    Merge rather than replace so an operator adding one tool keeps the vetted
    readback refusals for the others. An override that drops a refusal the
    bundled entry had is legal but warned about loudly, because that is the one
    edit here that hands the agent a credential.

    A malformed section falls back to the bundled defaults and logs at ERROR. The
    failure mode of ignoring it entirely would be silent loss of every shim, which
    sends the agent looking for another route to the same capability (see the
    module docstring); the failure mode of falling back is that an operator's
    *additions* are missing, which the error message names.
    """
    tools = {
        tool.name: tool
        for tool in parse_tools(
            settings.bundled("commands"), source=str(settings.BUNDLED_SETTINGS)
        )
    }
    section = settings.operator("commands")
    if not section:
        return tuple(tools.values())
    path = settings.operator_settings()
    try:
        extra = parse_tools(section, source=str(path))
    except ValueError as exc:
        logger.error(
            "commands: ignoring the commands section (%s); using bundled tools only, "
            "so any tool you added there is unavailable",
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


def installed(
    tools: tuple[ShimmedTool, ...],
) -> tuple[dict[str, ShimmedTool], list[str]]:
    """Split configured tools into the shimmable ones and the names not on PATH.

    Only tools actually installed are shimmed; shimming an absent binary would
    turn "command not found" into a confusing broker error.
    """
    present = {tool.name: tool for tool in tools if shutil.which(tool.name) is not None}
    return present, [tool.name for tool in tools if tool.name not in present]


def shimmed_names() -> list[str]:
    """Names of the tools that would be brokered on this host.

    Exposed so the agent's sandbox guidance can name them. An agent that knows
    `gh` is brokered and `aws` is not can tell a policy boundary from a broken
    tool, and relay the remedy that actually works.
    """
    return sorted(installed(load_tools())[0])


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
token = os.environ.get({token_env!r}, "")
if not endpoint:
    sys.stderr.write(
        "[sandbox] {tool} runs through the command broker, which is not "
        "reachable from here ({endpoint_env} is unset). Tell the user; this is "
        "configuration, not something you can work around.\\n"
    )
    raise SystemExit(127)
if not token:
    sys.stderr.write(
        "[sandbox] command broker token is missing; this invocation is refused.\\n"
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
request.add_header({token_header!r}, token)
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
    argv: list[str],
    *,
    flags_take_values: bool = True,
    boolean_flags: frozenset[str] = frozenset(),
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
    logout` walk past a refusal for `auth logout`. `boolean_flags` is the part of
    that table an operator declared: those flags never consume the next token.
    """
    tokens: list[str] = []
    skip_value = False
    for item in argv:
        if skip_value:
            skip_value = False
            continue
        if item.startswith("-"):
            # `--flag=value` carries its value; a bare flag may take the next arg.
            skip_value = (
                flags_take_values
                and "=" not in item
                and item != "--"
                and item not in boolean_flags
            )
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

    A readback flag is matched in every spelling its parser accepts, not just the
    bare one. `gh auth status --show-token=true` is accepted by real gh, checked
    against gh 2.100.0, so an exact-token match let the credential-printing flag
    through under the one spelling nobody thought to write down.
    """
    if any(
        _carries_flag(token, flag) for token in argv for flag in tool.readback_flags
    ):
        return True
    if not tool.readback:
        return False
    readings = (
        leading_tokens(argv, boolean_flags=tool.boolean_flags),
        leading_tokens(argv, flags_take_values=False),
    )
    return any(
        tokens[: len(prefix)] == prefix
        for tokens in readings
        for prefix in tool.readback
    )


# How a CLI says which HTTP method it wants. `-X` is the convention curl set and
# gh, hub and http share; gh spells the long form `--method`.
_METHOD_FLAGS = ("-X", "--method")

# Flags that add request parameters. These matter because of a behaviour that is
# easy to miss: `gh api --help` states "The default HTTP request method is GET
# normally and POST if any parameters were added", and again under `-f`, "adding
# request parameters will automatically switch the request method to POST". So a
# gate that reads only `--method` would pass `gh api repos/o/r -f a=b` as a read
# while gh performs a POST. Taken from the installed binary's own help, not from
# memory of the documentation.
# `--input` is here for the same reason, and was missed for longer: it supplies
# the request body from a file (or stdin with `-`), and gh POSTs when it is
# given. Measured against gh 2.100.0 pointed at a local server: `gh api repos/o/r
# --input /dev/null` sends POST.
_PARAMETER_FLAGS = ("-f", "--raw-field", "-F", "--field", "--input")

# HEAD is included because it is a read that returns no body. Everything else,
# including an unrecognised or absent value, is treated as a write.
_READ_METHODS = frozenset({"GET", "HEAD"})


def _flag_value(argv: list[str], flags: tuple[str, ...]) -> str | None:
    """The last value given to any of ``flags``, or None if none was given.

    Three spellings are read, `-X GET`, `--method=GET` and the attached short
    form `-XGET`. The last one wins because that is what an argument parser does
    with a repeated flag. A flag that ends the argv has no value, and returns the
    empty string rather than None so the caller can tell "malformed" from
    "absent" and refuse it.

    The attached form is not a nicety. gh's parser accepts it, so `gh api
    repos/o/r -XPOST` performs a POST while a gate reading only the separated
    spellings called it a read -- measured against gh 2.100.0 pointed at a local
    server, which reports the method it chose.
    """
    found: str | None = None
    for index, token in enumerate(argv):
        for flag in flags:
            if token == flag:
                found = argv[index + 1] if index + 1 < len(argv) else ""
            elif token.startswith(f"{flag}="):
                found = token[len(flag) + 1 :]
            elif (attached := _attached_short_value(token, flag)) is not None:
                found = attached
    return found


def _carries_flag(token: str, flag: str) -> bool:
    """Whether ``token`` is ``flag``, in any spelling a parser accepts.

    The bare flag, the `=value` form a boolean flag still takes (`--show-token=true`),
    and a value glued onto a short flag.
    """
    return (
        token == flag
        or token.startswith(f"{flag}=")
        or _attached_short_value(token, flag) is not None
    )


def _attached_short_value(token: str, flag: str) -> str | None:
    """The value glued onto a short flag, as in `-XPOST`, or None.

    Only for a real short flag, `-` plus one character. A long flag never carries
    its value this way, and treating `--methodological` as `--method` with the
    value `ological` would invent a flag the CLI does not have.
    """
    if len(flag) != 2 or flag.startswith("--") or not flag.startswith("-"):
        return None
    if not token.startswith(flag) or len(token) <= len(flag):
        return None
    return token[len(flag) :]


def requests_read_only(argv: list[str]) -> bool:
    """Whether this invocation asks the server only to read.

    An explicit method decides on its own. Without one, a parameter flag means
    the CLI will POST, so only a parameter-free invocation is a read.
    """
    method = _flag_value(argv, _METHOD_FLAGS)
    if method is not None:
        return method.strip().upper() in _READ_METHODS
    return not any(
        _carries_flag(token, flag) for token in argv for flag in _PARAMETER_FLAGS
    )


def allowed_command(tool: ShimmedTool, argv: list[str]) -> bool:
    """Return whether ``argv`` starts with one configured safe subcommand.

    Flags are removed while identifying the leading subcommand path, so ordinary
    options may appear before or after a vetted prefix. This is deliberately not
    a full CLI parser; a tool with no entries is deny-by-default and provider-side
    credential scope remains necessary.

    A prefix on `allow_read_only` additionally has to ask for a read. `allow` is
    checked first, so a prefix listed on both is allowed outright.

    Both flag readings have to admit the command, the mirror of the rule
    `refuses_readback` applies. Reading only the value-taking one let a boolean
    flag the operator did not declare swallow the real verb: with `status`
    allowed, `systemctl --quiet stop status` matched `status` and systemctl ran
    `stop`. The cost is a value flag written before the subcommand, such as
    `gh --repo o/r pr view`; none of 1884 real calls on the deployed host did that.
    """
    return all(
        _admits(tool, tokens, argv)
        for tokens in (
            leading_tokens(argv, boolean_flags=tool.boolean_flags),
            leading_tokens(argv, flags_take_values=False),
        )
    )


def _admits(tool: ShimmedTool, tokens: tuple[str, ...], argv: list[str]) -> bool:
    """Whether one reading of the subcommand path is on the allowlist."""
    if any(tokens[: len(prefix)] == prefix for prefix in tool.allow):
        return True
    if any(tokens[: len(prefix)] == prefix for prefix in tool.allow_read_only):
        return requests_read_only(argv)
    return False


def hidden_by_a_leading_flag(tool: ShimmedTool, argv: list[str]) -> bool:
    """True when a flag before the subcommand is the only reason for a refusal.

    Only used to choose the refusal wording. The broker cannot tell whether
    `--profile` in `aws --profile prod logs tail` takes a value, so the command
    is refused, but the CLI accepts the flag after the subcommand. Naming that
    fix keeps the agent from asking the operator for a prefix already listed.
    Moving the flag also exposes the real verb when the flag was boolean, so the
    hint is safe to give for an attempt to hide one.
    """
    if allowed_command(tool, argv):
        return False
    tokens = leading_tokens(argv, boolean_flags=tool.boolean_flags)
    return _admits(tool, tokens, argv)


def refused_as_write(tool: ShimmedTool, argv: list[str]) -> bool:
    """True when the prefix is admitted for reads but this invocation writes.

    Only used to choose the refusal wording. "this subcommand is not
    allowlisted" sends the agent to ask for a prefix that is already configured,
    and it retries or works around instead of dropping the write.
    """
    if allowed_command(tool, argv) or requests_read_only(argv):
        return False
    tokens = leading_tokens(argv, boolean_flags=tool.boolean_flags)
    return any(tokens[: len(prefix)] == prefix for prefix in tool.allow_read_only)


def _path_candidates(item: str) -> list[str]:
    """The substrings of one argv token that could be a path.

    An option carries its value three ways, and reading only the first left two
    bypasses. `--file=/etc/passwd` splits once, which is what the guard used to
    do; `--opt=a=/etc/passwd` puts an `=` inside the value, so the single split
    handed the check `a=/etc/passwd`, which is relative and passed; and a short
    option attaches its value with no separator at all, so `-o/etc/passwd` and
    `-D/Users/someone` were never looked inside.

    Everything after the short option letter is the value, because there is no
    way to tell `-C..` (option C, value `..`) from a cluster of boolean flags
    without a per-flag arity model the broker deliberately does not have. Reading
    the tail of a boolean cluster as a path over-refuses at worst: `-abc` is
    checked as the relative path `bc`, which is inside the workspace and allowed.
    """
    # The value after the first `=` as well as each `=` segment: a JSON value can
    # hold an `=` of its own, and the segments then cut it into invalid halves.
    whole_value = item.split("=", 1)[1:]
    if item.startswith("-"):
        candidates = [*whole_value, *item.split("=")[1:]]
        if not item.startswith("--"):
            candidates.append(item[2:])
    else:
        # A bare `key=value` token carries a path in its value for any CLI that
        # takes typed fields: `gh api -F body=@/etc/passwd` arrives as the single
        # token `body=@/etc/passwd`, which is not a flag, so splitting only flags
        # read the whole thing as one relative path inside the workspace.
        candidates = [item, *whole_value, *item.split("=")[1:]]
    candidates += [
        text for candidate in candidates for text in _json_strings(candidate)
    ]
    return [form for candidate in candidates for form in _introduced_paths(candidate)]


# Every string literal in JSON text, found without parsing it. A parser gives up on
# deep nesting (RecursionError) and on the lenient spellings some CLIs accept, and
# either failure would hand the guard nothing to check. A literal is a literal at
# any depth, so this reads the same strings the tool's own parser would.
_JSON_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _json_strings(candidate: str) -> list[str]:
    """The string values inside a JSON argument, each read as a path candidate.

    The guard reads a token as a path, so `--params '{"body":"/etc/passwd"}'` was
    one relative-looking token. No brokered tool is known to open a file named
    this way, and gws measured does not, but the guard should not depend on every
    future tool's grammar. None of 675 real JSON arguments on the deployed host
    held a value this refuses.
    """
    if candidate.lstrip()[:1] not in ("{", "["):
        return []
    strings = []
    for literal in _JSON_STRING.findall(candidate):
        try:
            strings.append(json.loads(f'"{literal}"'))
        except json.JSONDecodeError:
            strings.append(literal)
    return strings


# How an argument says "what follows is a file". `@` is the convention curl set
# and gh, http and jq share; `file://` is the URL spelling of the same thing.
# Neither makes the argument start with `/`, so without this a path behind one
# reads as an ordinary relative name and the guard lets it through.
#
# Matched case-insensitively, because RFC 3986 makes a URL scheme
# case-insensitive and curl honours that: `FILE:///etc/passwd` reads the file,
# measured. The `@` is unaffected by case, so one rule covers both.
_PATH_INTRODUCERS = ("@", "file://")
_URL_INTRODUCER = "file://"


def _introduced_paths(candidate: str) -> list[str]:
    """`candidate`, plus whatever it carries after a path introducer.

    Applied repeatedly, because the forms nest: `body=@file:///etc/passwd` has to
    shed the `@` and then the scheme before the absolute path is visible.

    Emitting rather than refusing keeps this cheap to be wrong about. An extra
    candidate only matters if it is absolute or traversing, so the ordinary Slack
    and GitHub arguments that begin with `@` cost nothing: `@alice` yields the
    extra candidate `alice`, which is relative and inside the workspace, exactly
    like the argument it came from.
    """
    forms = [candidate]
    seen = {candidate}
    queue = [candidate]
    while queue:
        current = queue.pop()
        for introducer in _PATH_INTRODUCERS:
            if current[: len(introducer)].lower() != introducer:
                continue
            rest = current[len(introducer) :]
            for form in (rest, *_url_forms(introducer, rest)):
                if form and form not in seen:
                    seen.add(form)
                    forms.append(form)
                    queue.append(form)
    return forms


def _url_forms(introducer: str, rest: str) -> tuple[str, ...]:
    """The other readings of a `file://` URL's tail: authority dropped, decoded.

    Two things a URL does that a filename does not, both measured against real
    curl rather than read from a document:

    - RFC 8089 makes `file://localhost/etc/passwd` mean `/etc/passwd`. Stripping
      only the scheme leaves `localhost/etc/passwd`, which is relative, so the
      guard would pass it. The authority is not parsed, because nothing here
      needs to know what a valid one looks like -- everything from the first
      slash is the path either way.
    - The path is percent-decoded, so `%2e%2e` is `..` and `%2f` is `/`. Without
      decoding, `file://<workspace>/%2e%2e/%2e%2e/etc/passwd` is lexically inside
      the workspace and allowed, and curl then reads `/etc/passwd`.

    Decoding runs to a fixed point rather than once. The caller re-queues what
    this returns, but a decoded form no longer starts with `file://`, so it would
    never re-enter this branch and `%252e%252e` would stop one step short of
    `..`. curl decodes once, so that step is past what curl itself does; it is
    taken anyway because over-refusing is the trade this guard makes everywhere,
    and the only thing it costs is a filename whose literal name contains `%25`.
    """
    if introducer != _URL_INTRODUCER:
        return ()
    slash = rest.find("/")
    forms = [rest[slash:]] if slash > 0 else []
    return tuple(
        decoded for form in (rest, *forms) if (decoded := _fully_decoded(form)) != form
    ) + tuple(forms)


# Enough for any real argument; a doubly-encoded path needs two.
_MAX_DECODE_PASSES = 8


def _fully_decoded(value: str) -> str:
    """`value` percent-decoded until it stops changing.

    Terminates because a decode either shortens the string (`%2e` -> `.`) or
    leaves it alone, and the cap is belt-and-braces against a decoder that ever
    stops being true.
    """
    for _ in range(_MAX_DECODE_PASSES):
        decoded = urllib.parse.unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def allowed_roots(tool: ShimmedTool) -> list[Path]:
    """`tool.allow_paths`, resolved, minus every entry that is refused.

    Validated against the sandbox's own refusal list rather than a second copy of
    it. A broker root is a sharper grant than a jail grant, not a softer one: the
    broker runs the real binary *outside* the sandbox holding the operator's real
    credential, so a root reaching `~/.ssh` hands the agent that key through a
    CLI that was only meant to read pull requests. Sharing the list is what keeps
    a credential added there from staying reachable here.

    A refused root is dropped and the rest are kept, matching `sandbox.extra_paths`
    for the reason recorded there: one typo should not cost an operator the roots
    that make the tool usable.
    """
    from claude_on_the_fly import sandbox

    roots: list[Path] = []
    for entry in tool.allow_paths:
        resolved = sandbox.resolve_grant_entry(entry)
        refusal = sandbox._extra_path_refusal(resolved)
        if refusal is not None:
            logger.error(
                "commands: %s allow_paths entry %r (resolved to %s) is refused: "
                "%s. The broker runs outside the sandbox with a real credential, "
                "so this root would be readable with the operator's identity.",
                tool.name,
                entry,
                resolved,
                refusal,
            )
            continue
        roots.append(resolved)
    return roots


def _unsafe_path_argument(
    argv: list[str], cwd: str | None = None, allow_roots: Iterable[Path] = ()
) -> str | None:
    """Return the first absolute/traversing/escaping argument, or ``None``.

    The broker is not a file-transfer channel. Relative paths are still allowed
    for a vetted command and are resolved by the CLI from the session workspace;
    host-absolute and escaping paths are refused before process creation. The
    Windows check matters when a cross-platform config is exercised on macOS.

    An absolute path is judged by where it lands, not by its leading slash. One
    that resolves inside a root is the session's own workspace written the long
    way, and refusing it while allowing the relative spelling of the same file
    guarded nothing -- it only taught the agent to rewrite the argument.

    `allow_roots` carries every tree an absolute argument may land in: the
    operator's `allow_paths` entries, and the session workspace itself when the
    caller has one. The workspace is passed in rather than taken from `cwd`,
    because `cwd` is only vetted against a workspace when there is one to vet it
    against. With none, `_workspace_cwd` accepts whatever the client said, and
    treating that as a root would make a declared cwd of `/` admit every absolute
    path on the host. Containment is checked after resolving, so a symlink planted
    in the workspace cannot point into a root and then out the other side.

    With ``cwd``, each candidate is also resolved against it and required to stay
    inside. That is the only reading that catches a relative path through a
    symlink, and the agent can plant one: its workspace is writable, so `ln -s /
    link` makes `link/etc/passwd` a lexically clean relative path that the CLI
    then opens as `/etc/passwd`. Resolving is the right half of the choice
    because the guard cannot refuse symlink components instead: the workspace
    itself is reached through `/var` -> `/private/var` on macOS, so every
    argument in it has one. Containment is checked against the resolved cwd for
    the same reason. `cwd` is what `_workspace_cwd` already vetted as being
    inside the session workspace, so containment here inherits that boundary
    rather than restating it.
    """
    root = Path(cwd).resolve(strict=False) if cwd else None
    permitted = [Path(entry).resolve(strict=False) for entry in allow_roots]
    for item in argv:
        for candidate in _path_candidates(item):
            if not candidate or candidate == ".":
                continue
            if candidate.startswith(("/", "~/", "~\\")):
                if _within_any(candidate, permitted):
                    continue
                return item
            if candidate == "..":
                return item
            if (
                PureWindowsPath(candidate).is_absolute()
                or ".." in Path(candidate).parts
            ):
                return item
            if root is not None and not _contained(root, candidate):
                return item
    return None


def _within_any(candidate: str, allow_roots: Iterable[Path]) -> bool:
    """Whether an absolute candidate resolves inside one of the allowed roots.

    Resolved first, so `~` and a symlink are judged by where they land. A
    candidate that resolves nowhere is not inside anything and is refused, which
    is the opposite of `_contained`'s reading: there an unresolvable path was
    already relative and confined, here it is the agent naming a host path that
    does not exist yet, and a write would create it.
    """
    try:
        resolved = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):  # pragma: no cover - resolve is lexical here
        return False
    return any(
        resolved == root or resolved.is_relative_to(root) for root in allow_roots
    )


def _contained(root: Path, candidate: str) -> bool:
    """Whether ``candidate``, resolved from ``root``, stays under it.

    A candidate that cannot be resolved at all is treated as contained: it is not
    a path the CLI can open either, and refusing on an OSError would turn an
    unreadable intermediate directory into a refused command.
    """
    try:
        return (root / candidate).resolve(strict=False).is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return True


def _workspace_cwd(body: dict, workspace: Path | None) -> str | None:
    """Validate a shim-reported cwd against its session workspace."""
    raw = body.get("cwd")
    if workspace is None:
        cwd = str(raw or Path.cwd())
        return cwd if Path(cwd).is_dir() else str(Path.cwd())
    if not isinstance(raw, str) or not raw:
        return None
    try:
        root = workspace.resolve(strict=False)
        candidate = Path(raw)
        if not candidate.is_absolute():
            return None
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(resolved) if resolved.is_dir() else None


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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if proc.returncode is None:
            proc.kill()
    except OSError:
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

    The endpoint requires a high-entropy bearer token. Production tokens are
    issued per turn and bound to that turn's canonical workspace; a process that
    merely discovers the loopback port cannot invoke a credentialed CLI or move
    its working directory outside the workspace. It is not a substitute for OS
    isolation, but it closes accidental and cross-process loopback access.
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
        self._tools, self._absent = installed(tools)
        self._run_timeout = run_timeout
        # aiohttp is imported lazily inside start() so this module stays importable
        # from the shim-generation path without pulling the web stack in.
        self._runner: Any = None
        self._port: int | None = None
        self._token = secrets.token_urlsafe(32)
        self._session_workspaces: dict[str, Path] = {}

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

    def agent_env(self, workspace: Path | None = None) -> dict[str, str]:
        """Env for a broker client, optionally scoped to one workspace.

        The no-argument form is retained for the broker's local test harness and
        startup plumbing. A real agent turn must pass its workspace so the token
        cannot be replayed against another directory.
        """
        if workspace is None:
            token = self._token
        else:
            token = secrets.token_urlsafe(32)
            self._session_workspaces[token] = workspace.resolve(strict=False)
        return {ENDPOINT_ENV: self.endpoint(), TOKEN_ENV: token}

    def publish_endpoint(self) -> None:
        """Put the endpoint, and only the endpoint, in this daemon's environment.

        The endpoint is harmless daemon-wide, and `sandbox.agent_env` forwards it
        to every spawn. The bearer token must be issued per turn and bound to that
        turn's workspace, so the private base token is kept out of the environment
        where agent_env could forward it by accident.
        """
        os.environ[ENDPOINT_ENV] = self.endpoint()
        os.environ.pop(TOKEN_ENV, None)

    def revoke_token(self, token: str) -> None:
        """Revoke one per-turn workspace token after its agent is reaped."""
        self._session_workspaces.pop(token, None)

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
            self._session_workspaces.clear()
            return
        runner, self._runner = self._runner, None
        await runner.cleanup()
        self._port = None
        self._session_workspaces.clear()

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
        # so a shim for a tool that has since been dropped from the config or
        # uninstalled does not fail over to the real binary -- it shadows it and
        # answers "not brokered" with rc 127, permanently, until someone notices
        # the file.
        for stale in self._shim_dir.iterdir():
            if stale.name in RESERVED_SHIM_NAMES:
                # Not a tool shim and not ours to sweep. It lives here because
                # fs-deny-most.sb re-grants reads on this directory and nothing
                # else under DATA_DIR, so it is the one place a sandboxed agent can
                # exec a generated helper from.
                continue
            if stale.name.startswith("."):
                # Another daemon's shim mid-write (see below). Both daemons
                # write this directory, and sweeping the temp file fails that
                # daemon's rename.
                continue
            if stale.is_file() and stale.name not in self._tools:
                logger.info("commands: removing stale shim %s", stale.name)
                stale.unlink()
        for name in self._tools:
            # Written aside and renamed over. The chat and jobs daemons both
            # write here at start, and an agent may exec a shim at any moment;
            # a rename hands it the old file or the new one, never half of one.
            path = self._shim_dir / f".{name}.{os.getpid()}.tmp"
            path.write_text(
                _SHIM_SOURCE.format(
                    interpreter=sys.executable,
                    tool=name,
                    endpoint_env=ENDPOINT_ENV,
                    token_env=TOKEN_ENV,
                    token_header=TOKEN_HEADER,
                    timeout=int(self._run_timeout),
                    stdin_wait=_STDIN_WAIT_SECONDS,
                )
            )
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
            os.replace(path, self._shim_dir / name)

    async def _handle(self, request):
        from aiohttp import web

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "malformed request"}, status=400)
        supplied = request.headers.get(TOKEN_HEADER, "")
        workspace = None
        if supplied and hmac.compare_digest(supplied, self._token):
            # The daemon's private base token is useful only to local callers
            # such as tests/startup diagnostics; production turns receive a
            # workspace-scoped token below and never inherit this one.
            workspace = None
        elif supplied:
            workspace = self._session_workspaces.get(supplied)
        base_token = bool(supplied and hmac.compare_digest(supplied, self._token))
        if not supplied or (workspace is None and not base_token):
            logger.warning("commands: deny unauthenticated loopback request")
            return web.json_response({"error": "unauthorized"}, status=403)
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
        result = await self._run(tool, argv, body, workspace=workspace)
        return web.json_response(result.as_payload())

    async def _run(
        self,
        tool: ShimmedTool,
        argv: list[str],
        body: dict,
        *,
        workspace: Path | None = None,
    ) -> CommandResult:
        if not allowed_command(tool, argv):
            logger.warning(
                "commands: REFUSE %s %s (%s)",
                tool.name,
                logs.redact_argv(argv),
                "write on a read-only subcommand"
                if refused_as_write(tool, argv)
                else "a flag before the subcommand"
                if hidden_by_a_leading_flag(tool, argv)
                else "not in the configured command allowlist",
            )
            if refused_as_write(tool, argv):
                return CommandResult(
                    stderr=(
                        f"[sandbox] {tool.name} may run this subcommand to read, "
                        "but this invocation asks the server to write. Re-run it "
                        "as a read, or ask the operator to do the write.\n"
                    ),
                    rc=126,
                    refused=True,
                )
            if hidden_by_a_leading_flag(tool, argv):
                return CommandResult(
                    stderr=(
                        f"[sandbox] {tool.name}: a flag before the subcommand "
                        "hides it from the allowlist. Put every flag after the "
                        "subcommand and run it again.\n"
                    ),
                    rc=126,
                    refused=True,
                )
            return CommandResult(
                stderr=(
                    f"[sandbox] {tool.name} subcommand is not allowlisted. "
                    "Ask the operator to add this exact safe subcommand to "
                    "commands.tools.allow.\n"
                ),
                rc=126,
                refused=True,
            )
        if refuses_readback(tool, argv):
            logger.warning(
                "commands: REFUSE %s %s (credential readback, cwd=%s)",
                tool.name,
                logs.redact_argv(argv),
                body.get("cwd", ""),
            )
            return CommandResult(stderr=_REFUSAL_TEXT + "\n", rc=1, refused=True)

        # The cwd is settled before the argv check rather than after, because a
        # relative argument means nothing without the directory the CLI resolves
        # it from: that is what turns a symlink the agent planted in its own
        # workspace back into the absolute path it points at.
        cwd = _workspace_cwd(body, workspace)
        if cwd is None:
            logger.warning(
                "commands: REFUSE %s (cwd is outside its session workspace)",
                tool.name,
            )
            return CommandResult(
                stderr=(
                    "[sandbox] the command broker only runs inside this session's "
                    "workspace.\n"
                ),
                rc=126,
                refused=True,
            )

        # The workspace joins the operator's roots here rather than being taken
        # from `cwd` inside the guard: `cwd` is only vetted when there is a
        # workspace to vet it against, so a session without one must not have its
        # client-declared cwd promoted to an allow root.
        roots = allowed_roots(tool)
        if workspace is not None:
            roots.append(Path(cwd))
        unsafe_path = _unsafe_path_argument(argv, cwd, roots)
        if unsafe_path is not None:
            logger.warning(
                "commands: REFUSE %s %s (absolute or escaping path argument)",
                tool.name,
                logs.redact_argv(argv),
            )
            return CommandResult(
                stderr=(
                    "[sandbox] absolute and workspace-escaping path arguments are "
                    "not allowed through the command broker.\n"
                ),
                rc=126,
                refused=True,
            )

        binary = shutil.which(tool.name)
        if binary is None:  # pragma: no cover - filtered at construction
            return CommandResult(stderr=f"[sandbox] {tool.name} not found\n", rc=127)

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
                start_new_session=True,
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
