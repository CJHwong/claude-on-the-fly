# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "pyyaml>=6.0"]
# ///
"""Run two weekly reviews through the public helper commands."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def run_helper(script: str, *arguments: str | Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name(script)), *map(str, arguments)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def add_session(root: Path, identifier: str, updated: datetime) -> None:
    transcript = root / f"{identifier}.jsonl"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "I selected the parent.",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": "[from-id: U_TEST] No, read linked issues before selecting the parent.",
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records))
    connection = sqlite3.connect(root / "codex/state_5.sqlite")
    connection.execute(
        "create table if not exists threads "
        "(id text, rollout_path text, cwd text, updated_at int, thread_source text)"
    )
    connection.execute(
        "insert into threads values (?,?,?,?,?)",
        (
            identifier,
            str(transcript),
            str(root / "cotf/workspaces/slack/dm/TEST"),
            int(updated.timestamp()),
            "user",
        ),
    )
    connection.commit()
    connection.close()


def select_run(root: Path, directory: Path, state: Path | None = None) -> dict:
    arguments = [
        "--out",
        directory / "candidates.json",
        "--days",
        "21",
        "--codex-home",
        root / "codex",
        "--claude-home",
        root / "claude",
        "--cotf-home",
        root / "cotf",
    ]
    if state:
        arguments.extend(["--state", state])
    run_helper("select_candidates.py", *arguments)
    return json.loads((directory / "candidates.json").read_text())


def write_review(directory: Path, evidence: list[str]) -> None:
    mechanism = {
        "id": "linked-parent",
        "task_class": "Ticket creation",
        "mechanism": "Read linked issues before selecting the parent.",
        "status": "pending",
        "reason": "Retain the observed procedure.",
        "evidence": evidence,
    }
    proposals = []
    if len(evidence) > 1:
        (directory / "patches").mkdir()
        (directory / "patches/parent.md").write_text(
            "# Ticket creation\nRead linked issues first.\n"
        )
        proposals.append(
            {
                "target": "new:ticket-parent",
                "action": "new-skill",
                "title": "Read links before selecting a parent",
                "rule": "Read linked issues first.",
                "why": "A linked issue can supply the parent.",
                "evidence": evidence,
                "mechanism_id": "linked-parent",
                "patch_file": "patches/parent.md",
            }
        )
    (directory / "proposals.json").write_text(
        json.dumps(
            {
                "proposals": proposals,
                "mechanisms": [mechanism],
                "discarded": [],
            }
        )
    )


def record_run(directory: Path, state: Path, receipt: str) -> dict:
    arguments = ["--run-dir", directory, "--mode", "live", "--state", state]
    run_helper("finalize.py", *arguments)
    run_helper("finalize.py", *arguments, "--record", receipt)
    return json.loads(state.read_text())


def test_long_tail_mechanism_reaches_the_threshold_in_a_later_week(tmp_path):
    for name in ("cotf", "codex", "claude"):
        (tmp_path / name).mkdir()
    (tmp_path / "cotf/skill-reflect.yaml").write_text(
        f"soul_root: {tmp_path / 'soul'}\n"
    )
    now = datetime.now(UTC)
    add_session(tmp_path, "first-session", now - timedelta(days=10))
    first = tmp_path / "first-review"
    selected = select_run(tmp_path, first)
    selected["window"]["until"] = (now - timedelta(days=7)).isoformat(
        timespec="seconds"
    )
    (first / "candidates.json").write_text(json.dumps(selected))
    write_review(first, ["first-session"])
    state = tmp_path / "state.json"
    saved = record_run(first, state, "first-receipt")
    assert saved["proposals"] == {}
    assert saved["mechanisms"]["linked-parent"]["status"] == "pending"

    (tmp_path / "first-session.jsonl").unlink()
    add_session(tmp_path, "later-session", now - timedelta(days=1))
    later = tmp_path / "later-review"
    selected = select_run(tmp_path, later, state)
    assert [entry["thread_id"] for entry in selected["candidates"]] == ["later-session"]
    assert [entry["thread_id"] for entry in selected["archived_candidates"]] == [
        "first-session"
    ]
    write_review(later, ["first-session", "later-session"])
    saved = record_run(later, state, "later-receipt")
    assert saved["mechanisms"]["linked-parent"]["status"] == "proposed"
    assert saved["mechanisms"]["linked-parent"]["evidence"] == [
        "first-session",
        "later-session",
    ]
    assert next(iter(saved["proposals"].values()))["status"] == "sent"
    final = json.loads((later / "proposals.final.json").read_text())
    assert final["kept"][0]["sessions"] == 2
    assert final["dropped"] == []
    assert "[from-id:" not in state.read_text()
