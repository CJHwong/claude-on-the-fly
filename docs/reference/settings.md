# Settings

Two files, split by whether the value is a secret.

| File | Holds | Reloads |
|---|---|---|
| `~/.claude-on-the-fly/.env` | tokens, plus `LOG_LEVEL` | no, restart |
| `~/.claude-on-the-fly/config.yaml` | everything else | yes, on save |

`config.yaml` is seeded from a commented template the first time a daemon starts, and
never overwritten afterwards. It documents every field inline, so it is the better
place to read from; this page is the index.

Every setting below that used to be an environment variable **still works as one, and
the environment still wins over the file.** That is the upgrade path: a deployment
whose `.env` sets `COTF_SANDBOX=jail` does not lose its jail to a `config.yaml` it
never edited. The daemon logs once, per variable, naming the key that replaced it. The
environment forms are undocumented from here on and will not gain new options.

## Saving is enough, except for four things

`config.yaml` is re-parsed when its mtime or size changes, so an edit lands at the next
read: the next CONNECT for a host, the next message for a sender, the next turn for a
model. No reload command, no restart.

Four settings are read once at startup, because acting on a new value means binding a
socket, writing a PATH shim, or deciding whether a service is constructed at all.
Editing one gets you a message on the frontend naming it, on your next turn:

| Setting | Why it cannot reload |
|---|---|
| `commands:` | the shims are written into the running agent's `PATH` |
| `permissions.mode` | decides whether the approval service exists, and writes the shim and MCP config |
| `slack.slash_command` | registered with Slack, not a local decision |
| `jobs.queue_kind` | the worker is a separate process started with the old value |

Reporting rather than applying is deliberate. Tearing down a credential-holding broker
mid-turn trades a config annoyance for a class of mid-turn failure, and all four are
set once per deployment.

## .env

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | for `claude-telegram` | Bot token from BotFather |
| `SLACK_APP_TOKEN` | for `claude-slack` | App-level token (`xapp-...`) for Socket Mode |
| `SLACK_TOKEN` | for `claude-slack` | Bearer token; kind inferred from prefix. `xoxp-...` replies as you (sees everything you can); `xoxb-...` replies as the app (bot must be in each channel — fine for DMs). Approve/deny buttons only reach a bot-token install, so tool approvals need `xoxb-` |
| `JOBS_SLACK_TOKEN` | no | Token the background-job worker posts replies with; falls back to `SLACK_TOKEN`. Set this to a **bot** (`xoxb-`) token if `SLACK_TOKEN` is a user token, or the worker posts as you and the Slack daemon re-ingests its own replies as new input |
| `LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`). Stays here because logging is configured before `config.yaml` can be parsed, and a complaint about that file needs somewhere to go. Message text follows it: redacted to a length at `INFO` and above, present at `DEBUG`, with no separate switch — `DEBUG` already dumps whole Slack events and `slack_bolt` payloads either way. Treat a `DEBUG` log as unshareable |

## config.yaml

### `sandbox:` — whether the agent ever holds a credential

| Key | Was | Description |
|---|---|---|
| `mode` | `COTF_SANDBOX` | `off` (default) inherits the full environment; `env` curates it and routes model calls through the loopback credential broker; `jail` adds a seatbelt profile denying external egress and keychain reads (macOS, falls back to `env` elsewhere). See [the broker notes](../agent/broker.md) |
| `fs` | `COTF_SANDBOX_FS` | `deny-most` makes `$HOME` opaque, re-granting the project dir, `~/.claude`, and `~/.cache/uv`. jail mode only. Watch the agent's own interpreter: mise, nvm, and `~/.local/bin` are under `$HOME`, and a binary it cannot exec means the run never starts |
| `extra_paths` | `COTF_SANDBOX_EXTRA_PATHS` | Extra read grants for `deny-most`, as a YAML list. Capped at 3 — seatbelt has no arrays, so a fourth is dropped with a warning; nest under a shared parent if you need more |
| `broker_only_loopback` | `COTF_SANDBOX_BROKER_ONLY_LOOPBACK` | Narrow egress from all loopback ports to the broker's and the egress proxy's. With no broker base-url published it leaves loopback open, so the agent is never locked out of a broker it needs |

### `agent:` — who answers, and as which model

| Key | Was | Description |
|---|---|---|
| `backend` | `AGENT_BACKEND` | `claude` (default) or `codex` |
| `claude.mode` | `CLAUDE_MODE` | `native` runs `claude` directly; `ollama` wraps it in `ollama launch claude`; `pty` drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p), which is the only mode that surfaces rate-limit and context-window stats and the only one where claude's own permission dialog exists to be forwarded (default: `native`) |
| `codex.mode` | `CODEX_MODE` | `native` runs `codex` directly; `ollama` wraps it in `ollama launch codex` (default: `native`) |
| `claude.model` | `CLAUDE_MODEL` | Passed to `claude --model` in native/pty mode. Unset omits the flag so the CLI picks its own default. Ignored in ollama mode |
| `codex.model` | `CODEX_MODEL` | Passed to `codex exec -m` (e.g. `o3`). Ignored in ollama mode |
| `ollama.model` | `OLLAMA_MODEL` | Required when either mode is `ollama`. A name from `ollama list`, e.g. `deepseek-v4-flash:cloud` |
| `auto_compact_pct` | `COTF_AUTO_COMPACT_PCT` | Compact a chat's history before answering, once the previous turn left the context at or above this share of the model's window (1-100). Unset means compaction only happens on `$compact`. Live under claude `native`/`pty` and under codex; inert under `claude.mode: ollama`, where the claude CLI reports a window for whichever model *it* thinks is answering rather than the one ollama routed to, so the reading is withheld. Preflight warns when it is set somewhere it cannot fire. Keep it well clear of the floor: the system prompt and tool schemas alone are ~7% of a 1M window, and no compaction shrinks them |
| `skills_cache_ttl_seconds` | `SKILLS_CACHE_TTL_SECONDS` | How long a probed skill list is cached (default 3600). Probing spawns the CLI (~0.8s) and the list only changes when plugins or prompts do. `0` or less probes every query |
| `pricing_ttl_seconds` | `COTF_PRICING_TTL_SECONDS` | How long the OpenRouter-backed price table is cached, for the codex cost line. `0` always refreshes; negative never expires |
| `pty.auto_install` | `COTF_AUTO_INSTALL_PTY` | Install claude-pty without prompting when it is missing |
| `pty.auto_refresh` | `COTF_PTY_AUTO_REFRESH` | Let preflight re-splice pty's hooks when they are incomplete (on by default) |

### `egress:` and `commands:` — what the agent may reach and run

No environment equivalent; these were never env vars. `egress.allow` is the hosts
tunnelled without an approval prompt, `egress.never_ask` the ones refused outright, and
`commands.tools` the credentialed CLIs reachable through the command broker. Each entry
grants a capability, so the template explains the trade at each one. Read
[the broker notes](../agent/broker.md) before adding to either.

### `permissions:` — whether tool calls are gated

`mode: off` (default) gates nothing and is byte-identical to a build without the
feature. `ask` routes permission questions to whichever frontend owns the session, with
approve/deny buttons. `claude_mode`, `ttl_seconds` and `timeout_seconds` tune it. What
you get asked is **not the same on every backend** — see
[the broker notes](../agent/broker.md#tool-permissions), which is also where the
measured behaviour of each `claude_mode` is written down.

### `slack:`

Read on every message, so adding a sender takes effect on their next one.

| Key | Was | Description |
|---|---|---|
| `allowed_senders` | `SLACK_ALLOWED_SENDER_IDS` | Senders allowed to trigger the agent, as a YAML list. One list, routed by Slack id prefix: `B…` is a bot and bypasses the @mention gate, anything else (`U…`/`W…`/`*`) is a human. `*` allows any human; bots always need an explicit id, or it loops on this app's own posts. The token's own id is always allowed — with a bot token that is the BOT's, so list your own `U…` id (or `*`) or your DMs are dropped |
| `blocked_senders` | `SLACK_BLOCKED_SENDER_IDS` | Denied senders, users or bots. Wins over the allowlist, so `*` can mean everyone except these |
| `silent_senders` | `SLACK_SILENT_SENDER_IDS` | Senders that trigger the agent but get no reply posted back |
| `slash_command` | `SLACK_SLASH_COMMAND` | Slash command to register, e.g. `/cof-yourname` (bot token only). Must match the app manifest — `claude-slack --manifest` renders one that agrees. Unset registers none, leaving the skill picker on the message shortcut. Slack does not namespace commands, so pick one nobody else in the workspace uses or the newest install wins and yours stops firing. **Restart required** |
| `job_command` | `SLACK_JOB_COMMAND` | Text prefix that queues a background job (default `$job`). Set it to rename the trigger, or set it **empty** to turn background jobs off. A message starting with it is handed to the `claude-jobs` worker, which runs the rest in a fresh session and replies in the same thread when it finishes, so the task outlives the chat turn that asked for it. Sent alone it lists the jobs queued from that channel. Works under either token kind and inside threads. A custom value should be punctuation-led: the trigger is matched against the head of every message, so a plain word swallows every message beginning with it |
| `stats` | `SLACK_STATS_MODE` | Reply footer: `off`, `summary` (default), `detailed` |
| `session_cap` | `SLACK_SESSION_CAP` | Live threads whose per-session state is retained before the least-recently-active is evicted (default 1000). An evicted thread re-hydrates from scratch if it sees another message |
| `reply_soft_limit` | `SLACK_REPLY_SOFT_LIMIT` | Agent replies per thread before inbound messages are gated (default 10). `$continue` resets the counter |

### `telegram:`

| Key | Was | Description |
|---|---|---|
| `allowed_user_id` | `TELEGRAM_ALLOWED_USER_ID` | Your numeric Telegram user ID. One bot, one operator; there is no list |
| `stats` | `TELEGRAM_STATS_MODE` | Reply footer: `off`, `summary` (default), `detailed` |

### `jobs:`

The worker that runs whatever cron and Slack queue. See [Cron](../how-to/cron.md).

| Key | Was | Description |
|---|---|---|
| `queue_kind` | `JOBS_QUEUE_KIND` | Which queue adapter to build (default `file`). An unregistered value fails preflight for both the worker and Slack. **Restart required** |
| `concurrency` | `JOBS_CONCURRENCY` | How many jobs run at once (default 1). A property of the machine, i.e. how many agent CLIs it can host, deliberately separate from a cron entry's own `max_concurrent`. Below 1 or unparseable falls back to 1 with a warning rather than refusing to start |
| `poll_interval_s` | `JOBS_POLL_INTERVAL_S` | Idle wait between drain attempts (default 2.0) |
| `timeout` | `JOBS_TIMEOUT` | Per-job wall clock in seconds (default: the shared agent timeout). `0` or negative means no limit; a cron entry's own `timeout` overrides it per job |

### `logs:`

| Key | Was | Description |
|---|---|---|
| `keep_days` | `COTF_LOG_KEEP_DAYS` | Days of logs to keep, pruned by the date in the filename (default 7). `0` disables pruning |
| `host_tag` | `COTF_HOST_TAG` | Machine name in log filenames (`logs/<role>-<host>-<date>.log`). Defaults to the short hostname; dashes become underscores |

## Cron

No settings here at all. Everything lives in `~/.claude-on-the-fly/cron.yaml` — see
[Cron](../how-to/cron.md). Whatever credentials a producer `command` needs are the
command's own business (`acli auth login`, `gh auth login`, and so on).
