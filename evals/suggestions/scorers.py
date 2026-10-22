"""How an option set is judged against what the user really did.

`gate` asks whether buttons belonged there at all. It is deterministic and
free. It is also the question a measured run already answered: gating harder
buys quiet and costs clicks.

`content` asks the question that has never been measured. For every case the
ledger records what the user did next, either the label they clicked or the
message they typed instead. That is the closest thing to ground truth this
data has, a revealed preference rather than a stated one. The judge decides
whether the offered set already contained it.

`calibrate` measures the judge itself. A case where the user clicked a button
has a known answer, so any other verdict is judge error.
"""

import json

from lib import HERE, ask_codex, ask_model, parse_json_object

# The reply comes last and is explicitly demoted. An earlier version led with
# it, and the judge answered a different question: whether a label was a
# sensible thing to offer, rather than whether it was what the user asked for.
# That cost 5 false misses on 33 cases whose answer was known.
JUDGE = """\
A user was shown buttons. Decide whether any button label means the same thing \
as what the user asked for next.

WHAT THE USER ASKED FOR:
{truth}

BUTTON LABELS:
{labels}

The assistant message the buttons appeared under, given only so you can read \
the wording in context. Do not judge whether a label was a sensible thing to \
offer here, and do not reason about whether it follows from this message. \
Judge only whether a label corresponds to what the user asked for.

{reply}

Answer with one JSON object and nothing else:

{{"verdict": "match" | "near" | "miss",
  "index": <zero-based index of the closest label, or null>,
  "why": "<one short sentence>"}}

`match` means a label meant the same thing the user asked for, however \
differently it is worded. `near` means a label covers the right subject at the \
wrong scope or the wrong level of detail. `miss` means nothing offered \
corresponds.
"""

CLICKED = ("selected", "verbatim_no_click")


def gate(case: dict, labels: list[str] | None, model: str = "") -> dict:
    if labels is None:
        return {"verdict": "malformed"}
    predicted = "buttons" if labels else "none"
    return {
        "verdict": "hit" if predicted == case["expected_gate"] else "miss",
        "predicted": predicted,
    }


JUDGE_SCHEMA = HERE / "schemas" / "judge.json"


def judge(case: dict, labels: list[str], truth: str, model: str) -> dict:
    prompt = JUDGE.format(
        truth=truth,
        labels=json.dumps(labels, ensure_ascii=False, indent=2),
        reply=case["reply_text"],
    )
    raw = (
        ask_codex(prompt, schema=JUDGE_SCHEMA)
        if model == "codex"
        else ask_model(prompt, model=model)
    )
    judged = parse_json_object(raw)
    if not judged or judged.get("verdict") not in ("match", "near", "miss"):
        return {"verdict": "malformed"}
    return judged


def content(case: dict, labels: list[str] | None, model: str = "sonnet") -> dict:
    """Judge one option set against what the user did next.

    A click needs no judge: the clicked label is in the offered set by
    construction, so the verdict is known and free. Only the message a user
    typed instead of clicking is an open question.
    """
    if case["outcome"] in CLICKED and case["clicked_position"] != "":
        return {
            "verdict": "match",
            "index": int(case["clicked_position"]),
            "why": "the user clicked this label",
        }
    if case["outcome"] != "alternative" or not case["alternative_text"].strip():
        return {"verdict": "skipped", "why": "no alternative recorded"}
    if labels is None:
        return {"verdict": "malformed", "why": "no block generated"}
    if not labels:
        return {"verdict": "miss", "index": None, "why": "empty block offered"}
    return judge(case, labels, case["alternative_text"], model)


def calibrate(case: dict, labels: list[str] | None, model: str = "sonnet") -> dict:
    """Score the judge, not the options.

    Only a case the user clicked carries a known answer. The judge is shown
    the clicked label as the thing asked for, so `match` at that index is the
    one correct result and anything else is judge error.
    """
    if case["outcome"] not in CLICKED or case["clicked_position"] == "":
        return {"verdict": "skipped", "why": "no known answer"}
    if labels is None:
        return {"verdict": "malformed", "why": "no block generated"}
    if not labels:
        return {"verdict": "skipped", "why": "empty block offered"}
    result = judge(case, labels, case["clicked_label"], model)
    position = int(case["clicked_position"])
    result["correct"] = result["verdict"] == "match" and result.get("index") == position
    result["expected_index"] = position
    return result


# Predictability is asked on its own, never alongside a verdict. Bundled into
# the content judge it collapsed: every one of 52 misses came back
# unpredictable, because the judge justified the verdict it had just given.
# This prompt never sees the offered labels and is never told a set failed.
PREDICTABLE = """\
Below is a chat turn: what a user wrote, and how the assistant replied. Then \
comes the user's next message.

Judge one thing. Before that next message arrived, could a thoughtful \
assistant have anticipated it well enough to offer it as one of three \
follow-up options?

Say true when the next message follows from what the reply left open: an \
explanation the reply invites, a check on a claim the reply makes, the \
obvious next step, or a step back to the wider goal.

Say false when the next message brings information only the user had, changes \
the subject, gives a new instruction, or asks something specific that nothing \
in the reply points to.

USER:
{user}

ASSISTANT:
{reply}

THE USER'S NEXT MESSAGE:
{truth}

Answer with one JSON object and nothing else:

{{"predictable": true | false,
  "kind": "explain" | "confirm" | "widen" | "act" | "new-information" | "specific-question",
  "why": "<one short sentence>"}}

`kind` describes what the next message wants. `explain` asks why or how. \
`confirm` checks whether something the assistant claimed is true or done. \
`widen` steps up to the broader goal. `act` asks for the next concrete step. \
`new-information` supplies something the assistant could not have known. \
`specific-question` asks a narrow factual question the reply did not raise.
"""


def predictable(case: dict, labels: list[str] | None, model: str = "sonnet") -> dict:
    """Could any option set have caught what the user said next?

    A clicked case is the control: a button did contain what the user wanted,
    so `predictable` must come back true. A judge that fails the control is
    measuring its own bias, which is what the first attempt did.
    """
    if case["outcome"] in CLICKED and case["clicked_position"] != "":
        truth, control = case["clicked_label"], True
    elif case["outcome"] == "alternative" and case["alternative_text"].strip():
        truth, control = case["alternative_text"], False
    else:
        return {"verdict": "skipped", "why": "no next message recorded"}
    answer = parse_json_object(
        ask_model(
            PREDICTABLE.format(
                user=case["prior_user_text"] or "(no prior user message)",
                reply=case["reply_text"],
                truth=truth,
            ),
            model=model,
        )
    )
    if not answer or not isinstance(answer.get("predictable"), bool):
        return {"verdict": "malformed"}
    answer["verdict"] = "judged"
    answer["control"] = control
    return answer


SCORERS = {
    "gate": gate,
    "content": content,
    "calibrate": calibrate,
    "predictable": predictable,
}
