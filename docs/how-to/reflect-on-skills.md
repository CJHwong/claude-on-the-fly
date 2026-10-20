# Review skill proposals from past sessions

The example `skill-reflect` skill under `docs/examples/skill-reflect/` reads the agent's past cotf sessions and finds lessons that repeat. It turns those lessons into proposed skill changes. It posts them to one approver in Slack, by DM or in a channel. Nothing changes a skill until the approver replies `approve <n>` in the digest thread.

It reads Codex and Claude Code transcripts whose working directory is a cotf workspace, and the per-job cron logs. Transcripts outlive the logs, so a first run can scan months of history.

## 1. Link the skill into the agent's skills directory

```bash
git clone https://github.com/CJHwong/claude-on-the-fly ~/claude-on-the-fly-src
ln -s ~/claude-on-the-fly-src/docs/examples/skill-reflect ~/my-agent-soul/skills/skill-reflect
```

Use the directory the backend loads skills from. A later `git pull` in the clone updates the skill.

## 2. Write the config

Create `~/.claude-on-the-fly/skill-reflect.yaml`. `SKILL.md` lists every key. The minimum:

```yaml
soul_root: ~/my-agent-soul
approver: U0123456789
slack_cli: ~/my-agent-soul/skills/slacker-sh/slacker.sh
schedule_file: ~/.claude-on-the-fly/cron.yaml
new_skill_dir: skills
```

The skill keeps its run files and state under `~/.claude-on-the-fly/state/skill-reflect/`. The run files hold DM excerpts, so they stay out of the soul repo.

Local skill patches now require paired evaluation before they enter the approval digest.
The runner compares frozen versions on transcript-derived cases and an unaffected control.
It saves outputs, grades, and hashes. Ties, regressions, stale skills, and altered outputs fail validation.
The approver reviews the exact full skill that passed evaluation.
After application, the weekly review checks later evidence against the recorded version.

This evaluation uses model calls and measures plans and draft answers.
It does not replay live writes or prove that a procedure works with real services.
Keep raw transcripts outside the repository. Remove secrets and identifiers before model calls.

## 3. Dry run

Ask the agent in a chat:

```
Use skill-reflect in dry-run mode with RUN_DIR /tmp/skill-reflect-dry and --days 30.
```

It writes `candidates.json`, `proposals.json`, `digest.md` and `digest-detail.md` to that directory and sends nothing. Read `digest.md`. Each proposal names its target skill, the rule, and how many sessions back it.

## 4. Test digest

Ask for `test` mode. The agent posts the digest where `digest_to` points (the approver's DM by default), marked as a test. A reply to a test digest is never applied.

To post the digest in a channel instead, set `digest_to` to the channel id. Only the approver's reply still counts, and in a channel that reply must tag the agent.

## 5. Schedule the weekly run

Copy the entry in `docs/examples/skill-reflect/cron.yaml` into your cron file.

The first live run scans the last 7 days. Each later run starts where the last one ended.
