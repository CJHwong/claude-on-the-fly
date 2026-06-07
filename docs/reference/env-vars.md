# Environment Variables

## Telegram

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | yes | Your numeric Telegram user ID |
| `TELEGRAM_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

## Slack

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_APP_TOKEN` | yes | App-level token (`xapp-...`) for Socket Mode |
| `SLACK_USER_TOKEN` | yes | User OAuth token (`xoxp-...`) to post as you |
| `SLACK_ALLOWED_USER_IDS` | no | Extra allowed user IDs (comma-separated). Your own ID is resolved from the user token. Use `*` to allow any sender (applies to channels and DMs) |
| `SLACK_BLOCKED_USER_IDS` | no | User IDs to deny (comma-separated). Takes priority over the allowlist, so `*` can allow everyone except these |
| `SLACK_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

## Gmail

| Variable | Required | Description |
|----------|----------|-------------|
| `GMAIL_GCP_PROJECT` | yes | GCP project ID (for Pub/Sub) |
| `GMAIL_ALLOWED_SENDERS` | yes | Allowed senders (comma-separated). Each entry is an exact address (`alice@example.com`), a domain wildcard (`*@gofreight.com`), or `*` for any sender |
| `GMAIL_POLL_INTERVAL` | no | Seconds between Pub/Sub pulls (default: 5) |
| `GMAIL_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

## Symphony

| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_EMAIL` | yes | Email associated with your Jira API token |
| `JIRA_API_TOKEN` | yes | Atlassian API token (generate at id.atlassian.com → security → API tokens) |

The `tracker.base_url`, `tracker.project_key`, and `tracker.jql_extra` live in `symphony.yaml`, not env vars.

## Shared

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENT_BACKEND` | no | Agent CLI to drive: `claude` (default), `codex`, or `pi` |
| `CLAUDE_MODE` | no | `native` runs `claude` directly; `ollama` wraps it in `ollama launch claude`; `pty` drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) to surface rate-limit and context-window stats (default: `native`) |
| `CODEX_MODE` | no | `native` runs `codex` directly; `ollama` wraps it in `ollama launch codex` (default: `native`) |
| `PI_MODE` | no | `native` runs `pi` directly; `ollama` wraps it in `ollama launch pi` (default: `native`) |
| `OLLAMA_MODEL` | conditional | Required when `CLAUDE_MODE=ollama`, `CODEX_MODE=ollama`, or `PI_MODE=ollama`. Name from `ollama list` (e.g. `deepseek-v4-flash:cloud`) |
| `CLAUDE_MODEL` | no | Model passed to `claude --model` in native/pty mode. Unset (default) omits `--model` so the claude CLI uses its own default. Ignored in ollama mode |
| `CODEX_MODEL` | no | Model passed to `codex exec -m` in native mode (e.g. `o3`, `gpt-4.1`). Ignored in ollama mode |
| `PI_MODEL` | no | Model passed to `pi --model` in native mode (e.g. `deepseek-v4-flash:cloud`). Ignored in ollama mode |
| `PI_PROVIDER` | no | Provider passed to `pi --provider` (default: `google`). Set to `ollama` for local/cloud ollama models, or use `PI_MODE=ollama` |
| `LOG_LEVEL` | no | Console log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
