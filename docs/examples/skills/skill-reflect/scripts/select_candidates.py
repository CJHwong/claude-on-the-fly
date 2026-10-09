#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Pick the cotf sessions most likely to hold a skill lesson.

How to make it work:
  1. Install uv (https://docs.astral.sh/uv/). It installs pyyaml on the first run.
  2. Write ~/.claude-on-the-fly/skill-reflect.yaml; the Configuration section of
     ../SKILL.md lists every key.
  3. The skill runs this script in its step 1. To run it by hand:
       uv run --script select_candidates.py --days 7 --out /tmp/sr/candidates.json
     It prints the counts and writes candidates.json. It sends nothing.

Deterministic and model-free. Reads Codex rollouts (through the Codex thread index) and
Claude Code transcripts whose working directory is a cotf workspace, plus the per-job
cron logs, and writes candidates.json for the skill-reflect skill. The model never
reads a full transcript: it reads the excerpts this script cuts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

HOME = Path.home()
DEFAULT_CODEX_HOME = HOME / ".codex"
DEFAULT_CLAUDE_HOME = HOME / ".claude"
DEFAULT_COTF_HOME = HOME / ".claude-on-the-fly"

FROM_ID = re.compile(
    r"\[from(?:-id)?: ([^\]]+)\]"
)  # `[from: <name>]` before cotf tagged ids
OUTBOX_CUT = "<cotf-outbox>"
EXIT_CODE = re.compile(r'exit_code\\?"?\s*[:=]\s*(\d+)|Process exited with code (\d+)')
CLAUDE_EXIT = re.compile(r"^Exit code (\d+)")
CMD_IN_JS = re.compile(r'exec_command\(\{\s*cmd\s*:\s*"((?:[^"\\]|\\.)*)"')
SKILL_PATH = re.compile(r"((?:~|/)[^\s'\"`;|&()]*/SKILL\.md)")
CRON_BLOCK = re.compile(
    r"^--- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) reply \((FAILED|done)\) ---$"
)

# Phrases a person uses when the agent got it wrong. Bare "no", "again", "還是" and "又" are
# left out: in a 14-day sample they mostly opened ordinary requests. The model filters the rest.
CORRECTION = re.compile(
    r"^\s*no[,.!]|\b(that'?s (?:wrong|not)|not what i|why did you|why didn'?t you|you (?:missed|forgot|ignored)|"
    r"should have|i (?:said|asked|told you)|instead of|too (?:long|verbose|much)|stop (?:doing|adding|using)|"
    r"don'?t (?:do|add|use|send|post|make)|wrong (?:file|one|place|ticket|channel|format)|redo|revert)\b|"
    r"不要再|不是這樣|不是这样|錯了|错了|不對|不对|為什麼你|为什么你|重做|我說|我说|應該要|应该要",
    re.IGNORECASE,
)
EXPLICIT_ASK = re.compile(
    r"\b(remember (?:this|that)|update (?:the|your) skill|add (?:this|it) to (?:the|your) skill|"
    r"from now on|next time|going forward)\b|記住|记住|記得|以後|以后|下次",
    re.IGNORECASE,
)

EXCERPT_RADIUS = 4  # timeline items kept on each side of a signal
EXCERPT_CHARS = 6000  # per session
TEXT_CHARS = {"user": 500, "assistant": 300, "tool": 240}
CLAUDE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
INVENTORY_SKIP = ("/.trash/", "/node_modules/", "/.git/")


@dataclass(frozen=True)
class Config:
    """The parts of skill-reflect.yaml that decide who owns a skill."""

    soul_root: Path | None = None
    upstream_marketplaces: tuple[str, ...] = ()
    third_party: tuple[str, ...] = ()  # prefixes under <soul_root>/skills


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise SystemExit(
            f"error: {path} not found; write it first (see Configuration in the skill's SKILL.md)"
        )
    data = yaml.safe_load(path.read_text()) or {}
    soul = data.get("soul_root")
    return Config(
        soul_root=Path(soul).expanduser() if soul else None,
        upstream_marketplaces=tuple(data.get("upstream_marketplaces") or ()),
        third_party=tuple(data.get("third_party_skills") or ()),
    )


@dataclass
class Item:
    kind: str  # user | assistant | tool
    text: str
    speaker: str = ""
    exit_code: int | None = None


@dataclass
class Session:
    thread_id: str
    transcript: str
    cwd: str
    kind: str  # slack | cron
    where: str
    backend: str = "codex"  # codex | claude
    timeline: list[Item] = field(default_factory=list)
    skills_read: Counter = field(default_factory=Counter)
    skills_edited: Counter = field(default_factory=Counter)
    signals: list[dict] = field(default_factory=list)
    score: float = 0.0


def owner_of(path: str, config: Config) -> str:
    """local, upstream, third-party or unknown, decided by where the SKILL.md lives."""
    full = os.path.expanduser(path)
    for market in config.upstream_marketplaces:
        # The installed plugin cache, or a working clone of the marketplace repo.
        if f"/plugins/cache/{market}/" in full or f"/{market}/plugins/" in full:
            return "upstream"
    if "/plugins/cache/" in full:
        return "third-party"
    relative = under_skills_dir(full, config)
    if relative is None:
        return "unknown"
    return "third-party" if relative.startswith(config.third_party) else "local"


def under_skills_dir(path: str, config: Config) -> str | None:
    """The path relative to <soul_root>/skills, following symlinks such as ~/.claude/skills."""
    if not config.soul_root:
        return None
    root = config.soul_root / "skills"
    for base in (root, root.resolve()):
        for candidate in (Path(path), Path(path).resolve()):
            if candidate.is_relative_to(base):
                return candidate.relative_to(base).as_posix()
    return None


VERSION = re.compile(r"^\d+\.\d+")
NOT_A_PLUGIN = {".codex", ".claude", ".agents", "~", "/"}


def skill_name(path: str, config: Config) -> str:
    """`<plugin>/[<version>/]skills/<name>/SKILL.md` -> `<plugin>:<name>`; a top-level skill keeps its bare name.

    Matches the names Codex's skills index and Claude's Skill tool use, so reads and the
    inventory line up."""
    parts = Path(path).parts
    name = parts[-2] if len(parts) >= 2 else path
    if len(parts) < 4 or parts[-3] != "skills":
        return name
    owner_dir = parts[-4]
    if VERSION.match(owner_dir) and len(parts) >= 5:
        owner_dir = parts[-5]
    generic = NOT_A_PLUGIN | ({config.soul_root.name} if config.soul_root else set())
    return name if owner_dir in generic else f"{owner_dir}:{name}"


def classify_cwd(cwd: str, workspaces: Path) -> tuple[str, str] | None:
    """('slack', 'dm/U123') or ('cron', '<job dir>'), or None when not a cotf session."""
    try:
        rel = Path(cwd).relative_to(workspaces)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "slack":
        return "slack", f"{parts[1]}/{parts[2]}"
    if len(parts) == 2 and parts[0] == "slack":
        # Before the shared workspace, each thread had `slack/<label>-<thread ts>[-<micro>]`.
        return "slack", re.sub(r"(-\d+)+$", "", parts[1])
    # `schedule/` and `jobs/` are older names of `cron/`.
    if len(parts) >= 2 and parts[0] in ("cron", "schedule", "jobs"):
        # A producer job runs each item in `<job>_<key>`; prompt jobs share `__runs/<id>`.
        return "cron", parts[1] if parts[1].startswith("__") else parts[1].split(
            "_", 1
        )[0]
    return None


def codex_manifest(db: Path, since: datetime, workspaces: Path) -> list[Session]:
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select id, rollout_path, cwd from threads "
        "where updated_at >= ? and cwd like ? and coalesce(thread_source,'') != 'subagent' "
        "order by updated_at",
        (int(since.timestamp()), f"{workspaces}/%"),
    ).fetchall()
    con.close()
    sessions = []
    for thread_id, rollout, cwd in rows:
        where = classify_cwd(cwd, workspaces)
        if where:
            sessions.append(Session(thread_id, rollout, cwd, where[0], where[1]))
    return sessions


def claude_manifest(projects: Path, since: datetime, workspaces: Path) -> list[Session]:
    """Claude Code keeps one `<session>.jsonl` per run in a folder named after its cwd.

    Subagent runs sit one level deeper and are not read."""
    if not projects.is_dir():
        return []
    prefix = re.sub(r"[^A-Za-z0-9]", "-", str(workspaces))
    sessions = []
    for folder in sorted(p for p in projects.iterdir() if p.name.startswith(prefix)):
        for path in sorted(folder.glob("*.jsonl")):
            if path.stat().st_mtime < since.timestamp():
                continue
            cwd = first_cwd(path)
            where = classify_cwd(cwd, workspaces) if cwd else None
            if where:
                sessions.append(
                    Session(path.stem, str(path), cwd, where[0], where[1], "claude")
                )
    return sessions


def first_cwd(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            cwd = json_object(line).get("cwd")
            if cwd:
                return cwd
    return ""


def json_object(line: str) -> dict:
    """One transcript line, or {} when it is not a JSON object (a torn write, a bare list)."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return record if isinstance(record, dict) else {}


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def real_user_text(raw: str) -> tuple[str, str] | None:
    """The person's own words from a cotf-tagged turn; None for injected context."""
    marks = list(FROM_ID.finditer(raw))
    if not marks:
        return None
    last = marks[-1]
    body = raw[last.end() :]
    body = body.split(OUTBOX_CUT, 1)[0]
    body = re.sub(r"^\s*\[display: [^\]]*\]\s*", "", body)
    return last.group(1), body.strip()


def ingest_text(session: Session, role: str, raw: str) -> None:
    if role == "assistant" and raw.strip():
        session.timeline.append(Item("assistant", raw.strip()))
        return
    if role != "user":
        return
    found = real_user_text(raw)
    if found and found[1]:
        session.timeline.append(Item("user", found[1], speaker=found[0]))


def record_skill_paths(
    session: Session, text: str, edited: bool, config: Config
) -> None:
    target = session.skills_edited if edited else session.skills_read
    for path in set(SKILL_PATH.findall(text)):
        if any(
            ch in path for ch in "{}*?$"
        ):  # a shell glob or brace list, not one skill
            continue
        target[(skill_name(path, config), owner_of(path, config))] += 1


def parse_session(session: Session, config: Config, inventory: dict[str, str]) -> None:
    if session.backend == "claude":
        parse_claude(session, config, inventory)
    else:
        parse_rollout(session, config)


# --- Codex rollouts ---------------------------------------------------------------------


def commands_in(payload: dict) -> list[str]:
    """Shell commands from a Codex tool call: JS `exec` wrapper or plain function call."""
    if payload.get("type") == "custom_tool_call":
        found = CMD_IN_JS.findall(payload.get("input", ""))
        return [unescape_js(c) for c in found]
    args = json_object(payload.get("arguments") or "{}")  # a function_call
    cmd = args.get("cmd") or args.get("command")
    return [" ".join(cmd) if isinstance(cmd, list) else str(cmd)] if cmd else []


def unescape_js(literal: str) -> str:
    """A double-quoted JS string body; its escapes are JSON's for every command seen."""
    try:
        return json.loads(f'"{literal}"')
    except json.JSONDecodeError:
        return literal


def is_patch(payload: dict) -> bool:
    return (
        "apply_patch" in payload.get("input", "")
        or payload.get("name") == "apply_patch"
    )


def parse_rollout(session: Session, config: Config) -> None:
    pending: dict[str, int] = {}  # call_id -> timeline index of its tool item
    with open(session.transcript, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            record = json_object(line)
            if record.get("type") != "response_item":
                continue
            ingest(session, record.get("payload") or {}, pending, config)


def ingest(
    session: Session, payload: dict, pending: dict[str, int], config: Config
) -> None:
    kind = payload.get("type")
    if kind == "message":
        ingest_text(session, payload.get("role", ""), text_of(payload.get("content")))
    elif kind in ("custom_tool_call", "function_call"):
        ingest_call(session, payload, pending, config)
    elif kind in ("custom_tool_call_output", "function_call_output"):
        index = pending.pop(payload.get("call_id", ""), None)
        match = EXIT_CODE.search(
            text_of(payload.get("output")) or str(payload.get("output", ""))
        )
        if index is not None and match:
            session.timeline[index].exit_code = int(match.group(1) or match.group(2))


def ingest_call(
    session: Session, payload: dict, pending: dict[str, int], config: Config
) -> None:
    raw_input = payload.get("input", "") or payload.get("arguments", "")
    edited = is_patch(payload)
    record_skill_paths(session, raw_input, edited, config)
    for cmd in commands_in(payload) or ([raw_input] if edited else []):
        session.timeline.append(Item("tool", cmd.strip()))
        pending[payload.get("call_id", "")] = len(session.timeline) - 1


# --- Claude Code transcripts ------------------------------------------------------------


def parse_claude(session: Session, config: Config, inventory: dict[str, str]) -> None:
    pending: dict[str, int] = {}  # tool_use id -> timeline index of its tool item
    with open(session.transcript, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            record = json_object(line)
            role = record.get("type")
            if record.get("isSidechain") or role not in ("user", "assistant"):
                continue
            content = (record.get("message") or {}).get("content")
            blocks = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else content
            )
            for block in blocks or []:
                if isinstance(block, dict):
                    ingest_block(session, role, block, pending, config, inventory)


def ingest_block(
    session: Session,
    role: str,
    block: dict,
    pending: dict[str, int],
    config: Config,
    inventory: dict[str, str],
) -> None:
    kind = block.get("type")
    if kind == "text":
        ingest_text(session, role, block.get("text", ""))
    elif kind == "tool_use":
        ingest_tool_use(session, block, pending, config, inventory)
    elif kind == "tool_result":
        index = pending.pop(block.get("tool_use_id", ""), None)
        if index is not None:
            session.timeline[index].exit_code = claude_exit_code(block)


def ingest_tool_use(
    session: Session,
    block: dict,
    pending: dict[str, int],
    config: Config,
    inventory: dict[str, str],
) -> None:
    name, args = block.get("name"), block.get("input") or {}
    if name == "Bash" and args.get("command"):
        command = str(args["command"])
        record_skill_paths(session, command, False, config)
        session.timeline.append(Item("tool", command.strip()))
        pending[block.get("id", "")] = len(session.timeline) - 1
    elif name == "Skill" and args.get("skill"):
        skill = inventory_name(str(args["skill"]), inventory)
        session.skills_read[(skill, inventory.get(skill, "unknown"))] += 1
    elif name == "Read" or name in CLAUDE_EDIT_TOOLS:
        path = str(args.get("file_path", ""))
        record_skill_paths(session, path, name in CLAUDE_EDIT_TOOLS, config)


def inventory_name(skill: str, inventory: dict[str, str]) -> str:
    """Claude's Skill tool calls a nested `<group>/skills/<name>` skill by its bare name."""
    if skill in inventory:
        return skill
    matches = [name for name in inventory if name.rsplit(":", 1)[-1] == skill]
    return matches[0] if len(matches) == 1 else skill


def claude_exit_code(block: dict) -> int:
    """Claude Code reports a failed command as `Exit code N`; any other error counts as 1."""
    match = CLAUDE_EXIT.match(text_of(block.get("content")))
    if match:
        return int(match.group(1))
    return 1 if block.get("is_error") else 0


# --- Signals and ranking ----------------------------------------------------------------


def command_key(cmd: str) -> str:
    """The first two words, so a retried `gh api ...` matches its failed attempt."""
    words = re.findall(r"[\w./:-]+", cmd)
    return " ".join(words[:2])


def find_signals(session: Session) -> None:
    failed: dict[str, int] = {}
    for index, item in enumerate(session.timeline):
        if item.kind == "user":
            if EXPLICIT_ASK.search(item.text):
                session.signals.append({"type": "explicit_ask", "at": index})
            elif CORRECTION.search(item.text) and has_prior_assistant(session, index):
                session.signals.append({"type": "correction", "at": index})
        if item.kind != "tool" or item.exit_code is None:
            continue
        key = command_key(item.text)
        if item.exit_code != 0:
            failed.setdefault(key, index)
        elif key in failed:
            session.signals.append(
                {"type": "recovered_error", "at": index, "failed_at": failed.pop(key)}
            )


def has_prior_assistant(session: Session, index: int) -> bool:
    return any(item.kind == "assistant" for item in session.timeline[:index])


# Weight and cap per signal type: a 1,000-call session with 30 hits must not crowd out
# ten short sessions that each hold one clean correction.
WEIGHTS = {
    "explicit_ask": (4.0, 3),
    "correction": (2.0, 4),
    "recovered_error": (1.0, 4),
}
PER_WHERE = 2  # candidates kept per DM, channel or cron job
CRON_SLOTS = 10  # of --top, reserved for cron sessions


def score(session: Session) -> None:
    hits = Counter(s["type"] for s in session.signals)
    base = sum(weight * min(hits[kind], cap) for kind, (weight, cap) in WEIGHTS.items())
    tools = sum(1 for item in session.timeline if item.kind == "tool")
    session.score = base + min(tools / 50, 2.0) if base else 0.0


def pick(sessions: list[Session], top: int) -> list[Session]:
    """Best first, at most PER_WHERE per conversation, CRON_SLOTS kept for cron when it has any."""
    seen: Counter = Counter()
    chosen: dict[str, list[Session]] = {"slack": [], "cron": []}
    for session in sorted((s for s in sessions if s.score > 0), key=lambda s: -s.score):
        if seen[(session.kind, session.where)] >= PER_WHERE:
            continue
        seen[(session.kind, session.where)] += 1
        chosen[session.kind].append(session)
    cron = chosen["cron"][: min(CRON_SLOTS, top)]
    slack = chosen["slack"][: top - len(cron)]
    return sorted(slack + cron, key=lambda s: -s.score)


def clip(item: Item) -> str:
    limit = TEXT_CHARS[item.kind]
    text = item.text if len(item.text) <= limit else item.text[:limit] + "…"
    if item.kind == "tool":
        code = "?" if item.exit_code is None else item.exit_code
        return f"[tool exit={code}] {text}"
    if item.kind == "user":
        return f"[user {item.speaker}] {text}"
    return f"[agent] {text}"


def excerpts(session: Session) -> list[dict]:
    spans: list[tuple[int, int, str]] = []
    for signal in session.signals:
        start = max(0, signal.get("failed_at", signal["at"]) - EXCERPT_RADIUS)
        spans.append((start, signal["at"] + EXCERPT_RADIUS + 1, signal["type"]))
    merged: list[list] = []
    for start, end, kind in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].add(kind)
            continue
        merged.append([start, end, {kind}])
    out, budget = [], EXCERPT_CHARS
    for start, end, kinds in merged:
        lines = [clip(item) for item in session.timeline[start:end]]
        text = "\n".join(lines)
        if budget <= 0:
            break
        out.append({"signals": sorted(kinds), "text": text[:budget]})
        budget -= len(text)
    return out


# --- Cron logs --------------------------------------------------------------------------


def cron_recoveries(logs: Path, since: datetime) -> list[dict]:
    """Jobs whose per-job log shows a FAILED reply followed later by a done reply.

    Only the cron logs carry the verdict, and cotf keeps them 30 days. Rollouts were
    checked as a longer source: a run cut off before task_complete is a reliable
    failure, but it covers under half of them, and most failures are CLI errors or
    restarts that step 2 discards anyway. The lessons inside a failing run already
    reach the review as recovered_error signals over the full window.
    """
    events: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
    cutoff = since.astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )  # cron logs stamp host-local time
    for path in sorted(logs.glob("cron-*.log")):
        job = job_of_log(path.name)
        if not job:
            continue
        for stamp, status, body in cron_blocks(path):
            if stamp >= cutoff:
                events[job].append((stamp, status, body))
    found = []
    for job, runs in events.items():
        runs.sort()
        failure = next((run for run in runs if run[1] == "FAILED"), None)
        if not failure:
            continue
        later_ok = next(
            (run for run in runs if run[0] > failure[0] and run[1] == "done"), None
        )
        fails = sum(1 for run in runs if run[1] == "FAILED")
        found.append(
            {
                "job": job,
                "failures": fails,
                "runs": len(runs),
                "first_failed_at": failure[0],
                "recovered_at": later_ok[0] if later_ok else None,
                "failure_excerpt": "\n".join(failure[2][:15])[:1500],
            }
        )
    return sorted(found, key=lambda entry: -entry["failures"])


def job_of_log(name: str) -> str | None:
    """`cron-<job>-<host>-<date>.log` -> `<job>`; the daemon's own `cron-<host>-…` -> None."""
    match = re.match(r"^cron-(.+)-([A-Za-z0-9_]+)-(\d{4}-\d{2}-\d{2})\.log$", name)
    return match.group(1) if match else None


def cron_blocks(path: Path):
    stamp, status, body = None, None, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        header = CRON_BLOCK.match(line)
        if header:
            if stamp:
                yield stamp, status, body
            stamp, status, body = header.group(1), header.group(2), []
        elif stamp:
            body.append(line)
    if stamp:
        yield stamp, status, body


# --- Skill inventory and usage ----------------------------------------------------------


def skill_inventory(
    config: Config, codex_home: Path, claude_home: Path
) -> dict[str, str]:
    """Every skill on disk the agent could load: the soul repo's own, then both plugin caches."""
    roots = [home / "plugins" / "cache" for home in (codex_home, claude_home)]
    if config.soul_root:
        roots.insert(0, config.soul_root / "skills")
    inventory: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            text = path.as_posix()
            if any(skip in text for skip in INVENTORY_SKIP):
                continue
            inventory.setdefault(skill_name(text, config), owner_of(text, config))
    return inventory


def usage_report(sessions: list[Session], inventory: dict[str, str]) -> dict:
    reads: Counter = Counter()
    for session in sessions:
        for (name, _owner), _count in session.skills_read.items():
            reads[name] += 1
    used = {name: count for name, count in reads.most_common()}
    unused = sorted(
        name
        for name, owner in inventory.items()
        if owner in ("local", "upstream") and name not in reads
    )
    return {
        "sessions_reading": used,
        "unused_in_window": unused,
        "inventory": inventory,
    }


def load_cursor(state: Path | None) -> datetime | None:
    if not state or not state.exists():
        return None
    value = json.loads(state.read_text()).get("last_run_at")
    return datetime.fromisoformat(value) if value else None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=float, default=7, help="window when no cursor exists"
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="skill-reflect state file; its last_run_at wins over --days",
    )
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, help="default: <cotf-home>/skill-reflect.yaml"
    )
    parser.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    parser.add_argument("--claude-home", type=Path, default=DEFAULT_CLAUDE_HOME)
    parser.add_argument("--cotf-home", type=Path, default=DEFAULT_COTF_HOME)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict:
    config = load_config(args.config or args.cotf_home / "skill-reflect.yaml")
    now = datetime.now(UTC)
    since = load_cursor(args.state) or now - timedelta(days=args.days)
    workspaces = args.cotf_home / "workspaces"
    inventory = skill_inventory(config, args.codex_home, args.claude_home)
    sessions = codex_manifest(
        args.codex_home / "state_5.sqlite", since, workspaces
    ) + claude_manifest(args.claude_home / "projects", since, workspaces)
    errors = 0
    for session in sessions:
        try:
            parse_session(session, config, inventory)
        except OSError:
            errors += 1
            continue
        find_signals(session)
        score(session)
    ranked = pick(sessions, args.top)
    signal_totals = Counter(sig["type"] for s in sessions for sig in s.signals)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {
            "since": since.isoformat(timespec="seconds"),
            "until": now.isoformat(timespec="seconds"),
        },
        "counts": {
            "sessions_scanned": len(sessions),
            "sessions_by_kind": dict(Counter(s.kind for s in sessions)),
            "sessions_by_backend": dict(Counter(s.backend for s in sessions)),
            "sessions_with_signals": sum(1 for s in sessions if s.signals),
            "signals": dict(signal_totals),
            "unreadable_transcripts": errors,
            "candidates": len(ranked),
        },
        "candidates": [candidate(s) for s in ranked],
        "cron_recoveries": cron_recoveries(args.cotf_home / "logs", since),
        "skill_usage": usage_report(sessions, inventory),
    }


def candidate(session: Session) -> dict:
    return {
        "thread_id": session.thread_id,
        "backend": session.backend,
        "kind": session.kind,
        "where": session.where,
        "speakers": sorted({item.speaker for item in session.timeline if item.speaker}),
        "score": round(session.score, 2),
        "signals": dict(Counter(sig["type"] for sig in session.signals)),
        "tool_calls": sum(1 for item in session.timeline if item.kind == "tool"),
        "skills_read": [
            {"skill": n, "owner": o, "reads": c}
            for (n, o), c in session.skills_read.most_common()
        ],
        "skills_edited": [
            {"skill": n, "owner": o, "edits": c}
            for (n, o), c in session.skills_edited.most_common()
        ],
        "excerpts": excerpts(session),
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = run(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
