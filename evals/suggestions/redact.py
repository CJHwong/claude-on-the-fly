"""Turn the private raw corpus into the redacted corpus the repository keeps.

The raw cases are real direct messages. They name people, customers, internal
services and local paths. None of that may enter this repository, so the raw
file stays gitignored and only the output of this script is committed.

Redaction runs in two passes. The first is deterministic and removes the
mechanical identifiers: links, addresses, ticket keys, user ids, home paths.
The second asks a model to rewrite the remaining prose into a neutral
equivalent, because a person's name or an internal service name inside free
text cannot be matched by a pattern.

    uv run python evals/suggestions/redact.py build
    uv run python evals/suggestions/redact.py check

`check` is the gate. It fails when a denied term survives, so a corpus can
never be committed on the assumption that the rewrite worked.
"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from lib import CORPUS, HERE, RAW, ask_model, digest, load_cases, parse_json_object

DENYLIST = HERE / "raw" / "denylist.txt"

TEXT_FIELDS = ("prior_user_text", "reply_text", "alternative_text")

# Structural identifiers. These carry no meaning the eval depends on, so they
# are replaced rather than rewritten.
PATTERNS = [
    (re.compile(r"https?://\S+"), "<link>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"<@[UW][A-Z0-9]+>"), "@person"),
    (re.compile(r"\b[UW][A-Z0-9]{8,}\b"), "@person"),
    (re.compile(r"/Users/[^/\s]+"), "/home/user"),
    (re.compile(r"\b[A-Z][A-Z0-9]{1,5}-\d+\b"), "<ticket>"),
]

NEUTRALIZE = """\
Rewrite the JSON below so it names nobody and no organization, while keeping \
its meaning as a conversation exactly intact.

Replace every person's name with a role word such as "a teammate". Replace \
every company, customer, product, internal service, repository, dashboard and \
tool name with a generic equivalent such as "the reporting service" or "the \
build tool". Replace concrete file paths with generic ones. Remove any \
identifier, credential or address that survived.

Keep everything else. Keep the language of each field: text in Chinese stays \
in Chinese. Keep the length, the tone, the level of detail, and above all keep \
the decision the conversation is at, because that is what is being measured. \
An option label must stay the same kind of action, the same specificity and \
under 75 characters.

Return only the rewritten JSON object, with exactly the same keys.

{payload}
"""


def scrub(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def neutralize(case: dict) -> dict:
    """Ask a model to rewrite one case's prose. Falls back to the scrubbed
    text, which `check` then rejects if anything private survived."""
    payload = {field: case[field] for field in TEXT_FIELDS if case[field]}
    payload["offered_labels"] = case["offered_labels"]
    rewritten = parse_json_object(
        ask_model(
            NEUTRALIZE.format(payload=json.dumps(payload, ensure_ascii=False, indent=2))
        )
    )
    if not rewritten:
        print(f"rewrite failed for {case['case_id']}", file=sys.stderr)
        return case
    for field in TEXT_FIELDS:
        if field in rewritten and isinstance(rewritten[field], str):
            case[field] = rewritten[field]
    labels = rewritten.get("offered_labels")
    if isinstance(labels, list) and len(labels) == len(case["offered_labels"]):
        case["offered_labels"] = [str(label)[:75] for label in labels]
    # The clicked label is a pointer into the offered set, so re-derive it
    # rather than rewriting it twice and letting the two drift apart.
    position = case["clicked_position"]
    if position != "":
        case["clicked_label"] = case["offered_labels"][int(position)]
    return case


def build(rewrite: bool = True) -> None:
    raw = load_cases(RAW)
    cases = []
    for row in raw:
        case = dict(row)
        case["case_id"] = digest(case.pop("permalink"))
        for field in TEXT_FIELDS:
            case[field] = scrub(case.get(field, ""))
        case["offered_labels"] = [scrub(label) for label in case["offered_labels"]]
        case["clicked_label"] = scrub(case["clicked_label"])
        cases.append(case)
    if rewrite:
        with ThreadPoolExecutor(max_workers=8) as pool:
            cases = list(pool.map(neutralize, cases))
    CORPUS.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS.open("w") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"redacted {len(cases)} cases -> {CORPUS}")


def check() -> int:
    """Fail when anything private survived the rewrite.

    Terms match on a word boundary, so a short acronym on the denylist cannot
    fire on an unrelated word that happens to contain it.
    """
    text = CORPUS.read_text()
    failures = [
        f"pattern {pattern.pattern}: {hit}"
        for pattern, _ in PATTERNS
        for hit in sorted(set(pattern.findall(text)))
    ]
    if DENYLIST.exists():
        for term in DENYLIST.read_text().splitlines():
            term = term.strip()
            if term and re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
                failures.append(f"denied term: {term}")
    else:
        print(f"no denylist at {DENYLIST}; structural checks only", file=sys.stderr)
    for failure in failures:
        print(f"LEAK {failure}", file=sys.stderr)
    print(f"{len(failures)} leaks in {CORPUS}")
    return 1 if failures else 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "build":
        # The scrub alone is free. It is useful to check the plumbing, and
        # `check` will reject its output, which is the point.
        build(rewrite="--no-rewrite" not in sys.argv)
    elif command == "check":
        sys.exit(check())
    else:
        raise SystemExit(f"unknown command {command!r}; use build or check")
