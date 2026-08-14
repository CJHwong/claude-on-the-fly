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
  interim.py           # Mid-turn progress: coalescing + rate limiting for one turn
  broker.py            # Loopback credential broker (keeps API keys out of the agent)
  egress.py            # CONNECT proxy gating outbound HTTPS by destination host
  commands.py          # Runs credentialed CLIs outside the sandbox via PATH shims
  approvals.py         # Runtime permission grants (ask the operator, then widen)
  permissions.py       # Tool-call approvals: config, subjects, per-backend wiring
  cotf_approve.py      # Shim a sandboxed backend runs to ask (MCP or hook)
  settings.py          # config.yaml (all settings, re-read on save); .env is secrets only
  envfile.py           # Reads DATA_DIR/.env the way a spawned daemon receives it (file wins)
  sandbox.py           # Spawn-time env curation + seatbelt jail wrapping
  turns.py             # Durable record of unanswered chat turns (restart recovery)
  upgrade.py           # Resolves how this install updates, and runs it
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

Both paths survive a stop. Chat turns are journaled to `state/<platform>.turns.json`
before they run (`turns.py`) and resumed or offered back on the next start; jobs
recover through the maildir. See `docs/agent/frontend.md` for the recovery contract.

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

Operator docs follow Diátaxis and start at `docs/index.md`:

- `docs/tutorials/` teaches through one complete first deployment.
- `docs/how-to/` gives task-oriented procedures.
- `docs/reference/` is the exact schema, defaults, and lifecycle contract.
- `docs/explanation/` describes security and design concepts.
- `docs/agent/` remains contributor-only implementation notes.

Keep each page focused on one of those jobs. A setting change must update the packaged
`config.yaml` template and the reference lifecycle table; add how-to or explanation only
when the operator gains a new task or concept.

When working across multiple areas, read all relevant files first — the subsystems share state through `orchestrator.py` and `agent.py`.

## Adding a setting

Settings live in `~/.claude-on-the-fly/config.yaml`; `.env` holds credentials and
`LOG_LEVEL` only. To add one:

1. Add a `FIELDS` entry in `settings.py` mapping the dotted YAML path to the
   environment-variable name readers use. Set `sep` if it is a list.
2. Document it in the bundled `config.yaml`, commented out. Defaults belong in the code
   that reads the setting, not in the template — an absent key means "whatever this
   build does". `permissions:` is the exception and ships real values.
3. Read it with `settings.get(NAME, default)`, which is a drop-in for
   `os.environ.get`. Never bind one to a module constant: that cannot see a value
   `load_dotenv()` sets after import, nor a later edit to the file.
4. If acting on a change means binding a socket, writing a PATH shim, or constructing a
   service, add it to `RESTART_REQUIRED` — otherwise an operator's edit silently does
   nothing.

An environment variable listed in `FIELDS` keeps working and keeps winning, so nothing
breaks for a deployment that never edits the file. `checks.fix_hint` derives "where do I
set this" from `FIELDS`, so a moved setting cannot leave a `.env` hint behind.
