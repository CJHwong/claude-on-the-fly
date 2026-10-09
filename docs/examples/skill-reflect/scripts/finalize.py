#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Validate skill-reflect proposals against the evidence, render the digest, record state.

The model writes proposals.json. This script decides what survives: every evidence id
must exist in candidates.json, the threshold must hold, the target's owner must allow
the action, and an earlier rejection must not come back. It renders digest.md (the Slack
message) and digest-detail.md (the attachment with every proposed text).

How to make it work:
  1. Install uv (https://docs.astral.sh/uv/). No other dependency.
  2. The skill runs this script in its steps 5 and 7, after select_candidates.py has
     written candidates.json and the model has written proposals.json into the same
     run directory. To run it by hand:
       uv run --script finalize.py --run-dir /tmp/sr --mode dry-run
  --record writes the state file and is meant for the skill's live mode only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ACTIONS = {"patch", "new-skill", "profile-note", "note"}
OWNER_ACTIONS = {"local": {"patch"}, "upstream": {"patch"}, "third-party": {"note"}}
REQUIRED = ("target", "action", "title", "rule", "why", "evidence")


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def rule_key(target: str, rule: str) -> str:
    norm = re.sub(r"\W+", " ", rule.lower()).strip()
    return hashlib.sha1(f"{target}|{norm}".encode()).hexdigest()[:12]


class Evidence:
    """What candidates.json can vouch for."""

    def __init__(self, candidates: dict):
        self.threads = {c["thread_id"]: c for c in candidates.get("candidates", [])}
        self.jobs = {f"job:{r['job']}" for r in candidates.get("cron_recoveries", [])}
        self.inventory = candidates.get("skill_usage", {}).get("inventory", {})

    def unknown(self, ids: list[str]) -> list[str]:
        return [i for i in ids if i not in self.threads and i not in self.jobs]

    def has_explicit_ask(self, ids: list[str]) -> bool:
        return any(
            self.threads.get(i, {}).get("signals", {}).get("explicit_ask") for i in ids
        )

    def people(self, ids: list[str]) -> int:
        return len(
            {who for i in ids for who in self.threads.get(i, {}).get("speakers", [])}
        )


def problem(prop: dict, ev: Evidence, run_dir: Path, rejected: dict) -> str | None:
    """The reason to drop a proposal, or None when it stands."""
    missing = [k for k in REQUIRED if not prop.get(k)]
    if missing:
        return f"missing {', '.join(missing)}"
    if prop["action"] not in ACTIONS:
        return f"unknown action {prop['action']}"
    ids = list(dict.fromkeys(prop["evidence"]))
    if unknown := ev.unknown(ids):
        return f"evidence not in candidates: {', '.join(unknown)}"
    if len(ids) < 2 and not (prop.get("explicit_ask") and ev.has_explicit_ask(ids)):
        return "fewer than 2 sessions and no explicit ask in the evidence"
    if reason := target_problem(prop, ev):
        return reason
    if (
        prop["action"] != "note"
        and not (run_dir / prop.get("patch_file", "")).is_file()
    ):
        return f"patch file not found: {prop.get('patch_file')}"
    if seen := rejected.get(rule_key(prop["target"], prop["rule"])):
        return f"rejected before ({seen})"
    return None


def target_problem(prop: dict, ev: Evidence) -> str | None:
    target, action = prop["target"], prop["action"]
    if action == "new-skill":
        name = target.removeprefix("new:")
        ok = target.startswith("new:") and name not in ev.inventory
        return (
            None if ok else f"new-skill target must be new:<unused name>, got {target}"
        )
    if action == "profile-note":
        return (
            None
            if target.startswith("profile:")
            else f"profile-note target must be profile:<id>, got {target}"
        )
    owner = ev.inventory.get(target)
    if owner is None:
        return f"target {target} is not an installed skill"
    if action not in OWNER_ACTIONS.get(owner, set()):
        return f"{action} not allowed on a {owner} skill"
    return None


def owner_label(prop: dict, ev: Evidence) -> str:
    if prop["action"] == "new-skill":
        return "local, new skill"
    if prop["action"] == "profile-note":
        return "personal profile"
    return f"{ev.inventory.get(prop['target'], '?')}, {prop['action']}"


def validate(
    run_dir: Path, state: dict
) -> tuple[list[dict], list[dict], Evidence, dict]:
    candidates = load_json(run_dir / "candidates.json")
    if candidates is None:
        raise SystemExit(
            f"error: {run_dir / 'candidates.json'} not found; run select_candidates.py first"
        )
    proposals = load_json(run_dir / "proposals.json", {}).get("proposals", [])
    ev = Evidence(candidates)
    rejected = {
        k: v.get("decided_at", "earlier")
        for k, v in state.get("proposals", {}).items()
        if v.get("status") == "rejected"
    }
    kept, dropped = [], []
    for prop in proposals:
        reason = problem(prop, ev, run_dir, rejected)
        if reason:
            dropped.append(
                {
                    "title": prop.get("title", "(untitled)"),
                    "target": prop.get("target", "?"),
                    "reason": reason,
                }
            )
            continue
        ids = list(dict.fromkeys(prop["evidence"]))
        kept.append(
            {
                **prop,
                "n": len(kept) + 1,
                "key": rule_key(prop["target"], prop["rule"]),
                "sessions": len(ids),
                "people": ev.people(ids),
                "owner": owner_label(prop, ev),
            }
        )
    return kept, dropped, ev, candidates


def render_digest(
    kept: list[dict], dropped: list[dict], candidates: dict, mode: str, run_dir: Path
) -> str:
    counts, window = candidates["counts"], candidates["window"]
    by_kind = counts.get("sessions_by_kind", {})
    lines = [f"**Skill proposals: {window['since'][:10]} to {window['until'][:10]}**"]
    if mode != "live":
        lines.append(
            "_Test run from existing transcripts. Replies in this thread are not applied._"
        )
    lines += [
        "",
        (
            f"Scanned {counts['sessions_scanned']:,} sessions ({by_kind.get('slack', 0):,} chat, "
            f"{by_kind.get('cron', 0):,} scheduled job). {counts['candidates']} candidates. "
            f"{plural(len(kept), 'proposal')}, {len(dropped)} dropped by validation."
        ),
        "",
    ]
    for prop in kept:
        ask = ", includes an explicit ask" if prop.get("explicit_ask") else ""
        lines += [
            f"**{prop['n']}. {prop['title']}**",
            f"Target: `{prop['target']}` ({prop['owner']})",
            f"Rule: {prop['rule']}",
            f"Why: {prop['why']}",
            f"Evidence: {plural(prop['sessions'], 'session')}, {plural(prop['people'], 'person', 'people')}{ask}",
            "",
        ]
    if dropped:
        lines.append("**Dropped by validation**")
        lines += [f"- {d['title']} (`{d['target']}`): {d['reason']}" for d in dropped]
        lines.append("")
    unused = candidates.get("skill_usage", {}).get("unused_in_window", [])
    discarded = load_json(run_dir / "proposals.json", {}).get("discarded", [])
    lines.append(
        f"Review discarded {plural(len(discarded), 'cluster')}. Skills with no reads in this window: {len(unused)}. "
        "The attached file lists both, with every proposed text."
    )
    if kept:
        lines += ["", "Reply in this thread with `approve 1,3` or `reject 2: reason`."]
    return "\n".join(lines) + "\n"


def plural(count: int, noun: str, many: str = "") -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {many or noun + 's'}"


def render_detail(kept: list[dict], candidates: dict, run_dir: Path) -> str:
    out = ["# Skill proposals: proposed text", ""]
    for prop in kept:
        out += [
            f"## {prop['n']}. {prop['title']}",
            "",
            f"Target: `{prop['target']}` ({prop['owner']})",
            "",
        ]
        if prop.get("patch_file"):
            out += [(run_dir / prop["patch_file"]).read_text().rstrip(), ""]
        else:
            out += ["No text change. This is a note for the skill's owner.", ""]
    discarded = load_json(run_dir / "proposals.json", {}).get("discarded", [])
    out += ["# Clusters the review discarded", ""]
    out += [f"- {d.get('theme', '?')}: {d.get('reason', '?')}" for d in discarded] or [
        "- none"
    ]
    out.append("")
    usage = candidates.get("skill_usage", {})
    out += [
        "# Skill usage in this window",
        "",
        "| Skill | Sessions that read it |",
        "| --- | --- |",
    ]
    out += [
        f"| `{name}` | {count} |"
        for name, count in usage.get("sessions_reading", {}).items()
    ]
    out += ["", "## Installed skills with no reads", ""]
    out += [f"- `{name}`" for name in usage.get("unused_in_window", [])] or ["- none"]
    return "\n".join(out) + "\n"


def record(run_dir: Path, state_path: Path, permalink: str) -> dict:
    final = load_json(run_dir / "proposals.final.json")
    candidates = load_json(run_dir / "candidates.json")
    if final is None or candidates is None:
        raise SystemExit("error: run finalize.py without --record first")
    state = load_json(state_path, {"proposals": {}})
    state["last_run_at"] = candidates["window"]["until"]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for prop in final["kept"]:
        state["proposals"].setdefault(
            prop["key"],
            {
                "status": "sent",
                "target": prop["target"],
                "title": prop["title"],
                "run_dir": str(run_dir),
                "n": prop["n"],
                "permalink": permalink,
                "sent_at": now,
            },
        )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    return state


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "test", "live"), required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--record",
        metavar="PERMALINK",
        help="live only: store sent proposals and the cursor",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.record:
        if args.mode != "live" or not args.state:
            raise SystemExit("error: --record needs --mode live and --state")
        record(args.run_dir, args.state, args.record)
        print(f"recorded state in {args.state}")
        return 0
    state = load_json(args.state, {}) if args.state else {}
    kept, dropped, _ev, candidates = validate(args.run_dir, state)
    (args.run_dir / "proposals.final.json").write_text(
        json.dumps({"kept": kept, "dropped": dropped}, ensure_ascii=False, indent=1)
    )
    (args.run_dir / "digest.md").write_text(
        render_digest(kept, dropped, candidates, args.mode, args.run_dir)
    )
    (args.run_dir / "digest-detail.md").write_text(
        render_detail(kept, candidates, args.run_dir)
    )
    print(json.dumps({"kept": len(kept), "dropped": dropped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
