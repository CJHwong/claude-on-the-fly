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
- **File output (outbox)**: the agent attaches files by dropping them in `workspace/outbox/`. The orchestrator scans that folder after the run (`agent.collect_outbox`), sets `response.attachments`, lets `send()` upload them, then archives to `outbox/.sent/<ts>/` (`agent.archive_outbox`). This is gated on `agent.ATTACHMENT_PLATFORMS` — the single source of truth that also injects the outbox instruction into the system prompt. To support outbox on a new frontend: add its platform to `ATTACHMENT_PLATFORMS` and have `send()` upload `response.attachments`. Slack uploads via `files_upload_v2` and must record the share `ts` (echo guard, since it posts as the user); Telegram routes images to `send_photo`, else `send_document`.

## Slack control surface (token-kind split)

Slack exposes two control surfaces, chosen by the token kind (`_is_bot_token`, from the `xoxb`/`xoxp` prefix):

- **Text prefixes (`$continue`, `$stop`)**: plain messages intercepted in `_ingest_event`, so they work under either token kind **and inside threads** — the only control surface Slack allows in threads (custom slash commands are hard-blocked there). `$stop` aborts the in-flight turn for that thread's session via `Orchestrator.abort`.
- **Bot token (`xoxb`) only**: the `SLACK_SLASH_COMMAND` (default `/cof`) + skill picker + a **message shortcut** ("Run a skill"), registered in `start()`. These are app interactions a user token never receives, and a slash command can't run in a thread — so the shortcut (from a message's `...` menu) is the thread-capable way to open the picker. Requires the manifest to declare the command + shortcut + `commands` scope + interactivity; all rides the existing Socket Mode connection (no public URL).

A message is acted on only when it's addressed to the bot — a DM the bot is in, or an @mention in a channel — and from an allowed sender. Under a bot token the app also holds the authorizing user's grant, so Slack delivers that user's *other* DMs (with third parties) too; `_is_bot_conversation` (a cached, fail-open `conversations.info` check) filters `im`/`mpim` to the ones the bot is actually in, skipping third-party DMs.

Routing: `/cof` (`_handle_slash_command`) — bare opens the picker; `continue [text]` resets the soft-limit counter; `stop` aborts; else forwards `/<skill> args`. A slash command has no `thread_ts`, so it targets the channel/DM root. The **message shortcut** (`_handle_run_skill_shortcut`) carries the clicked message's thread, so its picker forward is thread-scoped. Both go through `_enter_command_session(channel, user_id, thread_ts)`.

The picker is a **`static_select`** built at modal-open time (`_open_skill_picker` → `_skill_option_groups`): skills are grouped into Block Kit `option_groups` by plugin namespace (`plugin:skill` → group "plugin"; plain names → "user"), so the whole list is browsable on open. `option_groups` lift the flat 100-option cap (up to 100 groups × 100), which matters since there are >100 skills; `external_select` was rejected because it's search-first (shows nothing until you type) and caps a single response at 100. The option value stays the full `plugin:skill` name so the forward matches. Skills come from `AgentBackend.list_skills()`, which returns `(name, description)` pairs (the description renders as a second line per option). claude reads skill names from the `system/init` stream-json probe and pulls descriptions from each `SKILL.md` front-matter (scanned across the init event's active plugin paths + `$CLAUDE_CONFIG_DIR/skills`, since the init event carries names only); codex scans `$CODEX_HOME/prompts/*.md` (each a `/<name>`, description from the file's front-matter); pi/opencode return `[]`. `agent.cached_skills()` wraps this in a TTL cache (`SKILLS_CACHE_TTL_SECONDS`, default 1h; `<= 0` disables it — probe every query) with an in-memory layer plus a JSON file under `DATA_DIR/cache`, so a picker opened before startup warm finishes is still instant instead of paying the ~0.8s cold CLI probe. Startup warm re-probes with `force=True` and overwrites the cache, so **restarting the daemon picks up newly installed/updated skills** (within a run the TTL governs; the cache alone would otherwise mask changes for up to the TTL, even across restarts).

`/cof stop` calls `Orchestrator.abort(chat_id)`, which cancels the drain task and clears the queue. Because every backend now spawns with `start_new_session=True` and reaps via `agent._kill_process_tree` (SIGKILL to the process group), cancelling a turn kills the agent CLI *and* its tool subprocesses instead of orphaning them.

## Scheduler is a frontend too

`src/claude_on_the_fly/scheduler.py` implements `Frontend` to fire cron jobs through the same path as chat frontends. Look here if you're adding anything cron-shaped rather than chat-shaped — the existing pattern (jobs are prompts OR shell scripts, YAML config, mtime hot reload) is a useful template.

## Symphony is NOT a frontend

Symphony is daemon-shaped (poll/claim/dispatch), not request/response. It bypasses `Frontend` and runs directly on `agent.run()`. If you're tempted to make your new thing a `Frontend`, ask first whether it's really request/response — if not, it's a new orchestrator kind and warrants its own pattern.
