# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compare a frozen local skill and a proposed full replacement on offline cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GRADE_SCHEMA = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "why": {"type": "string"}},
    "required": ["passed", "why"],
    "additionalProperties": False,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generation_prompt(skill: str, case: dict) -> str:
    return (
        "This is an offline replay. Do not use tools or contact services. "
        "Use only the supplied facts. State the evidence you would inspect and "
        "its source, then draft the answer. Do not invent a successful execution.\n\n"
        f"SKILL INSTRUCTIONS:\n{skill}\n\nTASK:\n{case['prompt']}"
    )


def grading_prompt(case: dict, answer: str) -> str:
    return (
        "Judge only the supplied answer. Do not use tools or inspect files. "
        "Pass only if the answer meets every expectation. "
        "Assess observable actions and claims, not matching wording.\n\n"
        f"TASK:\n{case['prompt']}\n\n"
        f"EXPECTATIONS:\n{json.dumps(case['expectations'], ensure_ascii=False)}\n\n"
        f"ANSWER:\n{answer}\n\n"
        'Return JSON: {"passed": true or false, "why": "one short sentence"}.'
    )


def call(command: list[str]) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="cotf-skill-eval-") as directory:
            result = subprocess.run(
                command,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=300,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Model call timed out after 300 seconds") from error
    if result.returncode:
        raise RuntimeError(f"Model call failed with exit code {result.returncode}")
    return result.stdout


def generate(prompt: str, model: str) -> tuple[str, dict]:
    raw = call(
        [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--system-prompt",
            "You perform offline task replays. Use no tools.",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--output-format",
            "json",
        ]
    )
    envelope = json.loads(raw)
    if envelope.get("is_error") or not isinstance(envelope.get("result"), str):
        raise RuntimeError("Claude returned a failed execution")
    if not envelope["result"].strip():
        raise RuntimeError("Claude returned an empty answer")
    metadata = {
        key: envelope.get(key) for key in ("modelUsage", "total_cost_usd", "usage")
    }
    return envelope["result"], metadata


def grade(prompt: str, schema: Path, model: str | None = None) -> dict:
    command = [
        "codex",
        "exec",
        prompt,
        "--json",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema),
    ]
    if model:
        command += ["--model", model]
    raw = call(command)
    answer = None
    for line in raw.splitlines():
        event = json.loads(line)
        item = event.get("item") or {}
        if item.get("type") in {
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "web_search_call",
            "tool_call",
        }:
            raise RuntimeError("The judge attempted a tool call")
        if item.get("type") == "agent_message":
            answer = json.loads(item["text"])
    if not isinstance(answer, dict) or not isinstance(answer.get("passed"), bool):
        raise RuntimeError("The judge returned an invalid verdict")
    return answer


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text())["cases"]
    identifiers = [case["case_id"] for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Case IDs must be unique")
    if {case.get("kind") for case in cases} != {"targeted", "control"}:
        raise ValueError("Cases require targeted and control examples")
    if any(not case.get("prompt") or not case.get("expectations") for case in cases):
        raise ValueError("Each case requires a prompt and expectations")
    return cases


def relative_file(run_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(run_dir.resolve()))
    except ValueError as error:
        raise ValueError(
            "The proposed skill must be inside the run directory"
        ) from error


def evaluate(
    run_dir: Path,
    target: str,
    source: Path,
    patch: Path,
    cases: list[dict],
    *,
    reps: int,
    model: str,
    workers: int,
    judge_model: str | None = None,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_:-]*", target):
        raise ValueError("The target must be a skill name without path separators")
    if reps < 1 or workers < 1:
        raise ValueError("Repetitions and workers must be positive")
    patch_name = relative_file(run_dir, patch)
    destination = run_dir / "evaluations" / target.replace(":", "-")
    destination.mkdir(parents=True, exist_ok=False)
    originals = {"original": source.read_text(), "candidate": patch.read_text()}
    arms = {}
    for name, text in originals.items():
        frozen = destination / f"{name}.md"
        frozen.write_text(text)
        arms[name] = {"file": relative_file(run_dir, frozen), "sha256": sha256(frozen)}
    labels = ["A", "B"]
    random.Random(42).shuffle(labels)
    blind_labels = dict(zip(originals, labels, strict=True))
    schema = destination / "grade-schema.json"
    schema.write_text(json.dumps(GRADE_SCHEMA))
    jobs = [
        (case, arm, repeat)
        for case in cases
        for arm in originals
        for repeat in range(1, reps + 1)
    ]
    random.Random(42).shuffle(jobs)

    def execute(indexed: tuple[int, tuple]) -> dict:
        index, (case, arm, repeat) = indexed
        answer, metadata = generate(generation_prompt(originals[arm], case), model)
        output = destination / f"answer-{index:03d}.txt"
        output.write_text(answer)
        prompt = grading_prompt(case, answer)
        verdict = (
            grade(prompt, schema.resolve(), model=judge_model)
            if judge_model
            else grade(prompt, schema.resolve())
        )
        return {
            "case_id": case["case_id"],
            "kind": case["kind"],
            "repeat": repeat,
            "arm_label": blind_labels[arm],
            "output_file": relative_file(run_dir, output),
            "output_sha256": sha256(output),
            "grade": "pass" if verdict["passed"] else "fail",
            "why": verdict["why"],
            "generation": metadata,
        }

    manifest = {
        "schema_version": 1,
        "target": target,
        "source_skill_file": str(source.resolve()),
        "source_skill_sha256": sha256(source),
        "patch_file": patch_name,
        "patch_sha256": sha256(patch),
        "arms": arms,
        "blind_labels": blind_labels,
        "case_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True).encode()
        ).hexdigest(),
        "generation_model": model,
        "judge_model": judge_model
        or "Codex CLI default; exact model identifier unreported",
    }
    (destination / "cases.json").write_text(
        json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n"
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(execute, enumerate(jobs)))
    manifest["cases"] = rows
    (destination / "evaluation.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--source-skill", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--judge-model", help="Pin the Codex judge model")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if len(cases) * 2 * args.reps > 40:
        raise SystemExit("Evaluation exceeds the 40-generation limit")
    record = evaluate(
        args.run_dir,
        args.target,
        args.source_skill,
        args.candidate,
        cases,
        reps=args.reps,
        model=args.model,
        workers=args.workers,
        judge_model=args.judge_model,
    )
    print(
        json.dumps(
            {
                "rows": len(record["cases"]),
                "passed": sum(row["grade"] == "pass" for row in record["cases"]),
            }
        )
    )


if __name__ == "__main__":
    main()
