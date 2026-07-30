# CLAUDE.md

Experimental project that spawns Claude Code with `--permission-mode bypassPermissions` to give Slack/Telegram/cron frontends a Claude session. See [README.md](README.md) for human-facing overview.

## Stack

- Python 3.12+, [uv](https://docs.astral.sh/uv/) (never pip)
- Two agent backends: `claude` (default), `codex` — see `src/claude_on_the_fly/agent.py` for dispatch
- `prek` for hooks, `ruff` + `ruff-format` for lint/format, `ty` for typecheck, `pytest` + `pytest-asyncio` (asyncio_mode=auto)

## Layout

```
src/claude_on_the_fly/
  agent.py             # Backend dispatch + Response + shared helpers
  backends/            # claude.py, codex.py
  transcript.py        # Cross-backend conversation handoff
  pricing.py           # OpenRouter-backed price table (codex)
  logs.py              # Log naming (<role>-<host>-<date>), rollover, retention
  orchestrator.py      # Shared session/queue layer for chat frontends
  broker.py            # Loopback credential broker (keeps API keys out of the agent)
  egress.py            # CONNECT proxy gating outbound HTTPS by destination host
  commands.py          # Runs credentialed CLIs outside the sandbox via PATH shims
  approvals.py         # Runtime permission grants (ask the operator, then widen)
  settings.py          # ~/.claude-on-the-fly/sandbox.yaml (hosts + brokered CLIs)
  sandbox.py           # Spawn-time env curation + seatbelt jail wrapping
  protocol.py          # Frontend protocol (add new interfaces here)
  cron.py              # Cron producer daemon — runs shell, enqueues Jobs
  slack.py             # telegram.py
  jobs/                # Background-job daemon (claim/run/notify)
    core.py            # Job + the four ports the worker depends on
    file_queue.py      # JobQueue Protocol implementations live here
    keys.py            # job key -> filename / workspace segment
    key_state.py       # per-key backoff + no-progress memory
    ...
```

Work reaches an agent one of two ways: a chat frontend through `orchestrator.py`,
or `cron.py` -> the job queue -> `jobs/`. Nothing else calls `agent.run`.

## Verification

Always run before saying a change is done:

```bash
uv run prek run --all-files    # ruff + ruff-format + ty + gitleaks + sanity
uv run pytest                  # tests in tests/
```

Coverage is 100% of statements and is enforced (`fail_under = 100`). It is not in
pytest's `addopts` because it roughly triples the suite's wall clock, so run it
explicitly when you add or change code:

```bash
uv run pytest --cov=claude_on_the_fly --cov-report=term-missing
```

New code needs a test that fails without it. If a line genuinely cannot run under
test, `# pragma: no cover` with a comment saying why is the escape hatch. There
are two of those outside the `if __name__ == "__main__"` guards, both on states a
correctly-behaving OS will not produce: a child the kernel has not reaped after
SIGKILL, and a binary that vanished between the construction filter and the run.

`ty` covers the project — don't introduce mypy. `prek run --all-files` is the gate; never `--no-verify` on commit.

## Before working on…

Each subsystem has its own notes file. Read the relevant one before touching the area:

- `docs/agent/backends.md` — when modifying or adding an agent backend
- `docs/agent/cron.md` — when touching the cron producer, its config schema, or key state
- `docs/agent/frontend.md` — when adding a new frontend (Slack/Telegram-like)
- `docs/agent/jobs.md` — when touching the background-job worker, a queue adapter, or anything that reads the job queue
- `docs/agent/broker.md` — when changing the credential broker or sandbox/jail wiring

When working across multiple areas, read all relevant files first — the subsystems share state through `orchestrator.py` and `agent.py`.
