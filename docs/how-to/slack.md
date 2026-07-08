# Slack

## Setup (5 minutes)

### Create the app

1. Go to https://api.slack.com/apps
2. Click "Create New App" -> "From a manifest"
3. Select your workspace
4. Switch to JSON tab, paste the contents of [`slack_manifest.json`](slack_manifest.json)
5. Click "Create"

### Get your tokens

6. Left sidebar -> "Socket Mode" -> Toggle ON
7. Left sidebar -> "Basic Information" -> scroll to "App-Level Tokens" -> Create a token (name: anything, scope: `connections:write`)
8. Copy the `xapp-...` token
9. Left sidebar -> "Install App" -> "Install to Workspace" -> Allow
10. Copy one bearer token into `SLACK_TOKEN` — the kind is inferred from the prefix:
    - **User OAuth Token** (`xoxp-...`) — Claude replies as you, sees every channel you can.
    - **Bot User OAuth Token** (`xoxb-...`) — Claude replies as the app. The bot must be invited to each channel, but DMs work out of the box. Pick this for a normal Slack bot.

### Run

```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_TOKEN="xoxb-..."   # xoxp- to reply as you, xoxb- to reply as the app
# Optional: sender IDs allowed to trigger Claude — users (U…/W…) and bots (B…) in
# one list; "*" allows any human. Your own ID is always allowed.
# export SLACK_ALLOWED_SENDER_IDS="U111,U222,B07JPABE2"
# Optional: sender IDs to deny (wins over the allowlist):
# export SLACK_BLOCKED_SENDER_IDS="U999"
# Optional: sender IDs that trigger Claude but get no reply:
# export SLACK_SILENT_SENDER_IDS="B07JPABE2,U111"
uv run claude-slack
```

## How it works

- With a **user token**, anyone who DMs you triggers Claude (if they can DM you, they're trusted), and your own ID is always allowed
- With a **bot token**, the auto-allowed ID is the bot's, not yours — add your own `U…` id to `SLACK_ALLOWED_SENDER_IDS` (or use `*`) or your DMs will be dropped
- In channels, only allowed senders (auto-allowed ID + `SLACK_ALLOWED_SENDER_IDS`) can trigger Claude via @mention
- Bot posts (HubSpot, Jira, etc.) are ignored unless their bot ID (`B…`) is in `SLACK_ALLOWED_SENDER_IDS`; trusted bot posts trigger Claude without an @mention. Don't list this app's own bot ID (it would loop)
- Senders listed in `SLACK_SILENT_SENDER_IDS` still trigger Claude, but their reply is not posted back — useful for alert/automation bots you want handled quietly
- Claude responds in a thread — as you with a user token, or as the app with a bot token
- Each thread = one Claude session with memory
- The app must be invited to private channels (`/invite @your-app-name`)

See [Environment Variables](../reference/env-vars.md#slack) for the full Slack config.
