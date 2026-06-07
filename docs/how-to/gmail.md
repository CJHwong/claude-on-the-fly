# Gmail

## Prerequisites

- [gws CLI](https://github.com/googleworkspace/cli) installed and authenticated (`gws auth login`)
- A GCP project with Gmail API and Pub/Sub API enabled
- OAuth scope `gmail.modify`

## Run

```bash
export GMAIL_GCP_PROJECT="your-gcp-project-id"
export GMAIL_ALLOWED_SENDERS="alice@example.com,bob@example.com"
# Optional: polling interval in seconds (default: 5)
# export GMAIL_POLL_INTERVAL=10
uv run claude-gmail
```

## How it works

- On startup, sweeps existing unread inbox for emails from allowed senders
- Then watches for new emails via Gmail Pub/Sub push notifications (`gws gmail +watch`)
- Only emails from `GMAIL_ALLOWED_SENDERS` trigger Claude sessions
- Auto-generated emails (Jira, GitHub notifications, etc.) are filtered out
- Each email thread = one Claude session with memory
- Claude replies as you via `gws gmail +reply`
- Quoted reply content is stripped (Claude already has session history)
- If the watch process dies, it auto-restarts with exponential backoff

See [Environment Variables](../reference/env-vars.md#gmail) for the full Gmail config.
