"""Build the private raw corpus from the source ledgers.

    uv run python evals/suggestions/build_cases.py evals/suggestions/raw/v3

The directory must hold the eval ledger and the six-kind label file. Output is
the raw corpus, which stays gitignored. Run `redact.py build` afterwards to
produce the redacted corpus this repository keeps.

Only rows carrying eval text can form a case. The rest are labels and links
only, withheld by the audit's privacy rule. Do not recover them from the chat
platform: the ledger withholds them on purpose.
"""

import csv
import json
import sys
from pathlib import Path

from lib import RAW

# What each observed outcome implies about whether buttons belonged there. A
# click proves they did. A question the reply did not raise, or information
# only the user held, proves they did not. The rest is softer: it proves the
# offered set was wrong, not that no set would have worked.
GATE = {
    "confirm": ("buttons", "soft"),
    "widen": ("buttons", "soft"),
    "act": ("buttons", "soft"),
    "specific_question": ("none", "hard"),
    "new_information": ("none", "hard"),
    "decline": ("none", "hard"),
}
# A thread that simply finished needed no button. An abandoned one is the same
# signal, weaker. The other classes are measurement artifacts, not evidence.
UNRESOLVED = {"finished_thread": "hard", "likely_abandonment": "soft"}
USABLE = ("full_eval_text", "partial_eval_text")


def build(source: Path) -> list[dict]:
    labels = {
        row["source_permalink"]: row["alternative_kind"]
        for row in csv.DictReader(
            (source / "option-alternative-six-kind-labels.csv").open()
        )
    }
    cases = []
    for row in csv.DictReader((source / "option-feature-eval-ledger.csv").open()):
        if row["eval_text_usable"] not in USABLE:
            continue
        kind = labels.get(row["source_permalink"], "")
        gate, strength = resolve(row, kind)
        if not gate:
            continue
        cases.append(
            {
                "permalink": row["source_permalink"],
                "context_type": row["context_type"],
                "message_kind": row["message_kind"],
                "outcome": row["outcome"],
                # The weak label is deterministic and rule-based, so it selects
                # cases. It does not score them: the calibrated judge does that.
                "alt_kind": kind,
                "text_scope": row["text_scope"],
                "eval_text": row["eval_text_usable"],
                "unresolved_class": row["unresolved_class"],
                "expected_gate": gate,
                "strength": strength,
                "offered_labels": json.loads(row["offered_labels"] or "[]"),
                "clicked_label": row["clicked_label"],
                "clicked_position": row["clicked_position"],
                "prior_user_text": row["preceding_user_text"][:1500],
                "reply_text": row["reply_text"][:2500],
                "alternative_text": row["alternative_text"][:600],
            }
        )
    return cases


def resolve(row: dict, kind: str) -> tuple[str, str]:
    if row["outcome"] in ("selected", "verbatim_no_click"):
        return "buttons", "hard"
    if row["outcome"] == "alternative":
        return GATE.get(kind, ("", ""))
    if row["outcome"] == "unresolved":
        strength = UNRESOLVED.get(row["unresolved_class"], "")
        return ("none", strength) if strength else ("", "")
    return "", ""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cases = build(Path(sys.argv[1]))
    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"built {len(cases)} raw cases -> {RAW}", file=sys.stderr)


if __name__ == "__main__":
    main()
