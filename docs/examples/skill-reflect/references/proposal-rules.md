# Proposal rules

These rules decide what a proposal may say. They apply to the `rule`, the `why`, and the patch file.

## What a skill is

A skill holds the instructions for one class of task: the steps in order, the commands that work, the decision points, and the pitfalls that cost time. A future session should follow it and get the result right the first time.

## Order of preference

Pick the first option that fits:

1. **Patch the skill that was in use.** It was in play when the lesson happened, so it is the right home.
2. **Patch a broader skill** that covers the same class of task.
3. **Add a reference file** under an existing skill, for depth that is only needed sometimes. Name it by topic, never by date or incident. Extend an existing reference file before you add one.
4. **Propose a new skill** only when nothing covers the class. Name it for the class of task. A name that only fits this week's task is wrong.

## How to write the rule

- Write the rule as an instruction: "Read the ticket's linked issues before you set the parent."
- Give one clause of why: the mechanism, not the story. "…because the parent field rejects an issue in another project."
- Attach the rule to the step it changes. Quote that step's current text in the patch file so the reviewer sees where it goes.
- One lesson is one rule. If the skill already has a weaker form of the rule, change that sentence. Do not add a second copy.
- If a sentence in the skill is wrong, replace it. Do not add a correction note under it.
- If the rule asks a person something, search `<schedule_file>` from `~/.claude-on-the-fly/skill-reflect.yaml` for an entry that runs the skill. If one does, the rule must also say what the unattended run does instead, because nobody is there to answer.

## What a proposal never contains

- Ticket numbers, PR numbers, dates, customer names, or people's names.
- Quotes from a conversation. Paraphrase the rule; the evidence ids point to the source.
- Anything from a DM that the person would not post in a channel.
- Secrets, tokens, hostnames of internal systems, or file paths under a user's home other than skill paths.
- A claim that a tool or feature does not work.
- A restatement of what the soul repo's own instructions (AGENTS.md, CLAUDE.md, SOUL.md) or the tool's own help already says.

## Ownership

| Owner in `candidates.json` | Allowed actions |
| --- | --- |
| `local` | `patch` |
| `upstream` | `patch` only. Apply mode turns it into a draft PR to the marketplace repo. |
| `third-party` | `note` only |
| `unknown`, or no skill in use | `new-skill`, under `<soul_root>/<new_skill_dir>/` |

A personal preference is a `profile-note`, whatever skill was in use. Propose one only when the config sets `profile_dir`. It must meet both of these:

- It is about how the agent works with that one person. A request about a report, a channel post, or any scheduled output that other people read is a change to that skill or job, even when one person asked for it.
- That person's `<profile_dir>/<id>/profile.md` does not already say it. Read the profile before you propose a note. If it states the same preference in other words, discard the cluster.
