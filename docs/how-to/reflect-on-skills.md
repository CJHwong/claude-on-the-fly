# Review skill proposals from past sessions

The example `skill-reflect` skill under `docs/examples/skills/skill-reflect/` reads the agent's past cotf sessions and finds lessons that repeat. It turns those lessons into proposed skill changes. It posts them to one approver in Slack. Nothing changes a skill until the approver replies `approve <n>` in the digest thread.

It reads Codex and Claude Code transcripts whose working directory is a cotf workspace, and the per-job cron logs. Transcripts outlive the logs, so a first run can scan months of history.

## 1. Link the skill into the agent's skills directory

```bash
git clone https://github.com/CJHwong/claude-on-the-fly ~/claude-on-the-fly-src
ln -s ~/claude-on-the-fly-src/docs/examples/skills/skill-reflect ~/my-agent-soul/skills/skill-reflect
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

## 3. Dry run

Ask the agent in a chat:

```
Use skill-reflect in dry-run mode with RUN_DIR /tmp/skill-reflect-dry and --days 30.
```

It writes `candidates.json`, `proposals.json`, `digest.md` and `digest-detail.md` to that directory and sends nothing. Read `digest.md`. Each proposal names its target skill, the rule, and how many sessions back it.

## 4. Test digest

Ask for `test` mode. The agent posts the digest to the approver's DM, marked as a test. A reply to a test digest is never applied.

## 5. Schedule the weekly run

Add a prompt job to the cron file:

```yaml
- name: skill-reflect
  cron: "40 9 * * 1"
  timeout: 3600
  prompt: |
    Use skill-reflect in live mode. No confirmation needed.
```

The first live run scans the last 7 days. Each later run starts where the last one ended.
