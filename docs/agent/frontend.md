# Frontends

Messaging-platform adapters (Telegram, Slack, Gmail, scheduler) implement the `Frontend` ABC at `src/claude_on_the_fly/protocol.py:11`. The shared session/queue/typing-indicator layer is `src/claude_on_the_fly/orchestrator.py`.

## Adding a new frontend

1. Implement the `Frontend` ABC in a new file (e.g. `src/claude_on_the_fly/discord.py`).
2. Register the entrypoint in `pyproject.toml` under `[project.scripts]` (see existing `claude-telegram`, `claude-slack`, `claude-gmail`).
3. The `start(on_message)` hook is the only required one to wire up. `set_orchestrator()` is optional and only needed if the frontend needs to talk back to the orchestrator.

### Required hooks

- `start(on_message)` — begin listening, call `on_message(chat_id, text)` per incoming message.
- `send(chat_id, response)` — deliver a `Response` to the user.
- `send_typing(chat_id)` — typing indicator. No-op if the platform doesn't support it.
- `stop()` — graceful shutdown.
- `workspace_name(chat_id)`, `sender_name(chat_id)`, `channel_context(chat_id)` — human-readable labels used in logs and footer.

### Optional overrides worth knowing

- `notify_queued` / `notify_start` / `notify_complete` (default no-op) — for frontends that want cheaper signals than text replies (e.g. emoji reactions).
- `timeout_for(chat_id)` — per-message subprocess timeout override; `None` uses the agent default.
- `describe()` — frontend-specific settings to print in the startup preview. Redact secrets before returning.

## Cross-cutting concerns

- **Persona**: `ensure_persona()` at `src/claude_on_the_fly/agent.py:40` is called per workspace; the new frontend just needs to construct a `Path` workspace and pass it through.
- **Stats footer**: `stats_mode(platform)` and `footer_parts(response, platform)` in `agent.py` (`agent.py:57`, `agent.py:68`) generate the per-channel footer based on `{TELEGRAM,SLACK,GMAIL}_STATS_MODE` env vars. Add a new var for the new frontend if you want the same control.
- **Image / file input**: frontends save uploads into the workspace path; the agent reads them through its own file tooling. Don't try to pipe image bytes through the agent CLI.

## Scheduler is a frontend too

`src/claude_on_the_fly/scheduler.py` implements `Frontend` to fire cron jobs through the same path as chat frontends. Look here if you're adding anything cron-shaped rather than chat-shaped — the existing pattern (jobs are prompts OR shell scripts, YAML config, mtime hot reload) is a useful template.

## Symphony is NOT a frontend

Symphony is daemon-shaped (poll/claim/dispatch), not request/response. It bypasses `Frontend` and runs directly on `agent.run()`. If you're tempted to make your new thing a `Frontend`, ask first whether it's really request/response — if not, it's a new orchestrator kind and warrants its own pattern.
