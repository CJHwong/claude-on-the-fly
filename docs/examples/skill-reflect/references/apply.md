# Apply mode

Use this file when the approver replies in a skill-reflect digest thread.

**Warning:** A wrong apply changes a skill that every session of this agent loads. Apply only what the approver named, in the approver's own words, in that thread.

## 1. Confirm the reply is an approval

All of these must be true. If one is false, reply once in the thread with what is missing, then stop.

1. The reply comes from the `approver` in `~/.claude-on-the-fly/skill-reflect.yaml`.
2. The reply is in the thread of a message that this agent posted from `digest.md`.
3. The reply names proposal numbers from that digest, for example `approve 1,3` or `reject 2: too narrow`.
4. The digest is not marked as a test. A test digest is never applied. Reply that it was a test and stop.

## 2. Find the run

Read `posted.json` in each `~/.claude-on-the-fly/state/skill-reflect/runs/*/` directory to find the run whose permalink matches the thread. Load that run's `proposals.final.json`. The numbers in the reply are the `n` fields there.

## 3. Apply each approved proposal

Handle one proposal at a time. Read its patch file and the current target file before you change anything. If the target changed since the digest and the patch no longer fits, do not force it. Report that proposal as stale.

For a local patch, compare the current skill hash with `versions.source_sha256`.
Compare the proposal hash with `versions.candidate_sha256`. If either differs, mark the proposal stale and stop its application.
The proposal contains the complete proposed skill. Apply those exact bytes after approval.
Check that the resulting skill hash equals `versions.candidate_sha256`.
For an older proposal with `evaluation`, use its `source_skill_sha256` and `candidate_sha256` fields instead.
If neither record exists, regenerate the digest and obtain approval again.

| Action | Steps |
| --- | --- |
| `patch`, owner `local` | Apply the complete approved skill under `<soul_root>/skills/` after the hash checks above. Commit it as the soul repo requires. Push only if those instructions require it. |
| `new-skill` | Create the skill directory under `<soul_root>/<new_skill_dir>/` from the patch file. Commit as above. |
| `patch`, owner `upstream` | Work in a clone of the marketplace repo, on a new branch, following that repo's own contribution rules. Apply the patch to the plugin's source skill, not the installed cache. Run the focused eval below. Open a **draft** PR and put the eval scores in its body. The approver marks it ready. Never change the copy under a `plugins/cache/` directory. |
| `profile-note` | Add the preference to `<soul_root>/<profile_dir>/<id>/profile.md` for that user only. |
| `note` | Nothing to apply. Mark it applied. |

## Focused eval for an upstream patch

The skill's full suite can cost more than the change is worth. A small focused set shows whether the new rule changes behavior.

1. Add cases to the skill's `evals/evals.json`: one per branch of the new rule (for example, attended and unattended when the rule depends on the run mode), and one where nothing in the fixture points at the new rule. A case that hands the agent the answer passes on the old text too, so it proves nothing. Check every expectation against the skill text: an expectation the skill cannot meet by design, such as a field its template does not have, fails on both versions and also proves nothing.
2. Use the current harness and available tools. Run each case against both versions. Grade without version labels.
3. In a case prompt, ask for "the evidence you would inspect and the source you would read it from, then a draft answer". Request observable actions and outputs only. Do not request private reasoning.
4. If the patched text does not beat the original on at least one case, report that in the thread and stop. Do not open the PR.

## 4. Record and reply

1. Update `~/.claude-on-the-fly/state/skill-reflect/state.json`: set each proposal's status to `applied`, `rejected` (with the approver's reason), or `stale`.
2. Reply once in the thread. For each proposal number, give the result: the commit, the draft PR link, `rejected`, or `stale` with the reason.

For an applied local patch, retain `versions` (or the older `evaluation`) and record `applied_version`, `applied_at`, and the commit.
Set `followup` to `{"status": "pending", "evidence": []}`.
The next weekly review checks later sessions for recurrence and regressions.
That review records observations, not proof that the patch caused the result.
