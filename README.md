# claude-on-the-fly

> **Experimental.** This is a personal project, not production software. It spawns Claude Code with `--permission-mode bypassPermissions`, meaning Claude has full read/write access to files on the host machine within its workspace. Use at your own risk. Do not run this on a machine with sensitive data you wouldn't want an LLM to access.

Remote access to Claude Code via Slack and Telegram. Send a message, get Claude working on it, see the response with cost/latency/token stats.

## Important: API Key Required

Using a Claude subscription (Max/Pro) OAuth token with Claude Code in any external tool violates [Anthropic's Consumer Terms of Service](https://docs.anthropic.com/en/docs/claude-code/legal-and-compliance). You must use an [Anthropic API key](https://console.anthropic.com/settings/keys) (`ANTHROPIC_API_KEY`) instead.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated with an API key (`ANTHROPIC_API_KEY`)

## Quick Start (no clone needed)

```bash
# Telegram
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_ALLOWED_USER_ID=...
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-telegram

# Slack
# first time: generate the app manifest to paste into Slack (asks 3 questions)
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack --manifest
export SLACK_APP_TOKEN=xapp-...
export SLACK_TOKEN=xoxb-...   # xoxp- replies as you, xoxb- replies as the app
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack

# Cron (scheduled prompts, and tracker polling via a shell producer)
# write ~/.claude-on-the-fly/cron.yaml (see docs/how-to/cron.md)
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-cron
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-jobs  # runs what cron queues
```

## Local Development

```bash
git clone https://github.com/CJHwong/claude-on-the-fly && cd claude-on-the-fly
cp .env.example .env  # fill in your tokens
uv sync
uv run claude-tui    # supervisor TUI: start/stop daemons, tail logs, doctor
# or run a daemon directly:
uv run claude-slack  # or claude-telegram, claude-cron, claude-jobs
```

## Documentation

- [How-to guides](docs/how-to/) — setup for each channel: [Slack](docs/how-to/slack.md), [Telegram](docs/how-to/telegram.md), [Cron](docs/how-to/cron.md)
- [Reference](docs/reference/) — [Environment variables](docs/reference/env-vars.md), [Persona (CLAUDE.md)](docs/reference/persona.md), [Response footer](docs/reference/footer.md)
- `CLAUDE.md` — agent-only notes (backend quirks, architecture, subsystem internals)
