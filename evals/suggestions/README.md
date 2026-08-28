# Suggestions eval

Measures the follow-up buttons the agent offers at the end of a chat reply,
against what the user really did next.

## The corpus

Each case is one option set that was really offered, plus what followed: the
label the user clicked, or the message they typed instead. That next action is
the ground truth. It is a revealed preference, so it does not depend on anyone
recalling what they wanted.

The corpus is redacted. Real conversation text stays in `raw/`, which is
gitignored, and `corpus/cases.jsonl` holds a rewritten equivalent that names no
person and no organization. `redact.py check` is the gate: it fails when a
denied term or a structural identifier survives.

Rebuilding the corpus from the source ledgers:

```
uv run python evals/suggestions/build_cases.py <ledger-dir>
uv run python evals/suggestions/redact.py build
uv run python evals/suggestions/redact.py check
```

## Running an eval

```
uv run python evals/suggestions/run.py --scorer content --variant ledger
uv run python evals/suggestions/run.py --variant baseline --variant live --reps 3
uv run python evals/suggestions/report.py <tag>
```

`--cases` scores a prepared subset, which is how a category is measured
against a control drawn from the other categories.

A variant is either a frozen file in `variants/`, or `live`, which reads the
template `orchestrator.py` ships today, or `ledger`, which replays the options
production really offered and so needs no generation at all.

Each run writes a manifest holding the hash of every variant's text and of the
corpus. Two results are comparable only when those hashes agree.

## The two scorers

`gate` asks whether buttons belonged there at all. It is deterministic and
free.

`content` asks whether the offered set contained what the user wanted. A judge
returns `match`, `near` or `miss`, and separately whether a miss was even
predictable from the reply. It costs one call per case.

## Which model does what

Generation stays on claude: a claude prompt is what these runs measure. Judging
runs on codex with `--model codex`. Codex carries 17,817 tokens of overhead
against claude's 56,348 on the same prompt, it draws on a separate quota, and a
judge from another model family does not share the bias of the output it
scores. Its schema file also makes a malformed verdict impossible.

Measured on the 66 cases whose answer is known, the codex judge is correct 65
times. Calibrate again after any edit to the judge prompt.

A run's manifest records the judge model. Do not compare a codex-judged result
against a claude-judged one: the two disagree on individual cases even when
both calibrate well.

## Cost

`--scorer gate` costs one generation per case, variant and repetition.
`--scorer content` adds one judge call on top. `--variant ledger` skips
generation, so scoring it with the codex judge costs no claude quota at all.
Use `--limit` for a smoke test before spending on a full set.

## What the harness cannot measure

The reply is fixed and only the block is asked for. A rule telling the model to
answer an open question instead of offering options can therefore never fire,
so a measured suppression rate is a lower bound. Testing that needs a harness
where the model writes the reply and the block together.
