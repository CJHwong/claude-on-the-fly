# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Tests for finalize.py: validation, rendering, and state recording."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "finalize", Path(__file__).with_name("finalize.py")
)
fin = importlib.util.module_from_spec(SPEC)
sys.modules["finalize"] = fin
SPEC.loader.exec_module(fin)

CANDIDATES = {
    "window": {
        "since": "2026-10-01T00:00:00+00:00",
        "until": "2026-10-08T00:00:00+00:00",
    },
    "counts": {
        "sessions_scanned": 1782,
        "sessions_by_kind": {"slack": 877, "cron": 905},
        "candidates": 3,
    },
    "candidates": [
        {"thread_id": "t1", "speakers": ["U1"], "signals": {"correction": 1}},
        {"thread_id": "t2", "speakers": ["U2"], "signals": {"recovered_error": 2}},
        {"thread_id": "t3", "speakers": ["U1"], "signals": {"explicit_ask": 1}},
    ],
    "cron_recoveries": [{"job": "standup-flash"}],
    "skill_usage": {
        "sessions_reading": {"jira-kit:fetch": 4},
        "unused_in_window": ["agent-ops:unused-one"],
        "inventory": {
            "jira-kit:fetch": "upstream",
            "agent-ops:check": "local",
            "twg-jira": "third-party",
            "agent-ops:unused-one": "local",
        },
    },
}


def proposal(**over) -> dict:
    base = {
        "target": "jira-kit:fetch",
        "action": "patch",
        "title": "Read links first",
        "rule": "Read linked issues first.",
        "why": "the parent field rejects cross-project issues",
        "evidence": ["t1", "t2"],
        "patch_file": "patches/01.md",
    }
    return {**base, **over}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches/01.md").write_text(
        "Add to step 2:\n> Read linked issues first."
    )
    (tmp_path / "candidates.json").write_text(json.dumps(CANDIDATES))
    return tmp_path


def finalize(run_dir: Path, proposals: list[dict], *extra: str, discarded=()) -> dict:
    (run_dir / "proposals.json").write_text(
        json.dumps({"proposals": proposals, "discarded": list(discarded)})
    )
    assert fin.main(["--run-dir", str(run_dir), "--mode", "test", *extra]) == 0
    return json.loads((run_dir / "proposals.final.json").read_text())


def local_patch_with_evaluation(run_dir: Path, tmp_path: Path) -> dict:
    source = tmp_path.parent / f"{tmp_path.name}-installed-skill.md"
    original_text = "# Existing local skill\n\nCurrent rule.\n"
    candidate_text = "# Existing local skill\n\nCurrent rule. New rule.\n"
    source.write_text(original_text)
    evaluation_dir = run_dir / "evaluation"
    evaluation_dir.mkdir()
    (evaluation_dir / "original.md").write_text(original_text)
    (evaluation_dir / "candidate.md").write_text(candidate_text)
    patch_file = run_dir / "patches/local.md"
    patch_file.write_text(candidate_text)
    cases = []
    blind_labels = {"original": "B", "candidate": "A"}
    outputs = run_dir / "evaluation" / "outputs"
    outputs.mkdir()
    for case_id, kind, original_grade, candidate_grade in (
        ("target-1", "targeted", "fail", "pass"),
        ("control-1", "control", "pass", "pass"),
    ):
        for repeat in (1, 2):
            for arm, grade in (
                ("original", original_grade),
                ("candidate", candidate_grade),
            ):
                label = blind_labels[arm]
                output_file = f"evaluation/outputs/{case_id}-{repeat}-{label}.txt"
                output = run_dir / output_file
                output.write_text(
                    f"recorded {arm} output for {case_id} repeat {repeat}\n"
                )
                cases.append(
                    {
                        "case_id": case_id,
                        "kind": kind,
                        "repeat": repeat,
                        "arm_label": label,
                        "output_file": output_file,
                        "output_sha256": hashlib.sha256(
                            output.read_bytes()
                        ).hexdigest(),
                        "grade": grade,
                    }
                )

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    case_definitions = [
        {
            "case_id": case_id,
            "kind": kind,
            "prompt": "A recorded task",
            "expectations": ["The recorded requirement"],
        }
        for case_id, kind in (("target-1", "targeted"), ("control-1", "control"))
    ]
    (evaluation_dir / "cases.json").write_text(json.dumps({"cases": case_definitions}))
    evaluation = {
        "schema_version": 1,
        "target": "agent-ops:check",
        "source_skill_file": str(source),
        "source_skill_sha256": sha256(source),
        "patch_file": "patches/local.md",
        "patch_sha256": sha256(patch_file),
        "arms": {
            "original": {
                "file": "evaluation/original.md",
                "sha256": sha256(evaluation_dir / "original.md"),
            },
            "candidate": {
                "file": "evaluation/candidate.md",
                "sha256": sha256(evaluation_dir / "candidate.md"),
            },
        },
        "blind_labels": blind_labels,
        "case_sha256": hashlib.sha256(
            json.dumps(case_definitions, sort_keys=True).encode()
        ).hexdigest(),
        "cases": cases,
    }
    (evaluation_dir / "evidence.json").write_text(json.dumps(evaluation))
    candidates_path = run_dir / "candidates.json"
    candidates = json.loads(candidates_path.read_text())
    candidates["skill_usage"]["source_files"] = {
        "agent-ops:check": str(source.resolve())
    }
    candidates_path.write_text(json.dumps(candidates))
    return proposal(
        target="agent-ops:check",
        patch_file="patches/local.md",
        evaluation_file="evaluation/evidence.json",
    )


def test_valid_proposal_is_kept_with_counts(run_dir):
    out = finalize(run_dir, [proposal()])
    kept = out["kept"][0]
    assert (kept["n"], kept["sessions"], kept["people"], kept["owner"]) == (
        1,
        2,
        2,
        "upstream, patch",
    )
    digest = (run_dir / "digest.md").read_text()
    assert "Test run from existing transcripts" in digest
    assert "Scanned 1,782 sessions (877 chat, 905 scheduled job)" in digest
    assert "Evidence: 2 sessions, 2 people" in digest and "approve 1,3" in digest
    assert "1 proposal, 0 dropped" in digest and "Review discarded 0 clusters" in digest
    detail = (run_dir / "digest-detail.md").read_text()
    assert (
        "> Read linked issues first." in detail and "- `agent-ops:unused-one`" in detail
    )


def test_local_patch_requires_paired_evaluation(run_dir):
    local = proposal(target="agent-ops:check", patch_file="patches/01.md")
    out = finalize(run_dir, [local])

    assert out["kept"] == []
    assert "evaluation file is required" in out["dropped"][0]["reason"]


def test_local_patch_keeps_hash_checked_paired_evaluation(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)

    out = finalize(run_dir, [local])

    kept = out["kept"][0]
    assert kept["evaluation"]["targeted_gains"] == 2
    assert kept["evaluation"]["control_cases"] == 1
    detail = (run_dir / "digest-detail.md").read_text()
    assert "Paired evaluation: 1 targeted case, 1 control case" in detail
    assert kept["evaluation"]["source_skill_sha256"]


def test_local_evaluation_rejects_a_stale_source_skill(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    evidence = json.loads((run_dir / local["evaluation_file"]).read_text())
    Path(evidence["source_skill_file"]).write_text("changed after evaluation")

    out = finalize(run_dir, [local])

    assert "source skill hash does not match" in out["dropped"][0]["reason"]


def test_local_evaluation_rejects_an_unrelated_declared_source(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    evidence_path = run_dir / local["evaluation_file"]
    evaluation = json.loads(evidence_path.read_text())
    unrelated = run_dir / "evaluation/unrelated.md"
    source = Path(evaluation["source_skill_file"])
    unrelated.write_bytes(source.read_bytes())
    evaluation["source_skill_file"] = str(unrelated.resolve())
    evidence_path.write_text(json.dumps(evaluation))

    out = finalize(run_dir, [local])

    assert (
        "source skill path does not match selector inventory"
        in out["dropped"][0]["reason"]
    )


def test_local_evaluation_rejects_a_frozen_candidate_hash_mismatch(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    candidate = run_dir / "evaluation/candidate.md"
    candidate.write_text("changed frozen candidate")

    out = finalize(run_dir, [local])

    assert (
        "evaluation candidate hash does not match file" in out["dropped"][0]["reason"]
    )


def test_local_evaluation_rejects_unpaired_outputs(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    evidence_path = run_dir / local["evaluation_file"]
    evaluation = json.loads(evidence_path.read_text())
    evaluation["cases"].pop()
    evidence_path.write_text(json.dumps(evaluation))

    out = finalize(run_dir, [local])

    assert "unpaired case and repeat" in out["dropped"][0]["reason"]


def test_local_evaluation_requires_target_gain_and_passing_control(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    evidence_path = run_dir / local["evaluation_file"]
    baseline_evaluation = json.loads(evidence_path.read_text())
    evaluation = json.loads(evidence_path.read_text())
    for row in evaluation["cases"]:
        if row["kind"] == "targeted":
            row["grade"] = "pass"
    evidence_path.write_text(json.dumps(evaluation))

    out = finalize(run_dir, [local])

    assert "no targeted improvement" in out["dropped"][0]["reason"]

    evaluation = json.loads(evidence_path.read_text())
    for row in evaluation["cases"]:
        if row["case_id"] == "control-1" and row["arm_label"] == "A":
            row["grade"] = "fail"
    evidence_path.write_text(json.dumps(evaluation))
    out = finalize(run_dir, [local])
    assert "control candidate did not pass" in out["dropped"][0]["reason"]

    evaluation = baseline_evaluation
    for row in evaluation["cases"]:
        if row["case_id"] == "target-1" and row["repeat"] == 1:
            row["grade"] = "pass" if row["arm_label"] == "B" else "fail"
    evidence_path.write_text(json.dumps(evaluation))
    out = finalize(run_dir, [local])
    assert "candidate regressed" in out["dropped"][0]["reason"]


def test_local_evaluation_rejects_output_hash_mismatch(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    evidence_path = run_dir / local["evaluation_file"]
    evaluation = json.loads(evidence_path.read_text())
    output_file = run_dir / evaluation["cases"][0]["output_file"]
    output_file.write_text("changed output")
    evidence_path.write_text(json.dumps(evaluation))

    out = finalize(run_dir, [local])

    assert "output hash does not match" in out["dropped"][0]["reason"]


def test_record_saves_local_skill_version_and_evaluation(run_dir, tmp_path):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    finalize(run_dir, [local])
    state_file = tmp_path / "local-state.json"

    fin.record(run_dir, state_file, "local-evaluation")

    state = json.loads(state_file.read_text())
    (entry,) = state["proposals"].values()
    assert entry["skill_version"] == entry["evaluation"]["candidate_sha256"]
    assert entry["evaluation"]["targeted_gains"] == 2


@pytest.mark.parametrize("change", ["missing", "prompt", "expectations"])
def test_local_evaluation_rejects_changed_case_file(run_dir, tmp_path, change):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    case_file = run_dir / "evaluation/cases.json"
    if change == "missing":
        case_file.unlink()
    else:
        document = json.loads(case_file.read_text())
        document["cases"][0][change] = "Changed after evaluation"
        case_file.write_text(json.dumps(document))

    out = finalize(run_dir, [local])

    assert out["kept"] == []
    assert "case file" in out["dropped"][0]["reason"]


@pytest.mark.parametrize("change", ["missing", "unknown", "kind"])
def test_local_evaluation_results_must_match_declared_cases(run_dir, tmp_path, change):
    local = local_patch_with_evaluation(run_dir, tmp_path)
    path = run_dir / local["evaluation_file"]
    evaluation = json.loads(path.read_text())
    if change == "missing":
        case_path = run_dir / "evaluation/cases.json"
        document = json.loads(case_path.read_text())
        document["cases"].append(
            {
                "case_id": "target-unexecuted",
                "kind": "targeted",
                "prompt": "Another task",
            }
        )
        case_path.write_text(json.dumps(document))
        evaluation["case_sha256"] = hashlib.sha256(
            json.dumps(document["cases"], sort_keys=True).encode()
        ).hexdigest()
    else:
        for row in evaluation["cases"]:
            if row["case_id"] == "target-1":
                row["case_id" if change == "unknown" else "kind"] = (
                    "unknown-case" if change == "unknown" else "control"
                )
    path.write_text(json.dumps(evaluation))

    out = finalize(run_dir, [local])

    assert out["kept"] == []
    assert "declared cases" in out["dropped"][0]["reason"]


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"evidence": ["t1", "ghost"]}, "evidence not in candidates: ghost"),
        ({"evidence": ["t1"]}, "fewer than 2 sessions"),
        ({"evidence": ["t1", "t1"]}, "fewer than 2 sessions"),
        (
            {"evidence": ["t1"], "explicit_ask": True},
            "fewer than 2 sessions",
        ),  # claimed, but t1 has no ask
        ({"target": "twg-jira"}, "patch not allowed on a third-party skill"),
        ({"target": "nope:skill"}, "not an installed skill"),
        (
            {"action": "new-skill", "target": "new:jira-kit:fetch"},
            "new-skill target must be",
        ),
        ({"action": "delete"}, "unknown action delete"),
        ({"patch_file": "patches/missing.md"}, "patch file not found"),
        ({"rule": ""}, "missing rule"),
    ],
)
def test_invalid_proposals_are_dropped_with_reason(run_dir, over, reason):
    out = finalize(run_dir, [proposal(**over)])
    assert out["kept"] == [] and reason in out["dropped"][0]["reason"]
    assert reason in (run_dir / "digest.md").read_text()


def test_explicit_ask_in_one_session_passes(run_dir):
    out = finalize(run_dir, [proposal(evidence=["t3"], explicit_ask=True)])
    assert out["kept"][0]["sessions"] == 1
    assert (
        "Evidence: 1 session, 1 person, includes an explicit ask"
        in (run_dir / "digest.md").read_text()
    )


def test_job_evidence_counts(run_dir):
    out = finalize(run_dir, [proposal(evidence=["t2", "job:standup-flash"])])
    assert out["kept"][0]["sessions"] == 2


def test_note_and_profile_note(run_dir):
    out = finalize(
        run_dir,
        [
            proposal(target="twg-jira", action="note", patch_file=""),
            proposal(target="profile:U1", action="profile-note"),
            proposal(target="new:release-notes", action="new-skill", rule="Other."),
        ],
    )
    assert [k["owner"] for k in out["kept"]] == [
        "third-party, note",
        "personal profile",
        "local, new skill",
    ]


def test_rejected_rule_does_not_return(run_dir, tmp_path):
    state = tmp_path / "state.json"
    key = fin.rule_key("jira-kit:fetch", "read linked issues FIRST")
    state.write_text(
        json.dumps(
            {"proposals": {key: {"status": "rejected", "decided_at": "2026-10-01"}}}
        )
    )
    out = finalize(run_dir, [proposal()], "--state", str(state))
    assert out["dropped"][0]["reason"] == "rejected before (2026-10-01)"


def test_record_sets_cursor_and_sent_status(run_dir, tmp_path):
    finalize(run_dir, [proposal()])
    state = tmp_path / "s" / "state.json"
    assert (
        fin.main(
            [
                "--run-dir",
                str(run_dir),
                "--mode",
                "live",
                "--state",
                str(state),
                "--record",
                "https://x/p1",
            ]
        )
        == 0
    )
    saved = json.loads(state.read_text())
    assert saved["last_run_at"] == "2026-10-08T00:00:00+00:00"
    (entry,) = saved["proposals"].values()
    assert (entry["status"], entry["permalink"], entry["n"]) == (
        "sent",
        "https://x/p1",
        1,
    )


def test_record_refuses_outside_live(run_dir, tmp_path):
    with pytest.raises(SystemExit):
        fin.main(
            [
                "--run-dir",
                str(run_dir),
                "--mode",
                "test",
                "--state",
                str(tmp_path / "s.json"),
                "--record",
                "x",
            ]
        )


def test_missing_candidates_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match=r"candidates\.json"):
        fin.main(["--run-dir", str(tmp_path), "--mode", "dry-run"])


def test_discarded_clusters_are_listed_in_detail(run_dir):
    finalize(
        run_dir,
        [proposal()],
        discarded=[{"theme": "Deck retries", "reason": "Below evidence threshold"}],
    )
    assert "Review discarded 1 cluster." in (run_dir / "digest.md").read_text()
    assert (
        "- Deck retries: Below evidence threshold"
        in (run_dir / "digest-detail.md").read_text()
    )


def test_record_needs_a_finalized_run(run_dir, tmp_path):
    with pytest.raises(SystemExit, match="without --record first"):
        fin.record(run_dir, tmp_path / "state.json", "https://x/p1")
