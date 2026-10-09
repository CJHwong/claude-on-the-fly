# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Tests for finalize.py: validation, rendering, and state recording."""

from __future__ import annotations

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
