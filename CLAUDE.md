# CLAUDE.md

Experimental project that spawns Claude Code with `--permission-mode bypassPermissions` to give Telegram/Slack/Gmail/scheduler/symphony frontends a Claude session. See [README.md](README.md) for human-facing overview.

## Stack

- Python 3.12+, [uv](https://docs.astral.sh/uv/) (never pip)
- Three agent backends: `claude` (default), `codex`, `pi` — see `src/claude_on_the_fly/agent.py` for dispatch
- `prek` for hooks, `ruff` + `ruff-format` for lint/format, `ty` for typecheck, `pytest` + `pytest-asyncio` (asyncio_mode=auto)

## Layout

```
src/claude_on_the_fly/
  agent.py             # Backend dispatch + Response + shared helpers
  backends/            # claude.py, codex.py, pi.py
  transcript.py        # Cross-backend conversation handoff
  pricing.py           # OpenRouter-backed price table (codex/pi)
  orchestrator.py      # Shared session/queue layer for chat frontends
  protocol.py          # Frontend protocol (add new interfaces here)
  scheduler.py         # Cron-driven frontend
  telegram.py          # slack.py, gmail.py
  symphony/            # Jira-driven daemon (poll/claim/dispatch)
    tracker/jira.py    # Tracker Protocol implementations live here
    ...
  jobs/                # Background-job daemon (claim/run/notify)
    file_queue.py      # JobQueue Protocol implementations live here
    ...
```

## Verification

Always run before saying a change is done:

```bash
uv run prek run --all-files    # ruff + ruff-format + ty + gitleaks + sanity
uv run pytest                  # tests in tests/
```

`ty` covers the project — don't introduce mypy. `prek run --all-files` is the gate; never `--no-verify` on commit.

## Before working on…

Each subsystem has its own notes file. Read the relevant one before touching the area:

- `docs/agent/backends.md` — when modifying or adding an agent backend
- `docs/agent/symphony.md` — when modifying or adding a tracker, or changing the dispatch loop
- `docs/agent/frontend.md` — when adding a new frontend (Telegram/Slack/Gmail-like)
- `docs/agent/jobs.md` — when touching the background-job worker, a queue adapter, or anything that reads the job queue

When working across multiple areas, read all relevant files first — the subsystems share state through `orchestrator.py` and `agent.py`.
