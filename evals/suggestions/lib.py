"""Shared plumbing for the suggestions eval.

Every path is derived from this file's location, so the harness runs from any
checkout on any machine. Nothing here reaches outside the repository except
the `claude` binary and the gitignored raw corpus.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORPUS = HERE / "corpus" / "cases.jsonl"
RAW = HERE / "raw" / "cases.raw.jsonl"
VARIANTS = HERE / "variants"
RUNS = HERE / "runs"

BLOCK_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL)


def digest(text: str) -> str:
    """A short content hash. Two runs that share one are comparable."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def load_variant(name: str) -> str:
    """Resolve a variant name to its template text.

    `live` reads whatever `orchestrator.py` ships today, so a run always
    measures the code as it stands. Every other name is a frozen file, which
    is what makes an old result still mean something after the code moves on.
    """
    if name == "live":
        sys.path.insert(0, str(ROOT / "src"))
        from claude_on_the_fly.orchestrator import SUGGESTIONS_TEMPLATE

        return SUGGESTIONS_TEMPLATE
    path = VARIANTS / f"{name}.txt"
    if not path.exists():
        known = sorted(p.stem for p in VARIANTS.glob("*.txt"))
        raise SystemExit(f"no variant {name!r}; have: {', '.join(known)}, live")
    return path.read_text().rstrip("\n")


# Every field a scorer or the runner reads. A corpus rebuild that renames one
# used to surface only once a live run had started spending, so it is checked
# on load instead.
REQUIRED = (
    "outcome",
    "expected_gate",
    "strength",
    "offered_labels",
    "clicked_label",
    "clicked_position",
    "prior_user_text",
    "reply_text",
    "alternative_text",
)


def load_cases(path: Path | None = None) -> list[dict]:
    path = path or CORPUS
    if not path.exists():
        raise SystemExit(f"no corpus at {path}; see evals/suggestions/README.md")
    cases = [json.loads(line) for line in path.read_text().splitlines() if line]
    for case in cases:
        # The raw corpus is keyed by permalink; the redacted one already
        # carries the hash of it. Either way a case has a stable id.
        case.setdefault("case_id", digest(case.get("permalink", "")))
    missing = sorted(
        {field for case in cases for field in REQUIRED if field not in case}
    )
    if missing:
        raise SystemExit(f"{path} is missing required field(s): {', '.join(missing)}")
    return cases


def corpus_digest(cases: list[dict]) -> str:
    return digest("".join(sorted(c["case_id"] for c in cases)))


# `claude -p` ships its default system prompt, its tool definitions and every
# CLAUDE.md it can find. Measured at 65,581 input tokens against a 450-token
# judge prompt. Replacing the system prompt and dropping the tools takes that
# to 56,348, which is a third off every call. The rest is the memory files,
# which only an empty config directory removes, and that loses authentication.
LEAN = [
    "--exclude-dynamic-system-prompt-sections",
    "--allowed-tools",
    "",
]
CLASSIFIER = (
    "You are a precise classifier. Answer with one JSON object and nothing else."
)


def ask_model(
    prompt: str,
    model: str = "sonnet",
    timeout: int = 300,
    system: str = CLASSIFIER,
) -> str:
    """One `claude -p` call. Returns stdout, or an empty string on failure."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", model, "--system-prompt", system, *LEAN],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        print(f"claude failed: {result.stderr[:200]}", file=sys.stderr)
        return ""
    return result.stdout


# Codex runs on a separate quota and carries 17,817 tokens of overhead against
# claude's 56,348, measured on the same judge prompt. Generation stays on
# claude because a claude prompt is what these runs measure. Judging moves
# here: it is cheaper, and a judge from another model family does not share
# the bias of the thing it scores.
def ask_codex(prompt: str, schema: Path | None = None, timeout: int = 300) -> str:
    """One `codex exec` call. Returns the agent's final message."""
    command = ["codex", "exec", prompt, "--json", "--sandbox", "read-only"]
    command += ["--skip-git-repo-check"]
    if schema:
        command += ["--output-schema", str(schema)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(f"codex failed: {result.stderr[:200]}", file=sys.stderr)
        return ""
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {})
        if item.get("type") == "agent_message":
            return item.get("text", "")
    return ""


def parse_block(text: str) -> list[str] | None:
    """The last suggestions block in a reply, as a list. None when absent."""
    matches = BLOCK_RE.findall(text)
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1].strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


def parse_json_object(text: str) -> dict | None:
    """The first JSON object in a model reply. None when it emitted prose."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
