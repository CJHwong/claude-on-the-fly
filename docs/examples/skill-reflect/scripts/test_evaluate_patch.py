"""Checks for isolated, paired skill evaluation."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "evaluate_patch", Path(__file__).with_name("evaluate_patch.py")
)
evaluator = importlib.util.module_from_spec(SPEC)


def load_runner():
    SPEC.loader.exec_module(evaluator)
    return evaluator


def test_generation_never_sees_expectations():
    runner = load_runner()
    case = {
        "prompt": "Inspect the failed parser.",
        "expectations": ["HIDDEN EXPECTATION"],
    }
    prompt = runner.generation_prompt("Skill text", case)
    assert "Skill text" in prompt
    assert "Inspect the failed parser." in prompt
    assert "HIDDEN EXPECTATION" not in prompt


def test_judge_never_sees_arm_identity():
    runner = load_runner()
    prompt = runner.grading_prompt(
        {
            "prompt": "Inspect the parser.",
            "expectations": ["Inspect the saved response."],
        },
        "Check the saved response type.",
    )
    assert "Inspect the saved response." in prompt
    assert "original" not in prompt
    assert "candidate" not in prompt


def test_cases_require_a_control_and_unique_ids(tmp_path):
    runner = load_runner()
    path = tmp_path / "cases.json"
    case = {
        "case_id": "one",
        "kind": "targeted",
        "prompt": "Task",
        "expectations": ["Check"],
    }
    path.write_text(json.dumps({"cases": [case]}))
    with pytest.raises(ValueError, match="control"):
        runner.load_cases(path)
    path.write_text(json.dumps({"cases": [case, {**case, "kind": "control"}]}))
    with pytest.raises(ValueError, match="unique"):
        runner.load_cases(path)


def test_paired_run_saves_outputs_and_hashes(tmp_path, monkeypatch):
    runner = load_runner()
    source = tmp_path / "installed" / "SKILL.md"
    source.parent.mkdir()
    source.write_text("Existing instructions")
    run = tmp_path / "run"
    run.mkdir()
    patch = run / "patches" / "skill.md"
    patch.parent.mkdir()
    patch.write_text("Proposed instructions")
    monkeypatch.setattr(
        runner, "generate", lambda prompt, model: ("An answer", {"model": "fixture"})
    )
    monkeypatch.setattr(
        runner,
        "grade",
        lambda prompt, schema: {"passed": True, "why": "Meets expectations"},
    )
    cases = [
        {
            "case_id": name,
            "kind": kind,
            "prompt": "A task",
            "expectations": ["A criterion"],
        }
        for name, kind in (("one", "targeted"), ("two", "control"))
    ]
    record = runner.evaluate(
        run, "local-skill", source, patch, cases, reps=2, model="fixture", workers=1
    )
    assert len(record["cases"]) == 8
    assert record["source_skill_sha256"] == runner.sha256(source)
    assert record["arms"]["candidate"]["sha256"] == runner.sha256(patch)
    assert len(set(record["blind_labels"].values())) == 2
    for row in record["cases"]:
        assert runner.sha256(run / row["output_file"]) == row["output_sha256"]


def test_target_cannot_escape_the_evaluation_directory(tmp_path, monkeypatch):
    runner = load_runner()
    source = tmp_path / "SKILL.md"
    source.write_text("Original")
    run = tmp_path / "run"
    run.mkdir()
    patch = run / "candidate.md"
    patch.write_text("Candidate")
    monkeypatch.setattr(runner, "generate", lambda prompt, model: ("Answer", {}))
    monkeypatch.setattr(
        runner, "grade", lambda prompt, schema: {"passed": True, "why": "Valid"}
    )
    with pytest.raises(ValueError, match="target"):
        runner.evaluate(
            run, "../escape", source, patch, [], reps=1, model="fixture", workers=1
        )


def test_judge_uses_the_requested_model(tmp_path, monkeypatch):
    runner = load_runner()
    commands = []

    def fake_call(command):
        commands.append(command)
        return json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": '{"passed": true, "why": "Meets the criteria"}',
                },
            }
        )

    monkeypatch.setattr(runner, "call", fake_call)
    result = runner.grade(
        "Judge this answer", tmp_path / "schema.json", model="gpt-6-luna"
    )
    assert result["passed"] is True
    position = commands[0].index("--model")
    assert commands[0][position + 1] == "gpt-6-luna"
