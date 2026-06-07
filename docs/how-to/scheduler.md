# Scheduler

Run Claude prompts (and shell scripts) on a cron schedule. Write a YAML config to `~/.claude-on-the-fly/schedule.yaml`:

```yaml
jobs:
  - name: standup-mazu
    cron: "30 6 * * 1-5"          # Mon-Fri 06:30
    prompt: "/gf-ops:daily mazu — post to the team channel. No confirmation."
    timeout: 1800                 # optional, default 1800s

  - name: release-bot
    cron: "0 18 * * 1-5"
    script: ~/scripts/release-bot.sh   # shell escape hatch for multi-step jobs
    args: ["--verbose"]
    timeout: 1800
```

Each job needs `prompt` (goes through Claude with a fresh session every fire) OR `script` (runs as a subprocess). Output goes to `~/.claude-on-the-fly/logs/schedule-<name>.log`. Edits to the config are picked up within a minute, no restart required.

```bash
uv run claude-schedule
# or: uv run claude-schedule --config /path/to/schedule.yaml
```
