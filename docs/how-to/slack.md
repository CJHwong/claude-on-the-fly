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
10. Copy the "User OAuth Token" (`xoxp-...`)

### Run

```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."
# Optional: allow other users (comma-separated). Your own ID is resolved from the user token.
# export SLACK_ALLOWED_USER_IDS="U111,U222"
# Or set to "*" to allow any sender to @mention the bot in channels:
# export SLACK_ALLOWED_USER_IDS="*"
# Optional: let trusted app/bot posts (HubSpot, Jira) trigger Claude by bot ID:
# export SLACK_ALLOWED_BOT_IDS="B07JPABE2"
# Optional: senders (bot B… or user U… IDs) that trigger Claude but get no reply:
# export SLACK_SILENT_SENDER_IDS="B07JPABE2,U111"
uv run claude-slack
```

## How it works

- Anyone who DMs you triggers Claude (if they can DM you, they're trusted)
- In channels, only allowed users (your own ID + `SLACK_ALLOWED_USER_IDS`) can trigger Claude via @mention
- App/bot posts (HubSpot, Jira, etc.) are ignored unless their bot ID is in `SLACK_ALLOWED_BOT_IDS`; trusted bot posts trigger Claude without an @mention
- Senders listed in `SLACK_SILENT_SENDER_IDS` (bot `B…` or user `U…` IDs) still trigger Claude, but their reply is not posted back into Slack — useful for alert/automation bots you want handled quietly
- Claude responds as you (via user token) in a thread
- Each thread = one Claude session with memory
- The app must be invited to private channels (`/invite @your-app-name`)

See [Environment Variables](../reference/env-vars.md#slack) for the full Slack config.
