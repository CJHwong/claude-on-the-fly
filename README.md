# claude-on-the-fly

> **Experimental.** The default configuration is intentionally backward-compatible:
> the agent inherits the daemon environment and tool calls are not gated. Enable the
> sandbox and operator approvals before using it on a machine with sensitive data.

Remote access to Claude Code via Slack and Telegram. Send a message, get Claude working on it, see the response with cost/latency/token stats.

## Important: API Key Required

Using a Claude subscription (Max/Pro) OAuth token with Claude Code in any external tool violates [Anthropic's Consumer Terms of Service](https://docs.anthropic.com/en/docs/claude-code/legal-and-compliance). You must use an [Anthropic API key](https://console.anthropic.com/settings/keys) (`ANTHROPIC_API_KEY`) instead.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated with an API key (`ANTHROPIC_API_KEY`)

## Quick Start (no clone needed)

Tokens live in `~/.claude-on-the-fly/.env`; everything else lives in
`~/.claude-on-the-fly/config.yaml`, seeded with a commented template on first start
and re-read whenever you save it.

```bash
# Telegram
export TELEGRAM_BOT_TOKEN=...
# then set telegram.allowed_user_id in ~/.claude-on-the-fly/config.yaml
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

- [Documentation map](docs/index.md)
- New deployment: [build a safe Slack installation](docs/tutorials/first-safe-deployment.md)
- Common tasks: [how-to guides](docs/how-to/)
- Exact schemas and defaults: [reference](docs/reference/)
- Security and design concepts: [explanation](docs/explanation/)
- `CLAUDE.md` and `docs/agent/` are maintainer notes.
