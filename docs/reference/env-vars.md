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
| `SLACK_JOB_COMMAND` | no | Text prefix that queues a background job. Defaults to `$job`; set it to rename the trigger, or set it **empty** to turn background jobs off. A message starting with it is handed to the `claude-jobs` worker, which runs the rest in a fresh session and replies in the same thread when it finishes — so the task outlives the chat turn that asked for it. Sent on its own, with no task, it lists the jobs queued from that channel. Works under either token kind, and inside threads. A custom value should be punctuation-led: the trigger is matched against the head of every message, so a plain word swallows every message beginning with it. If `SLACK_TOKEN` is a user token, also set `JOBS_SLACK_TOKEN` to a bot token — otherwise the worker's replies post as you and this daemon re-ingests them as new input |
| `SLACK_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

## Cron

No env vars. Everything lives in `~/.claude-on-the-fly/cron.yaml` — see
[Cron](../how-to/cron.md). Whatever credentials a producer `command` needs are the
command's own business (`acli auth login`, `gh auth login`, and so on).

## Shared

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENT_BACKEND` | no | Agent CLI to drive: `claude` (default) or `codex` |
| `CLAUDE_MODE` | no | `native` runs `claude` directly; `ollama` wraps it in `ollama launch claude`; `pty` drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) to surface rate-limit and context-window stats (default: `native`) |
| `CODEX_MODE` | no | `native` runs `codex` directly; `ollama` wraps it in `ollama launch codex` (default: `native`) |
| `OLLAMA_MODEL` | conditional | Required when `CLAUDE_MODE=ollama` or `CODEX_MODE=ollama`. Name from `ollama list` (e.g. `deepseek-v4-flash:cloud`) |
| `CLAUDE_MODEL` | no | Model passed to `claude --model` in native/pty mode. Unset (default) omits `--model` so the claude CLI uses its own default. Ignored in ollama mode |
| `CODEX_MODEL` | no | Model passed to `codex exec -m` in native mode (e.g. `o3`, `gpt-4.1`). Ignored in ollama mode |
| `LOG_LEVEL` | no | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `COTF_HOST_TAG` | no | Machine name in log filenames (`logs/<role>-<host>-<date>.log`). Defaults to the short hostname; dashes become underscores |
| `COTF_LOG_KEEP_DAYS` | no | Days of logs to keep, pruned by the date in the filename (default: 7). `0` disables pruning |
| `COTF_AUTO_COMPACT_PCT` | no | Compact a chat's history before answering, once the previous turn left the context at or above this share of the model's window (1-100). Unset (default) means compaction only happens when asked for with `$compact`. Live under claude `native`/`pty` and under codex; inert only under `CLAUDE_MODE=ollama`, where the claude CLI reports a context window for whichever model *it* thinks is answering rather than the one ollama routed to, so the reading is withheld. Preflight warns when it is set somewhere it cannot fire. Keep it well clear of the floor: the system prompt and tool schemas alone are ~7% of a 1M window and no compaction shrinks them |

## Background jobs

The worker that runs whatever cron and Slack queue. See [Cron](../how-to/cron.md).

| Variable | Required | Description |
|----------|----------|-------------|
| `JOBS_CONCURRENCY` | no | How many jobs run at once (default `1`). A property of the machine, i.e. how many agent CLIs it can host, deliberately separate from a cron entry's own `max_concurrent`. Below 1 or unparseable falls back to 1 with a warning rather than refusing to start |
| `JOBS_POLL_INTERVAL_S` | no | Idle wait between drain attempts (default `2.0`) |
| `JOBS_TIMEOUT` | no | Per-job wall clock in seconds (default: the shared agent timeout). `0` or negative means no limit. A cron entry's `timeout` overrides it per job |
| `JOBS_QUEUE_KIND` | no | Which queue adapter to build (default `file`). An unregistered value fails preflight for both the worker and Slack |
| `JOBS_SLACK_TOKEN` | no | Token the worker posts replies with; falls back to `SLACK_TOKEN`. Set this to a **bot** (`xoxb-`) token if `SLACK_TOKEN` is a user token, or the worker posts as you and the Slack daemon re-ingests its own replies as new input |
