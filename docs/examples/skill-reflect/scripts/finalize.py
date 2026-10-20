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
        usage = candidates.get("skill_usage", {})
        self.inventory = usage.get("inventory", {})
        self.source_files = usage.get("source_files", {})

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
    if prop["action"] == "patch" and ev.inventory.get(prop["target"]) == "local":
        reason, _summary = evaluation_check(prop, run_dir, ev)
        if reason:
            return f"paired evaluation rejected: {reason}"
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


def run_file(run_dir: Path, value: str) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    root = run_dir.resolve()
    path = (root / value).resolve()
    return path if path.is_relative_to(root) else None


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def evaluation_check(
    prop: dict, run_dir: Path, ev: Evidence
) -> tuple[str | None, dict | None]:
    """Validate paired local-skill results and summarize their evidence."""
    eval_file = run_file(run_dir, prop.get("evaluation_file", ""))
    if not eval_file or not eval_file.is_file():
        return "evaluation file is required under the run directory", None
    try:
        evaluation = json.loads(eval_file.read_text())
    except (OSError, json.JSONDecodeError):
        return "evaluation file is invalid", None
    if (
        not isinstance(evaluation, dict)
        or type(evaluation.get("schema_version")) is not int
        or evaluation["schema_version"] != 1
    ):
        return "evaluation schema version must be 1", None
    if evaluation.get("target") != prop["target"]:
        return "evaluation target does not match proposal", None

    patch_file = run_file(run_dir, prop.get("patch_file", ""))
    if not patch_file or evaluation.get("patch_file") != prop["patch_file"]:
        return "evaluation patch file does not match proposal", None
    patch_hash = sha256_file(patch_file)
    if not patch_hash or patch_hash != evaluation.get("patch_sha256"):
        return "patch hash does not match evaluation", None

    source_name = evaluation.get("source_skill_file", "")
    source_file = Path(source_name) if isinstance(source_name, str) else Path()
    if not source_file.is_absolute():
        return "source skill file must be absolute", None
    trusted_name = ev.source_files.get(prop["target"])
    trusted_file = Path(trusted_name) if isinstance(trusted_name, str) else Path()
    if (
        not trusted_file.is_absolute()
        or source_file.resolve() != trusted_file.resolve()
    ):
        return "source skill path does not match selector inventory", None
    source_hash = sha256_file(source_file)
    if not source_hash or source_hash != evaluation.get("source_skill_sha256"):
        return "source skill hash does not match evaluation", None

    arms = evaluation.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"original", "candidate"}:
        return "evaluation must contain original and candidate arms", None
    arm_files = {}
    for arm in ("original", "candidate"):
        item = arms[arm]
        if not isinstance(item, dict):
            return f"evaluation {arm} arm is invalid", None
        path = run_file(run_dir, item.get("file", ""))
        if not path or not path.is_file():
            return (
                f"evaluation {arm} file is missing or outside the run directory",
                None,
            )
        actual_hash = sha256_file(path)
        if not actual_hash or actual_hash != item.get("sha256"):
            return f"evaluation {arm} hash does not match file", None
        arm_files[arm] = path
    if arms["original"]["sha256"] != source_hash:
        return "original skill hash does not match source skill", None
    if (
        arms["candidate"]["sha256"] != patch_hash
        or arm_files["candidate"].read_bytes() != patch_file.read_bytes()
    ):
        return "candidate skill does not match proposed full skill file", None

    labels = evaluation.get("blind_labels")
    if (
        not isinstance(labels, dict)
        or set(labels) != {"original", "candidate"}
        or any(
            not isinstance(label, str) or label not in {"A", "B"}
            for label in labels.values()
        )
        or len(set(labels.values())) != 2
    ):
        return "blind labels must assign A and B to both arms", None
    label_to_arm = {label: arm for arm, label in labels.items()}
    declared, reason = evaluation_case_definitions(evaluation, eval_file, run_dir)
    if reason:
        return reason, None
    result, reason = validate_evaluation_cases(
        evaluation.get("cases"), label_to_arm, run_dir, declared
    )
    if reason:
        return reason, None
    summary = {
        "source_skill_sha256": source_hash,
        "original_sha256": arms["original"]["sha256"],
        "candidate_sha256": arms["candidate"]["sha256"],
        "patch_sha256": patch_hash,
        "evaluation_sha256": sha256_file(eval_file),
        "case_sha256": evaluation["case_sha256"],
        **result,
    }
    return None, summary


def evaluation_case_definitions(
    evaluation: dict, eval_file: Path, run_dir: Path
) -> tuple[dict, str | None]:
    case_path = run_file(
        run_dir, str(eval_file.relative_to(run_dir.resolve()).parent / "cases.json")
    )
    if not case_path or not case_path.is_file():
        return {}, "evaluation case file is required under the run directory"
    try:
        document = json.loads(case_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, "evaluation case file is invalid"
    cases = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(cases, list) or not cases:
        return {}, "evaluation case file must contain cases"
    actual_hash = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()
    if actual_hash != evaluation.get("case_sha256"):
        return {}, "evaluation case file hash does not match recorded cases"
    declared = {}
    for case in cases:
        if not isinstance(case, dict):
            return {}, "evaluation case file contains an invalid case"
        case_id, kind = case.get("case_id"), case.get("kind")
        if not isinstance(case_id, str) or not case_id or case_id in declared:
            return {}, "evaluation case file needs unique case IDs"
        if not isinstance(kind, str) or kind not in {"targeted", "control"}:
            return {}, "evaluation case file contains an invalid kind"
        declared[case_id] = kind
    return declared, None


def validate_evaluation_cases(
    rows: object, label_to_arm: dict, run_dir: Path, declared: dict
) -> tuple[dict, str | None]:
    if not isinstance(rows, list) or not rows:
        return {}, "evaluation cases are missing"
    paired: dict[tuple[str, int], dict[str, dict]] = {}
    kinds: dict[str, str] = {}
    for row in rows:
        reason = validate_evaluation_row(row, label_to_arm, run_dir)
        if reason:
            return {}, reason
        case_id, repeat = row["case_id"], row["repeat"]
        if declared.get(case_id) != row["kind"]:
            return {}, "recorded cases do not match declared cases"
        if case_id in kinds and kinds[case_id] != row["kind"]:
            return {}, "evaluation case kind changed between repeats"
        kinds[case_id] = row["kind"]
        pair = paired.setdefault((case_id, repeat), {})
        arm = label_to_arm[row["arm_label"]]
        if arm in pair:
            return {}, "evaluation has a duplicate arm and repeat"
        pair[arm] = row
    if set(kinds) != set(declared):
        return {}, "recorded cases do not cover all declared cases"
    if any(set(pair) != {"original", "candidate"} for pair in paired.values()):
        return {}, "evaluation has an unpaired case and repeat"
    return score_evaluation_pairs(paired, kinds)


def validate_evaluation_row(
    row: object, label_to_arm: dict, run_dir: Path
) -> str | None:
    if not isinstance(row, dict):
        return "evaluation case row is invalid"
    if not isinstance(row.get("case_id"), str) or not row["case_id"]:
        return "evaluation case id is missing"
    if not isinstance(row.get("kind"), str) or row["kind"] not in {
        "targeted",
        "control",
    }:
        return "evaluation case kind must be targeted or control"
    if (
        not isinstance(row.get("repeat"), int)
        or isinstance(row["repeat"], bool)
        or row["repeat"] < 1
    ):
        return "evaluation repeat must be a positive integer"
    if (
        not isinstance(row.get("arm_label"), str)
        or row["arm_label"] not in label_to_arm
    ):
        return "evaluation case has an unknown blind label"
    if not isinstance(row.get("grade"), str) or row["grade"] not in {"pass", "fail"}:
        return "evaluation grade must be pass or fail"
    output = run_file(run_dir, row.get("output_file", ""))
    if not output or not output.is_file() or not output.stat().st_size:
        return "recorded output is missing or outside the run directory"
    if sha256_file(output) != row.get("output_sha256"):
        return "output hash does not match recorded output"
    return None


def score_evaluation_pairs(
    paired: dict[tuple[str, int], dict[str, dict]], kinds: dict[str, str]
) -> tuple[dict, str | None]:
    targeted_gains = 0
    targeted_cases = {case for case, kind in kinds.items() if kind == "targeted"}
    control_cases = {case for case, kind in kinds.items() if kind == "control"}
    if not targeted_cases or not control_cases:
        return {}, "evaluation needs targeted and control cases"
    for (case_id, _repeat), arms in paired.items():
        original = arms["original"]["grade"]
        candidate = arms["candidate"]["grade"]
        if kinds[case_id] == "control" and (original != "pass" or candidate != "pass"):
            return {}, "control candidate did not pass on both arms"
        if original == "pass" and candidate == "fail":
            return {}, "candidate regressed on an evaluation case"
        if kinds[case_id] == "targeted" and original == "fail" and candidate == "pass":
            targeted_gains += 1
    if not targeted_gains:
        return {}, "evaluation has no targeted improvement"
    return {
        "targeted_cases": len(targeted_cases),
        "control_cases": len(control_cases),
        "targeted_gains": targeted_gains,
        "targeted_regressions": 0,
        "paired_case_repeats": len(paired),
    }, None


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
        kept_prop = {
            **prop,
            "n": len(kept) + 1,
            "key": rule_key(prop["target"], prop["rule"]),
            "sessions": len(ids),
            "people": ev.people(ids),
            "owner": owner_label(prop, ev),
        }
        if prop["action"] == "patch" and ev.inventory.get(prop["target"]) == "local":
            _reason, kept_prop["evaluation"] = evaluation_check(prop, run_dir, ev)
        kept.append(kept_prop)
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


def render_evaluation(evaluation: dict) -> list[str]:
    return [
        "Paired evaluation: "
        f"{plural(evaluation['targeted_cases'], 'targeted case')}, "
        f"{plural(evaluation['control_cases'], 'control case')}, "
        f"{evaluation['targeted_gains']} targeted gains, "
        f"{evaluation['targeted_regressions']} regressions.",
        f"Original skill SHA-256: `{evaluation['original_sha256']}`",
        f"Candidate skill SHA-256: `{evaluation['candidate_sha256']}`",
        f"Proposal SHA-256: `{evaluation['patch_sha256']}`",
        "",
    ]


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
        if evaluation := prop.get("evaluation"):
            out += render_evaluation(evaluation)
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
        entry = {
            "status": "sent",
            "target": prop["target"],
            "title": prop["title"],
            "run_dir": str(run_dir),
            "n": prop["n"],
            "permalink": permalink,
            "sent_at": now,
        }
        if evaluation := prop.get("evaluation"):
            entry["evaluation"] = evaluation
            entry["skill_version"] = evaluation["candidate_sha256"]
        state["proposals"].setdefault(prop["key"], entry)
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
