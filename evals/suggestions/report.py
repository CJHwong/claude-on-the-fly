"""Aggregate a run into one table.

    uv run python evals/suggestions/report.py <tag>

Every metric is printed per repetition as well as averaged. The model is
sampled, so a single number cannot separate a template's effect from
run-to-run drift.
"""

import collections
import json
import statistics
import sys

from lib import RUNS

GATE_SLICES = [
    ("overall gate accuracy", lambda r: True),
    ("recall: buttons expected", lambda r: r["expected_gate"] == "buttons"),
    (
        "recall: real clicks only",
        lambda r: r["outcome"] in ("selected", "verbatim_no_click"),
    ),
    ("specificity: none expected", lambda r: r["expected_gate"] == "none"),
]


def rate(rows, keep, hit) -> float | None:
    subset = [r for r in rows if keep(r)]
    return sum(1 for r in subset if hit(r)) / len(subset) if subset else None


def show(name: str, values: list[float | None], count: int) -> None:
    real = [v for v in values if v is not None]
    if not real:
        return
    per = " ".join(f"{v:.0%}" for v in real)
    print(f"  {name:32s} n={count:3d}  {per}  mean {statistics.mean(real):.1%}")


def judged_only(reps: list[list[dict]]) -> list[list[dict]]:
    dropped = ("skipped", "malformed")
    return [[r for r in rows if r["score"]["verdict"] not in dropped] for rows in reps]


def gate_report(reps: list[list[dict]]) -> None:
    for name, keep in GATE_SLICES:
        values = [
            rate(rows, keep, lambda r: r["score"]["verdict"] == "hit") for rows in reps
        ]
        show(name, values, sum(1 for r in reps[0] if keep(r)))
    volume = [rate(rows, lambda r: True, lambda r: bool(r["labels"])) for rows in reps]
    show("offer volume", volume, len(reps[0]))
    counts = collections.Counter(len(r["labels"] or []) for rows in reps for r in rows)
    print(f"  {'options per offer':32s}       {dict(sorted(counts.items()))}")


def warn_malformed(reps: list[list[dict]]) -> None:
    """A malformed row means generation failed, not that the options were bad.
    Print it loudly: an earlier run scored 72 failed generations as misses."""
    bad = [
        sum(1 for r in rows if r["score"]["verdict"] == "malformed") for rows in reps
    ]
    if any(bad):
        print(f"  WARNING malformed (generation failed): {bad} of {len(reps[0])}")


def content_report(reps: list[list[dict]]) -> None:
    """Only the alternatives are judged. A click is a match by construction,
    so counting it here would inflate the result with a known answer."""
    warn_malformed(reps)
    rows_by_rep = [
        [r for r in rows if r["outcome"] == "alternative"] for rows in judged_only(reps)
    ]
    count = len(rows_by_rep[0])
    wording = {
        "match": "contained what they typed",
        "near": "was near what they typed",
        "miss": "missed what they typed",
    }
    for verdict, phrase in wording.items():
        values = [
            rate(rows, lambda r: True, lambda r, v=verdict: r["score"]["verdict"] == v)
            for rows in rows_by_rep
        ]
        show(f"offered set {phrase}", values, count)
    values = [
        rate(
            rows,
            lambda r: r["score"]["verdict"] in ("match", "near"),
            lambda r: r["score"].get("index") == 0,
        )
        for rows in rows_by_rep
    ]
    show("of those, the hit sat first", values, count)


def calibrate_report(reps: list[list[dict]]) -> None:
    """The user clicked one of these labels, so `match` at that index is the
    only correct answer. Anything else measures the judge, not the options."""
    warn_malformed(reps)
    rows_by_rep = judged_only(reps)
    count = len(rows_by_rep[0])
    show(
        "judge correct",
        [
            rate(rows, lambda r: True, lambda r: r["score"].get("correct"))
            for rows in rows_by_rep
        ],
        count,
    )
    for verdict in ("match", "near", "miss"):
        values = [
            rate(rows, lambda r: True, lambda r, v=verdict: r["score"]["verdict"] == v)
            for rows in rows_by_rep
        ]
        show(f"judge said {verdict}", values, count)


def predictable_report(reps: list[list[dict]]) -> None:
    """The control cases are provably predictable, so a low rate there means
    the judge is biased rather than the data being unpredictable."""
    rows_by_rep = judged_only(reps)
    for name, keep in (
        ("control (they clicked)", lambda r: r["score"]["control"]),
        ("the misses and alternatives", lambda r: not r["score"]["control"]),
    ):
        count = sum(1 for r in rows_by_rep[0] if keep(r))
        show(
            f"{name}: predictable",
            [
                rate(rows, keep, lambda r: r["score"]["predictable"] is True)
                for rows in rows_by_rep
            ],
            count,
        )
    alts = [[r for r in rows if not r["score"]["control"]] for rows in rows_by_rep]
    kinds = collections.Counter(r["score"].get("kind") for rows in alts for r in rows)
    print(f"\n  what the next message wanted: {dict(kinds.most_common())}")


RENDERERS = {
    "gate": gate_report,
    "predictable": predictable_report,
    "content": content_report,
    "calibrate": calibrate_report,
}


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else None
    if not tag:
        have = sorted(p.name for p in RUNS.iterdir() if p.is_dir())
        raise SystemExit(f"usage: report.py <tag>; have: {have}")
    directory = RUNS / tag
    manifest = json.loads((directory / "manifest.json").read_text())
    print(json.dumps(manifest, indent=2))
    by_variant: dict[str, list[list[dict]]] = collections.defaultdict(list)
    for path in sorted(directory.glob("*.jsonl")):
        _, variant, _ = path.stem.split("__")
        by_variant[variant].append(
            [json.loads(line) for line in path.read_text().splitlines() if line]
        )
    render = RENDERERS[manifest["scorer"]]
    for variant, reps in by_variant.items():
        print(f"\n--- {variant}, {len(reps)} rep(s) ---")
        render(reps)


if __name__ == "__main__":
    main()
