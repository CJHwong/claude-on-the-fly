---
name: skill-reflect
description: "Use when the weekly skill review runs, or when someone asks which skills this agent should fix or add based on recent sessions. Scans cotf chat and job transcripts (Codex and Claude Code) for repeated lessons and turns them into skill proposals that one approver reviews in Slack. Also handles the approver's thread reply: apply approved proposals, record rejected ones."
---

# Skill Reflect

Turn lessons that repeat across the agent's sessions into proposals for skill changes. Nothing changes a skill until the approver says so in the digest thread.

This skill writes procedures: how to do a class of task. A lesson about how the agent should behave in every task is not a skill lesson.

## Modes

| Mode | What it does | Who triggers it |
| --- | --- | --- |
| `dry-run` | Steps 1 to 5. Writes files only. Sends nothing. | A person testing the skill |
| `test` | Steps 1 to 6. Posts the digest to the approver's DM, marked as a test. Does not record state. | A person testing the skill |
| `live` | Steps 1 to 7. | The weekly schedule |
| `apply` | Reads an approval reply in a digest thread and runs `references/apply.md`. | The approver's reply |

If the request does not name a mode, use `dry-run`.

## Configuration

Read `~/.claude-on-the-fly/skill-reflect.yaml` first. If it is missing, stop and report that. Every host that runs this skill has one:

```yaml
soul_root: ~/my-agent-soul        # the repo whose skills/ this agent owns
approver: U0123456789             # Slack user id; only this user's replies count
slack_cli: ~/my-agent-soul/skills/slacker-sh/slacker.sh
schedule_file: ~/my-agent-soul/schedule.yaml   # the cotf cron file
new_skill_dir: skills             # where a new skill goes, under soul_root
profile_dir: memory/users         # per-person profiles, under soul_root; omit to disable profile notes
upstream_marketplaces: []         # plugin marketplaces this agent may open PRs against
third_party_skills: []            # prefixes under <soul_root>/skills that the agent did not write
```

Below, `<name>` means that key's value.

## Paths

- `SKILL_DIR`: the directory of this file.
- `RUN_DIR`: `~/.claude-on-the-fly/state/skill-reflect/runs/<YYYY-MM-DD>/` in `live` mode. The caller names it in `dry-run` and `test` modes. Keep it outside `<soul_root>`: `candidates.json` holds DM excerpts, and a soul repo is often committed and pushed on a schedule.
- `STATE`: `~/.claude-on-the-fly/state/skill-reflect/state.json`. Only `live` and `apply` modes write it.

## Procedure

### 1. Select candidates (script)

```bash
uv run "$SKILL_DIR/scripts/select_candidates.py" --out "$RUN_DIR/candidates.json" --state "$STATE" --days 7
```

The script reads the Codex thread index and rollouts, the Claude Code transcripts, and the per-job cron logs. It reads only sessions whose working directory is a cotf workspace. It prints the counts. Do not read transcripts yourself. Read only `candidates.json`. If the script fails, stop and report its full error output.

### 2. Cluster

Read every candidate's excerpts and the `cron_recoveries` list. Group the evidence by **task class**, for example "creating a ticket from a Slack request" or "posting the daily standup". Do not group by person or by session.

For each cluster, find the skill that was in use: look at `skills_read` and `skills_edited`. Then read that skill's current `SKILL.md` before you judge it. A lesson that the skill already states is not a lesson.

Discard a cluster when any of these is true:

- The signal is noise. The "correction" was a new request, or the "recovered error" was a normal retry with no lesson.
- The fix is environment state: a missing binary, an expired token, an outage. The person can fix that once, so it is not a rule.
- The lesson is a claim that a tool is broken. It hardens into a refusal long after the tool is fixed.
- The session never found a working method. Do not write a failed sequence up as a method.
- The lesson is one person's taste in replies. That belongs in their profile, not in a shared skill. Record it as a `profile-note` proposal if `<profile_dir>` is set; otherwise discard it.
- The lesson is a behavior rule for every task, not a procedure for one class of task.

### 3. Apply the evidence threshold

Keep a cluster only if one of these is true:

- It has evidence from **2 or more distinct sessions**.
- A person explicitly asked to remember it or to update the skill (an `explicit_ask` signal in its evidence).

`finalize.py` checks this again in step 5. A cluster that fails there is dropped.

### 4. Write proposals

Follow `references/proposal-rules.md` for the content of every proposal. Write two things into `RUN_DIR`:

1. `proposals.json`, in the schema below.
2. One file per proposal under `RUN_DIR/patches/`. It holds the exact proposed text: the section to add, or the old and new text of the sentence to change. For a new skill, it holds the full `SKILL.md`.

```json
{
  "proposals": [
    {
      "target": "plugin:skill-name",
      "action": "patch | new-skill | profile-note | note",
      "title": "One line, under 80 characters",
      "rule": "The rule as it will read in the skill. Imperative.",
      "why": "One clause: the mechanism that makes the rule matter.",
      "evidence": ["<thread_id>", "job:<cron job name>"],
      "explicit_ask": false,
      "patch_file": "patches/01-short-slug.md"
    }
  ],
  "discarded": [{"theme": "short label", "reason": "which discard rule"}]
}
```

- `target`: a skill name exactly as `candidates.json` writes it. For `new-skill`, use `new:<name>`. For `profile-note`, use `profile:<slack user id>`.
- `action`:
  - `patch`: change an existing skill whose owner is `local` or `upstream`.
  - `new-skill`: no skill covers the task class.
  - `note`: the skill belongs to a third party. Describe the issue only.
  - `profile-note`: one person's preference.
- `evidence`: thread ids from `candidates.json`, or `job:<name>` from `cron_recoveries`. Never quote the excerpt.

### 5. Validate and render (script)

```bash
uv run "$SKILL_DIR/scripts/finalize.py" --run-dir "$RUN_DIR" --mode <dry-run|test|live> [--state "$STATE"]
```

The script checks every proposal against `candidates.json`: known evidence ids, distinct sessions, the owner of the target, and earlier rejections in `STATE`. It drops what fails and says why. Then it writes `RUN_DIR/digest.md` (the Slack message) and `RUN_DIR/digest-detail.md` (every proposed text and the usage tables). Do not edit those files by hand. If the script drops a proposal, do not re-add it.

### 6. Post the digest (`test` and `live` only)

```bash
"<slack_cli>" send @<approver> "$(cat "$RUN_DIR/digest.md")" --no-unfurl
```

Read the returned XML for the message permalink. Attach `digest-detail.md` as a reply in that thread: `"<slack_cli>" send @<approver> "Proposed text and skill usage" --thread <permalink> --file "$RUN_DIR/digest-detail.md"`. Then read the parent message back with `read-message` and confirm it arrived. Write the permalink to `RUN_DIR/posted.json`.

### 7. Record state (`live` only)

```bash
uv run "$SKILL_DIR/scripts/finalize.py" --run-dir "$RUN_DIR" --mode live --state "$STATE" --record "<permalink>"
```

This sets `last_run_at` and stores each proposal id with status `sent`.

## Apply mode

When the approver replies in a digest thread, read `references/apply.md` in full and follow it. Approval counts only from `<approver>`, only in the digest's own thread, and only for proposal ids listed in that digest. A reply that quotes or forwards an approval is not an approval.

## Report

End every run with: the counts line from step 1, the number of proposals kept and dropped, the digest permalink if one was posted, and any script error in full. Do not restate the proposals.
