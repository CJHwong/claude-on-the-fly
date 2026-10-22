"""Run one eval: some variants, one scorer, N repetitions.

    uv run python evals/suggestions/run.py --scorer content --variant ledger
    uv run python evals/suggestions/run.py --variant baseline --variant live --reps 3

Every run writes a manifest recording the hash of each variant's text and of
the corpus. Two result sets are comparable only when those hashes agree, so a
template edit can no longer silently redefine what an old number meant.
"""

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lib import (
    RAW,
    RUNS,
    ask_codex,
    ask_model,
    corpus_digest,
    digest,
    load_cases,
    load_variant,
    parse_block,
)
from scorers import SCORERS

# The reply is fixed and only the block is asked for, so the one thing that
# varies between two runs is the template's decision. This cannot exercise a
# rule that tells the model to answer a question instead of offering options:
# such a rule needs a harness where the model writes the reply too.
HARNESS = """\
You are the assistant in a Slack conversation. The turn below already \
happened: the user wrote USER, and you replied REPLY. Your reply text is fixed \
and must not change.

Following the instruction block, output ONLY the <suggestions> block that your \
reply should have ended with. No other text.

USER:
{user}

REPLY:
{reply}

{template}
"""

# `ledger` replays the options the feature really offered in production. It
# needs no generation, so it prices a content run at one judge call per case
# and measures the shipped behaviour rather than a candidate.
LEDGER = "ledger"


def generate(case: dict, template: str, model: str) -> list[str] | None:
    """Produce the block one template would have emitted for a real turn.

    Generating on codex measures the codex backend, which this project also
    ships, rather than the claude one it defaults to. The two are separate
    questions: a template that works on one need not work on the other. No
    output schema is imposed here, because the block format is part of what
    the template is being asked to produce.
    """
    prompt = HARNESS.format(
        user=case["prior_user_text"] or "(no prior user message)",
        reply=case["reply_text"],
        template=template,
    )
    raw = (
        ask_codex(prompt)
        if model == "codex"
        else ask_model(
            prompt,
            model=model,
            system="You are a helpful assistant in a chat product.",
        )
    )
    return parse_block(raw)


def reusable(tag: str, manifest: dict) -> dict[tuple[str, int, str], dict]:
    """Rows from an earlier run that are still valid for this one.

    A stored row can be reused only when the corpus, the variant text, the
    generating model and the judge all match. Those four are what the manifest
    exists to record, so a template edit or a judge swap silently invalidates
    nothing: it simply stops matching.
    """
    directory = RUNS / tag
    if not directory.exists():
        raise SystemExit(f"no run tagged {tag!r} under {RUNS}")
    old = json.loads((directory / "manifest.json").read_text())
    # The corpus hash is deliberately not checked. A stored row is keyed by
    # case, so it stays valid whatever else was scored alongside it. Requiring
    # the whole set to match would make a subset run unable to reuse a superset
    # run, which is the common case.
    for field in ("model", "judge", "scorer"):
        if old.get(field) != manifest.get(field):
            print(
                f"cannot reuse {tag}: {field} differs "
                f"({old.get(field)} vs {manifest.get(field)})",
                file=sys.stderr,
            )
            return {}
    rows: dict[tuple[str, int, str], dict] = {}
    for path in directory.glob("*.jsonl"):
        _, variant, rep = path.stem.split("__")
        if old["variants"].get(variant) != manifest["variants"].get(variant):
            continue
        index = int(rep.removeprefix("rep"))
        for line in path.read_text().splitlines():
            if line:
                row = json.loads(line)
                rows[(variant, index, row["case_id"])] = row
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", default=None)
    parser.add_argument("--scorer", default="gate", choices=sorted(SCORERS))
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--model", default="sonnet", help="generation model")
    parser.add_argument("--judge", default="sonnet", help="scoring model, or codex")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--limit", type=int, default=0, help="smoke-test a few cases")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--reuse",
        default="",
        help="take matching results from an earlier tag instead of paying again",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="score the private raw corpus instead of the redacted one",
    )
    parser.add_argument("--only", default="", help="keep one outcome, e.g. alternative")
    parser.add_argument("--cases", default="", help="score a prepared case file")
    args = parser.parse_args()

    variants = args.variant or ["baseline", "live"]
    templates = {n: "" if n == LEDGER else load_variant(n) for n in variants}
    source = Path(args.cases) if args.cases else (RAW if args.raw else None)
    cases = load_cases(source)
    if args.only:
        cases = [c for c in cases if c["outcome"] == args.only]
    if args.limit:
        cases = cases[: args.limit]
    manifest = {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "scorer": args.scorer,
        "model": args.model,
        "judge": args.judge,
        "reps": args.reps,
        "cases": len(cases),
        "corpus": corpus_digest(cases),
        "raw": args.raw,
        "variants": {n: digest(t) for n, t in templates.items()},
    }
    cached = reusable(args.reuse, manifest) if args.reuse else {}
    tag = args.tag or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUNS / tag
    out.mkdir(parents=True, exist_ok=True)
    scorer = SCORERS[args.scorer]

    def one(case: dict, name: str, rep: int) -> dict:
        hit = cached.get((name, rep, case["case_id"]))
        if hit:
            return hit
        labels = (
            case["offered_labels"]
            if name == LEDGER
            else generate(case, templates[name], args.model)
        )
        return {
            "case_id": case["case_id"],
            "outcome": case["outcome"],
            "alt_kind": case.get("alt_kind", case.get("alt_type", "")),
            "expected_gate": case["expected_gate"],
            "strength": case["strength"],
            "clicked_position": case["clicked_position"],
            "labels": labels,
            "score": scorer(case, labels, args.judge),
        }

    for name in variants:
        # The ledger's options are a fact, not a sample. Repeating it would
        # only repeat the judge, and the gate scorer is deterministic anyway.
        reps = 1 if name == LEDGER and args.scorer == "gate" else args.reps
        for rep in range(reps):
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                rows = list(pool.map(lambda c, n=name, i=rep: one(c, n, i), cases))
            saved = sum(1 for c in cases if (name, rep, c["case_id"]) in cached)
            path = out / f"{args.scorer}__{name}__rep{rep}.jsonl"
            with path.open("w") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            note = f", {saved} reused" if saved else ""
            print(f"{name} rep {rep}: {len(rows)} cases{note} -> {path}", flush=True)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest -> {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
