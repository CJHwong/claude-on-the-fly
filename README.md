# claude-on-the-fly

> **Experimental.** This is a personal project, not production software. It spawns Claude Code with `--permission-mode bypassPermissions`, meaning Claude has full read/write access to files on the host machine within its workspace. Use at your own risk. Do not run this on a machine with sensitive data you wouldn't want an LLM to access.

Remote access to Claude Code via Telegram, Slack, and Gmail. Send a message, get Claude working on it, see the response with cost/latency/token stats.

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
export SLACK_APP_TOKEN=xapp-...
export SLACK_USER_TOKEN=xoxp-...
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack

# Gmail
export GMAIL_GCP_PROJECT=your-gcp-project
export GMAIL_ALLOWED_SENDERS=alice@example.com,bob@example.com
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-gmail

# Scheduler
# write ~/.claude-on-the-fly/schedule.yaml (see Scheduler Setup)
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-schedule

# Symphony (Jira-driven daemon)
# write ~/.claude-on-the-fly/symphony.yaml + symphony-prompt-{jira,github}.md (see Symphony Setup)
export JIRA_EMAIL=you@your-org.com
export JIRA_API_TOKEN=...
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-symphony
```

## Local Development

```bash
git clone https://github.com/CJHwong/claude-on-the-fly && cd claude-on-the-fly
cp .env.example .env  # fill in your tokens
uv sync
uv run claude-telegram  # or claude-slack, claude-gmail
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

### Run

```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."
# Optional: allow other users (comma-separated). Your own ID is resolved from the user token.
# export SLACK_ALLOWED_USER_IDS="U111,U222"
# Or set to "*" to allow any sender to @mention the bot in channels:
# export SLACK_ALLOWED_USER_IDS="*"
uv run claude-slack
```

### How It Works

- Anyone who DMs you triggers Claude (if they can DM you, they're trusted)
- In channels, only allowed users (your own ID + `SLACK_ALLOWED_USER_IDS`) can trigger Claude via @mention
- Claude responds as you (via user token) in a thread
- Each thread = one Claude session with memory
- The app must be invited to private channels (`/invite @your-app-name`)

## Gmail Setup (5 minutes)

### Prerequisites

- [gws CLI](https://github.com/googleworkspace/cli) installed and authenticated (`gws auth login`)
- A GCP project with Gmail API and Pub/Sub API enabled
- OAuth scope `gmail.modify`

### Run

```bash
export GMAIL_GCP_PROJECT="your-gcp-project-id"
export GMAIL_ALLOWED_SENDERS="alice@example.com,bob@example.com"
# Optional: polling interval in seconds (default: 5)
# export GMAIL_POLL_INTERVAL=10
uv run claude-gmail
```

### How It Works

- On startup, sweeps existing unread inbox for emails from allowed senders
- Then watches for new emails via Gmail Pub/Sub push notifications (`gws gmail +watch`)
- Only emails from `GMAIL_ALLOWED_SENDERS` trigger Claude sessions
- Auto-generated emails (Jira, GitHub notifications, etc.) are filtered out
- Each email thread = one Claude session with memory
- Claude replies as you via `gws gmail +reply`
- Quoted reply content is stripped (Claude already has session history)
- If the watch process dies, it auto-restarts with exponential backoff

## Scheduler Setup (2 minutes)

Run Claude prompts (and shell scripts) on a cron schedule. Write a YAML config to `~/.claude-on-the-fly/schedule.yaml`:

```yaml
jobs:
  - name: standup-mazu
    cron: "30 6 * * 1-5"          # Mon-Fri 06:30
    prompt: "/gf-ops:daily mazu — post to the team channel. No confirmation."
    timeout: 1800                 # optional, default 1800s

  - name: release-bot
    cron: "0 18 * * 1-5"
    script: ~/scripts/release-bot.sh   # shell escape hatch for multi-step jobs
    args: ["--verbose"]
    timeout: 1800
```

Each job needs `prompt` (goes through Claude with a fresh session every fire) OR `script` (runs as a subprocess). Output goes to `~/.claude-on-the-fly/logs/schedule-<name>.log`. Edits to the config are picked up within a minute, no restart required.

```bash
uv run claude-schedule
# or: uv run claude-schedule --config /path/to/schedule.yaml
```

## Symphony Setup (5 minutes)

Long-running daemon that polls one or more trackers (Jira, GitHub PRs), claims tickets, and runs Claude Code in per-ticket sessions until each ticket leaves an active state. Inspired by [openai/symphony](https://github.com/openai/symphony), extended for multi-source dispatch.

### Two config files

**`~/.claude-on-the-fly/symphony.yaml`** — daemon settings. Multi-tracker shape (each source has its own active/terminal states, gate label, and prompt):

```yaml
trackers:
  jira:
    kind: jira
    base_url: https://your-org.atlassian.net
    email: $JIRA_EMAIL
    api_token: $JIRA_API_TOKEN
    project_key: PROJ
    jql_extra: 'AND assignee = currentUser() AND labels = "stevedore"'
    gate_label: stevedore
    prompt: ./symphony-prompt-jira.md

  github:
    kind: github                  # PRs requesting your review (gh CLI auth)
    prompt: ./symphony-prompt-github.md
    # No gate label: submitting any review removes you from reviewRequests,
    # which is symphony's "done" signal.

# Defaults shown; uncomment to override:
# polling_ms: 30000
# max_concurrent: 1               # global cap across ALL trackers
# max_turns: 20                   # -1 = unlimited (rely on stall_timeout_ms)
# stall_timeout_ms: 1800000
```

The legacy singular `tracker:` form (with top-level `prompt:`, `gate_label:`, `max_concurrent_by_state:`) is still accepted and auto-wrapped into a single-entry trackers map. Existing configs keep working unchanged.

**Adding a new tracker** (Linear, GitHub Issues, etc.): write a class that satisfies `tracker.Tracker` Protocol (`fetch_one`, `fetch_candidates`, `fetch_summaries_by_keys`, `is_terminal`, `is_active`, `issue_to_summary`, `aclose`, `from_config`), register it in `SUPPORTED_TRACKERS` in `src/claude_on_the_fly/symphony/tracker/__init__.py`, then add a stanza under `trackers:` in `symphony.yaml`. No orchestrator changes needed.

**`~/.claude-on-the-fly/symphony-prompt-{source}.md`** — Liquid-templated instructions the agent follows per ticket. Variables available: `issue.identifier`, `issue.title`, `issue.state`, `issue.url`, `issue.labels`, `issue.description_json` (Jira), `issue.body_text` (GitHub), `attempt`, `workspace_path`, `gate_label`. See `symphony-prompt-jira.md.example` and `symphony-prompt-github.md.example` at the repo root.

Edits to either file are picked up on the next tick (mtime hot reload). Restart the daemon to apply schema changes that affect already-claimed tickets.

### Run

```bash
export JIRA_EMAIL="you@your-org.com"
export JIRA_API_TOKEN="..."
uv run claude-symphony
# or: uv run claude-symphony /path/to/symphony.yaml
```

### How It Works

- Polls every configured tracker each `polling_ms`. Jira: JQL `project = PROJ AND status in <active_states> {jql_extra}`. GitHub: `gh search prs --review-requested=@me --state=open --draft=false`.
- Gating: Jira uses a label (default `stevedore`) that the agent removes when done. GitHub uses review assignment — submitting any review removes you from `reviewRequests` automatically.
- Per ticket: creates a per-source scratch dir at `~/.claude-on-the-fly/workspaces/symphony/<source>/<KEY>/` (with the global `CLAUDE.md` persona symlinked in), spawns Claude Code with `--resume <deterministic-uuid>`, loops turns until the ticket reaches a terminal state, leaves active states, or hits `max_turns`.
- The agent works on existing source-repo clones declared in your prompt — it `git worktree add`s alongside those clones, NOT inside the scratch dir. GitHub PR review uses `gh` directly without cloning. Edit the repo list in `symphony-prompt-jira.md` to match your machine.
- Reconciles every tick using each tracker's `is_terminal` / `is_active` predicates. Catches mid-turn state transitions (ticket moved to Done while the agent is working, or `@me` removed from a PR's reviewRequests), cancels the worker, removes the scratch dir.
- Failures (`ClaudeUnavailableError`, exceptions, stalls) go through a retry queue with exponential backoff. `max_turns` exhaustion gets a 1s continuation retry.
- The agent owns git worktrees and branch hygiene (the daemon does NOT run git). It also owns tracker writes: Jira status transitions via `acli`, comments via direct ADF POST to the REST API; GitHub reviews via `gh pr review` and `gh api`.
- Stop signals (escalating force): Jira — remove the gate label, transition to terminal state, SIGINT. GitHub — submit any review, close the PR, SIGINT.

### Symphony Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_EMAIL` | yes | Email associated with your Jira API token |
| `JIRA_API_TOKEN` | yes | Atlassian API token (generate at id.atlassian.com → security → API tokens) |

The `tracker.base_url`, `tracker.project_key`, and `tracker.jql_extra` live in `symphony.yaml`, not env vars.

## Running All

```bash
# Terminal 1
TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USER_ID=... uv run claude-telegram

# Terminal 2
SLACK_APP_TOKEN=... SLACK_USER_TOKEN=... SLACK_ALLOWED_USER_IDS=... uv run claude-slack

# Terminal 3
GMAIL_GCP_PROJECT=... GMAIL_ALLOWED_SENDERS=... uv run claude-gmail

# Terminal 4
uv run claude-schedule

# Terminal 5
JIRA_EMAIL=... JIRA_API_TOKEN=... uv run claude-symphony
```

Or use a `.env` file with all vars and a process manager.

## Environment Variables

### Telegram

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | yes | Your numeric Telegram user ID |
| `TELEGRAM_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

### Slack

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_APP_TOKEN` | yes | App-level token (`xapp-...`) for Socket Mode |
| `SLACK_USER_TOKEN` | yes | User OAuth token (`xoxp-...`) to post as you |
| `SLACK_ALLOWED_USER_IDS` | no | Extra allowed user IDs (comma-separated). Your own ID is resolved from the user token. Use `*` to allow any sender (applies to channels and DMs) |
| `SLACK_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

### Gmail

| Variable | Required | Description |
|----------|----------|-------------|
| `GMAIL_GCP_PROJECT` | yes | GCP project ID (for Pub/Sub) |
| `GMAIL_ALLOWED_SENDERS` | yes | Allowed senders (comma-separated). Each entry is an exact address (`alice@example.com`), a domain wildcard (`*@gofreight.com`), or `*` for any sender |
| `GMAIL_POLL_INTERVAL` | no | Seconds between Pub/Sub pulls (default: 5) |
| `GMAIL_STATS_MODE` | no | Footer mode: `off`, `summary`, `detailed` (default: `summary`) |

### Shared

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENT_BACKEND` | no | Agent CLI to drive: `claude` (default) or `codex` |
| `CLAUDE_MODE` | no | `native` runs `claude` directly; `ollama` wraps it in `ollama launch claude` (default: `native`) |
| `CODEX_MODE` | no | `native` runs `codex` directly; `ollama` wraps it in `ollama launch codex` (default: `native`) |
| `OLLAMA_MODEL` | conditional | Required when `CLAUDE_MODE=ollama` or `CODEX_MODE=ollama`. Name from `ollama list` (e.g. `deepseek-v4-flash:cloud`) |
| `CLAUDE_MODEL` | no | Model passed to `claude --model` in native mode (default: `sonnet`). Ignored in ollama mode |
| `CODEX_MODEL` | no | Model passed to `codex exec -m` in native mode (e.g. `o3`, `gpt-4.1`). Ignored in ollama mode |
| `LOG_LEVEL` | no | Console log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

#### ollama launch mode

Routes the agent's model calls through a local or cloud Ollama model instead of the agent's native API. Requires the agent CLI (`claude` or `codex`) and `ollama` installed, and the target model already pulled (`ollama pull <name>`):

```bash
# Claude via ollama
export CLAUDE_MODE=ollama
export OLLAMA_MODEL=deepseek-v4-flash:cloud
uv run claude-telegram

# Codex via ollama
export AGENT_BACKEND=codex
export CODEX_MODE=ollama
export OLLAMA_MODEL=deepseek-v4-flash:cloud
uv run claude-telegram
```

For claude, cost in the stats footer reflects Ollama's billing for `:cloud` models, or `$0` for local ones. Session resume, tool use, and Skills behave the same as native mode — only the model provider changes.

#### codex backend notes

Codex differs from claude in a few ways:

- **Cost is computed locally.** Codex's CLI doesn't emit a cost field, so we look the model up in [OpenRouter's public model registry](https://openrouter.ai/api/v1/models) and multiply by token counts. The table is fetched on demand and cached at `~/.claude-on-the-fly/pricing/openrouter.json` (TTL 7 days, configurable via `COTF_PRICING_TTL_SECONDS`). OpenRouter covers native API models (`gpt-5.4`, `gpt-4.1`, …) and ollama-cloud variants (`deepseek-v4-flash:cloud`, `deepseek-v4-pro:cloud`). Lookup strips the vendor prefix from registry keys (`deepseek/deepseek-v4-flash` → `deepseek-v4-flash`) and tries `:cloud`-stripped and date-stripped variants for snapshot drift (`gpt-4.1-2025-04-14` → `gpt-4.1`). Misses return `$0` rather than guessing — purely local ollama models (qwen, gemma) aren't in the registry and stay $0 (which is correct, they don't bill). Prices may differ slightly from your provider's billing (OpenRouter charges a routing markup) — within 10-20% is the expected accuracy.
- **Session model.** Codex assigns its own `thread_id` per session, so we persist a `<workspace>/.codex_sessions/<our-session-uuid>` mapping file after the first turn and pass `resume <thread_id>` on follow-ups.
- **No system-prompt flag.** Codex has no `--system-prompt`, so the format hint is prepended to each user message. Your persona `~/.claude-on-the-fly/CLAUDE.md` is symlinked into the workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex), so both backends see it.
- **No skill tracking.** Codex has no skill concept; `skill_counts` is always empty. Tool counts come from `item.completed` event types (e.g. `command_execution`, `file_change`).
- **Image input.** Works the same as claude. Frontends save uploaded images to the workspace and codex reads them through its own file-read tooling — the underlying model is multimodal, so it sees image content directly, no `-i` flag needed.


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
  agent.py        # Backend dispatch (AgentBackend protocol, OllamaLauncher, get_backend) + Response + shared helpers
  backends/
    claude.py     # Drives `claude -p` directly; optional ollama-launch prefix
    codex.py      # Drives `codex exec --json`; tracks codex thread_ids per session
  transcript.py   # Cross-backend conversation handoff: parses prior backend's session JSONL when daemon switches
  pricing.py      # OpenRouter-backed price-table lookup for codex (claude reports its own cost)
  orchestrator.py # Session management, queuing, typing indicators (chat frontends)
  protocol.py     # Frontend protocol (for adding new interfaces)
  scheduler.py    # Cron-driven frontend, YAML config, auto-reload
  telegram.py     # Telegram frontend
  slack.py        # Slack frontend
  gmail.py        # Gmail frontend
  symphony/       # Jira-driven daemon: poll, dispatch, reconcile, retry queue
    cli.py            # entrypoint
    config.py         # YAML schema + $VAR resolution
    prompt.py         # markdown loader + Liquid renderer + mtime hot reload
    state.py          # in-memory orchestrator state
    retry.py          # exponential-backoff retry queue
    workspace.py      # per-ticket scratch dir lifecycle (no git)
    agent_runner.py   # bridges TicketRunner ↔ ClaudeAgent
    orchestrator.py   # poll → reconcile → dispatch loop
    tracker/jira.py   # Jira REST adapter (httpx, basic auth)
```

Each chat frontend (Telegram/Slack/Gmail) implements the `Frontend` protocol and plugs into the shared `orchestrator.py`. The scheduler implements `Frontend` to fire cron jobs through the same path. Symphony is daemon-shaped (poll/claim/dispatch instead of request/response), so it bypasses `Frontend` and runs directly on `agent.run()`. All entrypoints share `agent.py`'s subprocess driver and emit a `session: id=...` log line per Claude run for cross-integration tracing.

## Response Footer

Controlled per channel via `{TELEGRAM,SLACK,GMAIL}_STATS_MODE`:

- `off` — no footer
- `summary` (default) — single stats line
- `detailed` — stats line + tool-use breakdown

```
$0.0471 | 3.6s | ↑1523 ↓42 | claude-sonnet-4-6
🔧 8 (Read×4 Bash×3 Grep×1)
```
