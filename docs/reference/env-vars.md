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
| `SLACK_TOKEN` | yes | Bearer token; kind inferred from prefix. `xoxp-...` replies as you (sees everything you can); `xoxb-...` replies as the app (bot must be in each channel — fine for DMs) |
| `SLACK_ALLOWED_SENDER_IDS` | no | Sender IDs allowed to trigger Claude (comma-separated). Users (`U…`/`W…`) and bots (`B…`, e.g. `B07JPABE2` for HubSpot/Jira) share one list; bots bypass the @mention gate. Your own ID is always allowed. `*` allows any human; bots always need an explicit id (no wildcard, or it loops on this app's own posts) |
| `SLACK_BLOCKED_SENDER_IDS` | no | Sender IDs (users or bots) to deny (comma-separated). Takes priority over the allowlist, so `*` can allow everyone except these |
| `SLACK_SILENT_SENDER_IDS` | no | Sender IDs (users or bots) that trigger Claude but get no reply posted back. Empty by default, so every triggered run replies |
| `SLACK_SLASH_COMMAND` | no | Slash command to register, e.g. `/cof-yourname` (bot token only). Must match the command in the app manifest. Unset registers none, leaving the skill picker on the message shortcut. Slack doesn't namespace commands, so pick one nobody else in the workspace uses or the newest install wins and yours stops firing. `claude-slack --manifest` renders a matching manifest |
| `SLACK_JOB_COMMAND` | no | Text prefix that queues a background job, e.g. `$job`. A message starting with it is handed to the `claude-jobs` worker, which runs the rest of the message in a fresh session and replies in the same thread when it finishes — so the task outlives the chat turn that asked for it. Sent on its own, with no task, it lists the jobs queued from that channel instead. Unset (the default) registers no trigger at all and such a message is answered as ordinary text. Works under either token kind, and inside threads. Pick a punctuation-led value: the trigger is matched against the head of every message, so a plain word swallows every message beginning with it. If `SLACK_TOKEN` is a user token, also set `JOBS_SLACK_TOKEN` to a bot token — otherwise the worker's replies post as you and this daemon re-ingests them as new input |
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
| `AGENT_BACKEND` | no | Agent CLI to drive: `claude` (default), `codex`, `pi`, or `opencode` |
| `CLAUDE_MODE` | no | `native` runs `claude` directly; `ollama` wraps it in `ollama launch claude`; `pty` drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) to surface rate-limit and context-window stats (default: `native`) |
| `CODEX_MODE` | no | `native` runs `codex` directly; `ollama` wraps it in `ollama launch codex` (default: `native`) |
| `PI_MODE` | no | `native` runs `pi` directly; `ollama` wraps it in `ollama launch pi` (default: `native`) |
| `OPENCODE_MODE` | no | `native` runs `opencode` directly; `ollama` wraps it in `ollama launch opencode` (default: `native`) |
| `OLLAMA_MODEL` | conditional | Required when `CLAUDE_MODE=ollama`, `CODEX_MODE=ollama`, `PI_MODE=ollama`, or `OPENCODE_MODE=ollama`. Name from `ollama list` (e.g. `deepseek-v4-flash:cloud`) |
| `CLAUDE_MODEL` | no | Model passed to `claude --model` in native/pty mode. Unset (default) omits `--model` so the claude CLI uses its own default. Ignored in ollama mode |
| `CODEX_MODEL` | no | Model passed to `codex exec -m` in native mode (e.g. `o3`, `gpt-4.1`). Ignored in ollama mode |
| `PI_MODEL` | no | Model passed to `pi --model` in native mode (e.g. `deepseek-v4-flash:cloud`). Ignored in ollama mode |
| `OPENCODE_MODEL` | no | Model passed to `opencode run -m` in native mode, in `provider/model` form (e.g. `github-copilot/claude-haiku-4.5`). Unset omits `-m` so opencode uses its own default. Ignored in ollama mode |
| `PI_PROVIDER` | no | Provider passed to `pi --provider` (default: `google`). Set to `ollama` for local/cloud ollama models, or use `PI_MODE=ollama` |
| `LOG_LEVEL` | no | Console log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
