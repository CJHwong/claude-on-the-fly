# claude-on-the-fly

> **Experimental.** This is a personal project, not production software. It spawns Claude Code with `--permission-mode bypassPermissions`, meaning Claude has full read/write access to files on the host machine within its workspace. Use at your own risk. Do not run this on a machine with sensitive data you wouldn't want an LLM to access.

Remote access to Claude Code via Telegram and Slack. Send a message, get Claude working on it, see the response with cost/latency/token stats.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude auth login`)

## Quick Start (no clone needed)

```bash
# Telegram
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_ALLOWED_USER_ID=...
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-telegram

# Slack
export SLACK_APP_TOKEN=xapp-...
export SLACK_USER_TOKEN=xoxp-...
export SLACK_USER_ID=UXXXXXXXX
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack
```

## Local Development

```bash
git clone https://github.com/CJHwong/claude-on-the-fly && cd claude-on-the-fly
cp .env.example .env  # fill in your tokens
uv sync
uv run claude-telegram  # or claude-slack
```

## Telegram Setup (2 minutes)

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`, pick a name and username (must end in `bot`)
3. Copy the API token

Get your user ID (to restrict the bot to you only):

4. Search for `@userinfobot` on Telegram, send any message
5. Copy your numeric user ID

Set the env vars and run:

```bash
export TELEGRAM_BOT_TOKEN="your-token-from-step-3"
export TELEGRAM_ALLOWED_USER_ID="your-id-from-step-5"
uv run claude-telegram
```

Send a message to your bot. That's it.

### Telegram Commands

- `/new` - Start a fresh session (clears conversation history)
- `/status` - Check if Claude is working or idle

### Supported Input

- Text messages
- Files (documents, code files) - saved to workspace, Claude can read them
- Photos/images - saved to workspace, Claude can view them
- Multiple photos - batched into a single prompt

## Slack Setup (5 minutes)

### Create the App

1. Go to https://api.slack.com/apps
2. Click "Create New App" -> "From a manifest"
3. Select your workspace
4. Switch to JSON tab, paste the contents of `slack_manifest.json` from this repo
5. Click "Create"

### Get Your Tokens

6. Left sidebar -> "Socket Mode" -> Toggle ON
7. Left sidebar -> "Basic Information" -> scroll to "App-Level Tokens" -> Create a token (name: anything, scope: `connections:write`)
8. Copy the `xapp-...` token
9. Left sidebar -> "Install App" -> "Install to Workspace" -> Allow
10. Copy the "User OAuth Token" (`xoxp-...`)

### Get Your User ID

11. In Slack, click your profile picture -> "Profile"
12. Click the three dots menu -> "Copy member ID"

### Run

```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."
export SLACK_USER_ID="UXXXXXXXX"
# Optional: allow other users (comma-separated). Your own ID is always included.
# export SLACK_ALLOWED_USER_IDS="U111,U222"
uv run claude-slack
```

### How It Works

- Only messages from allowed users are processed (your `SLACK_USER_ID` + anyone in `SLACK_ALLOWED_USER_IDS`)
- Someone DMs you -> Claude responds as you (via user token) in a thread
- Someone @mentions you in a channel -> Claude responds in a thread
- Each thread = one Claude session with memory
- The app must be invited to private channels (`/invite @your-app-name`)

## Running Both

```bash
# Terminal 1
TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USER_ID=... uv run claude-telegram

# Terminal 2
SLACK_APP_TOKEN=... SLACK_USER_TOKEN=... SLACK_USER_ID=... SLACK_ALLOWED_USER_IDS=... uv run claude-slack
```

Or use a `.env` file with all vars and a process manager.

## Environment Variables

| Variable | Required For | Description |
|----------|-------------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Telegram | Your numeric Telegram user ID |
| `SLACK_APP_TOKEN` | Slack | App-level token (`xapp-...`) for Socket Mode |
| `SLACK_USER_TOKEN` | Slack | User OAuth token (`xoxp-...`) to post as you |
| `SLACK_USER_ID` | Slack | Your Slack member ID |
| `SLACK_ALLOWED_USER_IDS` | Slack (optional) | Comma-separated additional allowed user IDs |


## Persona (CLAUDE.md)

Customize Claude's identity and behavior by placing a `CLAUDE.md` at the data root:

```bash
~/.claude-on-the-fly/CLAUDE.md
```

This file is automatically symlinked into every workspace. Claude Code loads it as project instructions. Use it for:

- Bot identity and tone (e.g., "You are Avery, an AI assistant for the EPD team")
- Team directory references
- Custom behavioral rules
- Channel-specific guidelines

The symlink is re-created on every message, so even if removed mid-session it self-heals.

If no `CLAUDE.md` exists, Claude runs with the default system prompt only.

## Architecture

```
src/claude_on_the_fly/
  agent.py        # Claude CLI wrapper + Response dataclass
  orchestrator.py # Session management, queuing, typing indicators
  protocol.py     # Frontend protocol (for adding new interfaces)
  telegram.py     # Telegram frontend
  slack.py        # Slack frontend
```

Each frontend implements the `Frontend` protocol (start, send, send_typing, stop) and plugs into the shared orchestrator. The orchestrator manages sessions, queues messages, and runs Claude Code via subprocess.

## Response Footer

Every response includes a footer with:

```
$0.0471 | 3.6s | ↑1523 ↓42 | claude-sonnet-4-6
  cost    time   tokens in/out   model
```
