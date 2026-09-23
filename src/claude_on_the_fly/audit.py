"""Replay past tool calls against a config, as if the agent ran under the jail.

    python -m claude_on_the_fly.audit --data-dir DIR [TRANSCRIPT_OR_DIR ...]

Reads claude transcripts (`*.jsonl`, default `~/.claude/projects`) and judges every
recorded call against the `config.yaml` in DIR with `sandbox.mode: jail` forced:

- each brokered tool call, by the broker's own checks, and whether it reached the
  shim at all (a bare name) or bypassed it (a path, or a variable holding one);
- each file write (Write, Edit, a `>` redirect) against the jail's write grants;
- each file read (Read) against the jail's read grants.

Run it with the Python that has cotf installed, so the policy judged is the one
that ships. The counts are what the history did, not what an agent will do next:
a refusal the agent never hit before can still happen.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

Grants = dict[str, list[Path]]

_CONTROL = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;", "{", "}"}
_WRITE_REDIRECTS = {">", ">>", "&>", "&>>", ">|"}
_INPUT_REDIRECTS = {"<", "<<", "<<<", "<&", ">&"}
# Words that run the next word as the command. `timeout` also takes a duration.
_WRAPPERS = {"env", "command", "exec", "nohup", "time", "sudo", "nice"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGNMENTS = re.compile(
    r"(?:^|[\s;&|])([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_DEFAULT_EXPANSION = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}]*)\}")
_FILE_WRITERS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}


@dataclass
class Command:
    """One simple command: its words, and where its output redirects write."""

    argv: list[str]
    writes: list[str] = field(default_factory=list)


@dataclass
class Tally:
    calls: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    refused: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    blocked_writes: dict[str, int] = field(default_factory=Counter)
    blocked_reads: dict[str, int] = field(default_factory=Counter)


# --- splitting a Bash call into commands ---


def _without_heredocs(script: str) -> list[str]:
    """The lines of `script` with every heredoc body removed.

    A body is data. Read as commands, an earlier replay reported `SELECT` and
    `f.write` as missing binaries.
    """
    kept: list[str] = []
    pending: list[str] = []
    for line in script.splitlines():
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        kept.append(line)
        pending = [match.group(2) for match in _HEREDOC.finditer(line)]
    return kept


def _logical_lines(script: str) -> list[str]:
    return "\n".join(_without_heredocs(script)).replace("\\\n", " ").splitlines()


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _command_word(argv: list[str]) -> list[str]:
    """`argv` from its real command word: past assignments and wrappers."""
    words = list(argv)
    while words:
        if _ASSIGNMENT.match(words[0]) or words[0] in _WRAPPERS:
            words.pop(0)
        elif words[0] == "timeout":
            words = words[2:]
        else:
            break
    return words


def _commands_on(line: str) -> list[Command]:
    found: list[Command] = []
    current = Command(argv=[])
    tokens = iter(_tokens(line))
    for token in tokens:
        if token in _CONTROL:
            found.append(current)
            current = Command(argv=[])
        elif token in _WRITE_REDIRECTS or token in _INPUT_REDIRECTS:
            _redirect(current, token, next(tokens, ""))
        else:
            current.argv.append(token)
    found.append(current)
    return found


def _redirect(command: Command, operator: str, target: str) -> None:
    # `2> err` tokenises as `2`, `>`, `err`: the digit is the fd, not an argument.
    if command.argv and command.argv[-1].isdigit():
        command.argv.pop()
    if operator in _WRITE_REDIRECTS and target:
        command.writes.append(target)


def commands_in(script: str) -> list[Command]:
    """Every simple command in a Bash tool call, heredoc bodies excluded."""
    found: list[Command] = []
    for line in _logical_lines(script):
        for command in _commands_on(line):
            command.argv = _command_word(command.argv)
            if command.argv:
                found.append(command)
    return found


def assignments_in(script: str) -> dict[str, str]:
    """`NAME=value` assignments in the script, quotes stripped."""
    body = "\n".join(_without_heredocs(script))
    return {name: value.strip("\"'") for name, value in _ASSIGNMENTS.findall(body)}


# --- which invocations reach the broker ---


def _variable_target(word: str, variables: dict[str, str]) -> str | None:
    name = word.strip("${}")
    value = variables.get(name)
    if value is None:
        return None
    default = _DEFAULT_EXPANSION.search(value)
    return default.group(1) if default else value


def invocation(
    command: Command, names: set[str], variables: dict[str, str]
) -> tuple[str, str] | None:
    """(tool, how it was named) when `command` runs a brokered tool, else None.

    `bare` resolves through PATH to the shim. `path` and `variable` name a file,
    which bypasses the shim and runs the real binary with its real credential.
    """
    word = command.argv[0]
    if word.startswith("$"):
        target = _variable_target(word, variables)
        base = os.path.basename(target) if target else ""
        return (base, "variable") if base in names else None
    if "/" in word:
        base = os.path.basename(word)
        return (base, "path") if base in names else None
    return (word, "bare") if word in names else None


def verdict(tool: Any, args: list[str], cwd: Path | str) -> str:
    """What the broker answers for `args`, checked in the order `_run` checks."""
    from claude_on_the_fly import commands

    if not commands.allowed_command(tool, args):
        if commands.refused_as_write(tool, args):
            return "write on read-only"
        if commands.hidden_by_a_leading_flag(tool, args):
            return "flag before subcommand"
        return "not allowlisted"
    if commands.refuses_readback(tool, args):
        return "credential readback"
    roots = [*commands.allowed_roots(tool), Path(cwd)]
    if commands._unsafe_path_argument(args, str(cwd), roots) is not None:
        return "path argument"
    return "allowed"


def _refusal_key(tool: Any, args: list[str], reason: str) -> str:
    """The reason and the subcommand path, never an argument value.

    Leading tokens alone cannot tell `repo delete` from the repo name after it,
    so the path is cut at the depth the tool's own policy ever looks: its
    deepest allow, read-only or readback prefix.
    """
    from claude_on_the_fly import commands

    prefixes = [*tool.allow, *tool.allow_read_only, *tool.readback]
    depth = max((len(prefix) for prefix in prefixes), default=1)
    path = commands.leading_tokens(args, boolean_flags=tool.boolean_flags)
    return f"{reason}: {' '.join(path[:depth])}".rstrip(": ")


# --- file access against the jail's grants ---


def _under(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def writable(path: Path, grants: Grants) -> bool:
    if _under(path, grants.get("write_denied", [])):
        return False
    return _under(path, grants.get("read_write", []))


def readable(path: Path, grants: Grants) -> bool:
    if _under(path, grants.get("masked", [])):
        return False
    if _under(path, [*grants.get("read_only", []), *grants.get("read_write", [])]):
        return True
    return not _under(path, grants.get("opaque", []))


def group(path: Path, home: Path) -> str:
    """The directory a blocked path is reported under: two levels deep."""
    if path == home or home in path.parents:
        return "~/" + "/".join(path.relative_to(home).parts[:2])
    return "/" + "/".join(path.parts[1:3])


def _resolve(target: str, cwd: Path, home: Path) -> Path:
    """Where the write lands. Resolved through links, because the kernel follows
    them before the jail's mounts decide: a link the jail mounts or recreates
    points at the same place inside it as out here."""
    expanded = re.sub(r"^(~|\$HOME|\$\{HOME\})(?=/|$)", str(home), target)
    return Path(os.path.realpath(cwd / expanded))


# --- one transcript ---


def _tool_uses(transcript: Path) -> Iterator[tuple[str | None, str, dict]]:
    for line in transcript.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "assistant":
            continue
        for item in record.get("message", {}).get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield record.get("cwd"), item.get("name", ""), item.get("input") or {}


def _replay_bash(
    script: str,
    cwd: Path,
    tools: dict[str, Any],
    grants: Grants,
    tally: Tally,
    home: Path,
) -> None:
    variables = assignments_in(script)
    for command in commands_in(script):
        for target in command.writes:
            _check_write(_resolve(target, cwd, home), grants, tally, home)
        found = invocation(command, set(tools), variables)
        if found is None:
            continue
        name, kind = found
        tally.calls[name][kind] += 1
        args = command.argv[1:]
        reason = verdict(tools[name], args, cwd)
        if reason != "allowed":
            tally.refused[name][_refusal_key(tools[name], args, reason)] += 1


def _check_write(path: Path, grants: Grants, tally: Tally, home: Path) -> None:
    if path.parts[:2] == ("/", "dev"):
        return
    if not writable(path, grants):
        tally.blocked_writes[group(path, home)] += 1


def _check_read(path: Path, grants: Grants, tally: Tally, home: Path) -> None:
    if not readable(path, grants):
        tally.blocked_reads[group(path, home)] += 1


def replay(
    transcript: Path,
    tools: dict[str, Any],
    grants_for: Callable[[Path], Grants],
    tally: Tally,
    *,
    home: Path,
) -> None:
    """Add one transcript's calls to `tally`."""
    for raw_cwd, name, tool_input in _tool_uses(transcript):
        cwd = Path(raw_cwd) if raw_cwd else home
        grants = grants_for(cwd)
        if name == "Bash":
            _replay_bash(
                str(tool_input.get("command", "")), cwd, tools, grants, tally, home
            )
        elif name in _FILE_WRITERS and tool_input.get(_FILE_WRITERS[name]):
            _check_write(
                _resolve(tool_input[_FILE_WRITERS[name]], cwd, home),
                grants,
                tally,
                home,
            )
        elif name == "Read" and tool_input.get("file_path"):
            _check_read(
                _resolve(tool_input["file_path"], cwd, home), grants, tally, home
            )


# --- the report ---


def _table(header: str, rows: list[str]) -> list[str]:
    if not rows:
        return ["None.", ""]
    return [header, "|" + "---|" * header.count("|", 1), *rows, ""]


def render(tally: Tally, *, transcripts: int) -> str:
    lines = [
        "# Transcript audit",
        "",
        f"{transcripts} transcripts, judged as if `sandbox.mode: jail`.",
        "",
        "## Brokered tools",
        "",
        "A `path` or `variable` call names the binary's file, so it bypasses the shim.",
        "",
    ]
    lines += _table(
        "| Tool | Bare | Path | Variable | Refused |",
        [
            f"| {tool} | {kinds.get('bare', 0)} | {kinds.get('path', 0)} "
            f"| {kinds.get('variable', 0)} | {sum(tally.refused[tool].values())} |"
            for tool, kinds in sorted(tally.calls.items())
        ],
    )
    lines += ["## Refusals", ""]
    lines += _table(
        "| Tool | Refusal | Calls |",
        [
            f"| {tool} | {reason} | {count} |"
            for tool, reasons in sorted(tally.refused.items())
            for reason, count in Counter(reasons).most_common()
        ],
    )
    for title, blocked in (
        ("Blocked writes", tally.blocked_writes),
        ("Blocked reads", tally.blocked_reads),
    ):
        lines += [f"## {title}", ""]
        lines += _table(
            "| Target | Calls |",
            [
                f"| {target} | {count} |"
                for target, count in Counter(blocked).most_common()
            ],
        )
    return "\n".join(lines)


def _transcripts(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        found += sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m claude_on_the_fly.audit", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    # Before any cotf module reads them: DATA_DIR is fixed at import, and the
    # grants below are the jail's whatever the file says.
    os.environ["COTF_DATA_DIR"] = str(args.data_dir)
    os.environ["COTF_SANDBOX"] = "jail"
    from claude_on_the_fly import commands, sandbox

    tools = {tool.name: tool for tool in commands.load_tools()}
    grants_for = cache(sandbox._linux_grants)
    transcripts = _transcripts(args.paths or [args.home / ".claude" / "projects"])
    tally = Tally()
    for transcript in transcripts:
        replay(transcript, tools, grants_for, tally, home=args.home)
    print(render(tally, transcripts=len(transcripts)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
