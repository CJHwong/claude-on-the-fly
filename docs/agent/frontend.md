# Frontends

Messaging-platform adapters (Slack, Telegram) implement the `Frontend` ABC at `src/claude_on_the_fly/protocol.py:11`. The shared session/queue/typing-indicator layer is `src/claude_on_the_fly/orchestrator.py`.

## Adding a new frontend

1. Implement the `Frontend` ABC in a new file (e.g. `src/claude_on_the_fly/discord.py`).
2. Register the entrypoint in `pyproject.toml` under `[project.scripts]` (see existing `claude-slack`, `claude-telegram`).
3. The `start(on_message)` hook is the only required one to wire up. `set_orchestrator()` is optional and only needed if the frontend needs to talk back to the orchestrator.

### Required hooks

- `start(on_message)` — begin listening, call `on_message(chat_id, text)` per incoming message.
- `send(chat_id, response)` — deliver a `Response` to the user.
- `send_typing(chat_id)` — typing indicator. No-op if the platform doesn't support it.
- `stop()` — graceful shutdown.
- `workspace_name(chat_id)`, `sender_name(chat_id)`, `channel_context(chat_id)` — human-readable labels used in logs and footer.

### Optional overrides worth knowing

- `notify_queued` / `notify_start` / `notify_complete` (default no-op) — for frontends that want cheaper signals than text replies (e.g. emoji reactions).
- `route_for(chat_id)` / `restore_route(chat_id, route)` — the routing context a pending turn is journaled with, and how it comes back. Default is an empty dict and a no-op, which is right for a frontend whose chat id is already an address (Telegram). Slack must override both: `_session_key` is `sha256(channel:thread_ts)`, so the chat id cannot address the thread again and a replayed turn would run with nowhere to post. Telegram overrides them for its `/new` token, because `_load_sessions` runs inside `start()` — after pending turns are replayed — so without it a replay resumes the journaled session in the base session's workspace. Whatever `route_for` returns must be JSON-serializable and must not hold a credential: it goes to disk.
- `notify_resumed(chat_id, count)` — no-op by default, and meant to stay that way. A resumed turn is announced by the same things that announce a fresh one: the reaction it gets while it runs and the reply it posts. Only a frontend with no such affordance at all should say anything here.
- `notify_nudge(chat_id, text)` — offers a turn back, reached only for one that hit the replay limit. Default posts the prompt with `agent.strip_sender_markers` applied, because the markers are prompt grammar and quoting them back shows the person scaffolding they never wrote. Override if the platform has a tappable affordance.
- `notify_interrupted(chat_id, running=, queued=)` — called once per affected chat while the daemon shuts down, *before* `Orchestrator.shutdown` cancels anything. Slack overrides it to mark the affected messages `:arrows_counterclockwise:` and post nothing: every pending turn resumes, so prose here is the daemon narrating its own lifecycle, once per stop, and `r` in the dashboard is a stop plus a start. A frontend with no state to show falls back to `protocol.interrupted_notice(...)`. The whole pass is bounded by `orchestrator.SHUTDOWN_NOTICE_BUDGET_S` and sits inside `supervisor.SAFE_GRACE_S`, so a platform whose API has gone away costs the exit a few seconds rather than the SIGKILL window.
- `send_progress(chat_id, text)` (default no-op) — one mid-turn narration message while a turn runs, gated on `interim.progress`; only Slack implements it, and the implementation contract is on `Frontend.send_progress` in `protocol.py`. The coalescing and rate limiting live in `src/claude_on_the_fly/interim.py`, not in the adapter and not in `orchestrator.py` — how often a person wants to be interrupted is the same question on every platform.
- `timeout_for(chat_id)` — per-message subprocess timeout override; `None` uses the agent default.
- `describe()` — frontend-specific settings to print in the startup preview. Redact secrets before returning.

## Cross-cutting concerns

- **Persona**: `ensure_persona()` in `agent.py` is called per workspace; the new frontend just needs to construct a `Path` workspace and pass it through. To let one chat run different instructions than the rest, override `persona_source(chat_id)` (default `None` = the data-root `CLAUDE.md`) and resolve it through `agent.persona_for(platform, keys)`, which reads a `personas:` mapping from your section of `config.yaml`. The frontend supplies the candidate keys because only it knows which of its identifiers may decide a persona — Slack deliberately keys channels on the channel id/name and never on the sender, since a channel's sender changes per message while its workspace is per thread. Build the keys at lookup time, not when the session metadata is cached: an identifier that arrives later (a DM whose first message carried no `user` field) would otherwise be frozen out.
- **Stats footer**: `stats_mode(platform)` and `footer_parts(response, platform)` in `agent.py` (`agent.py:57`, `agent.py:68`) generate the per-channel footer from `<platform>.stats` in `config.yaml`, looked up by the platform's `{PLATFORM}_STATS_MODE` key. Add a `stats` key under your new frontend's section, and a `FIELDS` entry in `settings.py` pointing at it, if you want the same control.
- **Text formatting**: the agent writes plain Markdown on every platform (`FORMAT_HINTS` in `agent.py`), and each frontend renders it. Slack has two surfaces with different parsers, and picking the wrong one is the whole bug class here. A reply body goes in a `markdown` block, which Slack parses server-side into native `rich_text_list` (with real indent) and native `table` blocks; feed it the Markdown as written and convert nothing. Everything else on Slack is a `section` or `context` block, which take legacy `mrkdwn` only — no lists, no tables — so text bound for one of those goes through `slack_mrkdwn.to_mrkdwn()` first. The mid-turn progress line is the one place agent text still takes that path, because it lands in a `context`. A `markdown` block holds 12000 characters (characters, not bytes: 12000 CJK characters pass), and `slack_mrkdwn.split_blocks()` lays a longer body across several. Never hand-build `rich_text` blocks, and never put mrkdwn's `*bold*` or a literal `•` into a `markdown` block — both render as literal text.
- **Image / file input**: frontends save uploads into the workspace path; the agent reads them through its own file tooling. Don't try to pipe image bytes through the agent CLI.
- **File output (outbox)**: the agent attaches files by dropping them in `workspace/outbox/`. The orchestrator scans that folder after the run (`agent.collect_outbox`), sets `response.attachments`, lets `send()` upload them, then archives to `outbox/.sent/<ts>/` (`agent.archive_outbox`). This is gated on `agent.ATTACHMENT_PLATFORMS` — the single source of truth that also injects the outbox instruction into the system prompt. To support outbox on a new frontend: add its platform to `ATTACHMENT_PLATFORMS` and have `send()` upload `response.attachments`. Slack uploads via `files_upload_v2` and must record the share `ts` (echo guard, since it posts as the user); Telegram routes images to `send_photo`, else `send_document`.

## Slack control surface (token-kind split)

Slack exposes two control surfaces, chosen by the token kind (`_is_bot_token`, from the `xoxb`/`xoxp` prefix):

- **Text prefixes (`$continue`, `$stop`, `$compact`, and an opt-in job trigger)**: plain messages intercepted in `_ingest_event`, so they work under either token kind **and inside threads** — the only control surface Slack allows in threads (custom slash commands are hard-blocked there). `$stop` aborts the in-flight turn for that thread's session via `Orchestrator.abort`. `$compact` calls `Orchestrator.on_compact`, which **queues** the compaction as a `Turn` rather than running it inline: that way it inherits the reaction, the live status ticker, and `$stop`, and on a large thread it needs all three (compaction takes minutes, and silence reads as a dead daemon). Both are matched by exact equality, so `$compact the notes` is a message about notes. An **opt-in** fourth prefix, `slack.job_command` (e.g. `$job`), queues the rest of the message as a background job for the `claude-jobs` worker; it is intercepted after `$stop` and before the `$continue` soft-limit gate, so an enqueue is never blocked by the reply budget and never leaves a pending entry behind. Unset (the default) builds no queue and no intercept — the message is answered as ordinary text — which also keeps an unrelated `jobs.queue_kind` typo from taking down the Slack daemon. `checks.check_slack` validates a set trigger via `_job_command_error`, which rejects three kinds of mistake and says at each rule which one it is: Slack never delivers it, one of our own prefixes eats it, or it fires but not as its author means. Only that last class is a trap rather than a dead trigger, so the distinction is worth preserving if you touch those rules.
- **Bot token (`xoxb`) only**: the skill picker + a **message shortcut** ("Run a skill") + an **opt-in** `slack.slash_command`, registered by `_register_app_interactions()`. These are app interactions a user token never receives, and a slash command can't run in a thread — so the shortcut (from a message's `...` menu) is the thread-capable way to open the picker. Requires the manifest to declare the shortcut + `commands` scope + interactivity (+ the command, if set); all rides the existing Socket Mode connection (no public URL).

  The slash command is opt-in because Slack does not namespace slash commands: two installed apps declaring the same command means the most recent install wins workspace-wide and the other stops firing with no error. Unset (the default) registers no command and leaves the picker on the shortcut. The view/shortcut callback ids are app-scoped and can't collide, so they always register. `claude-slack --manifest` (`slack_manifest.py`) renders a manifest from `slack_manifest.json` that agrees with the env: per-mode scope blocks, and the `slash_commands` block only when a command was chosen. Its prompt default comes from `suggested_command()` (`/cof-<login name>`) rather than the app name — a prompt default gets accepted without reading it, so it has to be unique by construction, and the app name isn't (two installs keeping the default name would derive the same command). `checks.check_slack` rejects a command missing its leading `/`, which would otherwise register and never match.

Two things about the reply budget's `$continue` gate are load-bearing, and both are about the notice being *seen*. It is addressed to the sender (`<@id>`), and it is posted from a background task after `slack.reply_limit_notice_seconds` rather than inline. Posted the instant the message lands, it arrives while the sender still has the thread open, Slack marks it read on arrival, and they walk away with no unread badge believing their message is being worked on. There is one pending task per thread (`_gate_notices`), cancelled by `$continue`, session eviction, and `stop()` — a notice that posts *after* the thread resumed tells the user to send what they already sent. Each further gated message **restarts** the wait, because a burst means the sender is still typing and a notice landing between their messages is read on arrival just the same; `REPLY_LIMIT_NOTICE_MAX_HOLD` (30s, timed from the first gated message and carried across reschedules in `_gate_deadlines`) stops a fast talker deferring it forever. That ceiling is a guard on the delay rather than a second knob, so it is a module constant, not a setting. `_drop_gate_task` cancels without clearing the ceiling (the debounce path); `_cancel_gate_notice` clears both (the give-up paths). The task's own cleanup checks it is still the thread's current notice before popping, since a debounce cancels it *after* its replacement is already stored.

A message is acted on only when it's addressed to the bot — a DM the bot is in, or an @mention in a channel — and from an allowed sender. `channel_type` alone decides whether a tag is needed (`TAG_REQUIRED_CHANNEL_TYPES`: `channel`/`group` yes, `im`/`mpim` no), so the forgot-to-tag notice lives inside that branch and cannot fire in a DM. It fires once per thread (`_mention_hinted`) and only in a thread that already has a live session, since that's where somebody is talking *to* the bot and the missing tag is a slip; without that check it would answer ordinary channel chatter.

Both notices ship **off**, so an install that never opens `config.yaml` behaves exactly as it did before they existed: `reply_limit_notice_seconds` defaults to `0` (the gate notice posts the moment the message is gated, as it always did) and `mention_notice_seconds` defaults to `0`, which returns from `_hint_mention_required` before any state is touched — no task, no `_mention_hinted` entry, nothing to clean up. Where the notice is switched on it is **held** for that many seconds rather than posted inline, and for the same reason as the gate notice: the sender is watching the thread for a reply they think is coming, so a notice posted into that wait is read on arrival and forgotten. Two minutes of nothing means they have moved on, and it lands as an unread ping addressed to them. Another untagged message **restarts** the wait (still typing means still watching); a **tagged** message cancels it outright, since somebody who corrected themselves does not need telling. The delay is read when the notice is scheduled and carried into the task, so switching the setting off cannot be outlived by a task that wakes minutes later and posts anyway. So it only ever reaches the person who forgot *and* left — who is precisely the person who comes back to a thread and wonders why nothing happened. `_mention_hinted` is set when it posts, not when it is scheduled, so a cancelled notice leaves the thread still tellable later.

Routing: the slash command (`_handle_slash_command`) — bare opens the picker; anything else forwards `/<skill> args`. A slash command has no `thread_ts`, so it targets the channel/DM root. Turn control (stop / continue) is the `$stop` / `$continue` text prefix instead, since slash commands can't run in threads where the conversation lives. The **message shortcut** (`_handle_run_skill_shortcut`) carries the clicked message's thread, so its picker forward is thread-scoped. Both go through `_enter_command_session(channel, user_id, thread_ts)`.

The picker is a **`static_select`** built at modal-open time (`_open_skill_picker` → `_skill_option_groups`): skills are grouped into Block Kit `option_groups` by plugin namespace (`plugin:skill` → group "plugin"; plain names → "user"), so the whole list is browsable on open. `option_groups` lift the flat 100-option cap (up to 100 groups × 100), which matters since there are >100 skills; `external_select` was rejected because it's search-first (shows nothing until you type) and caps a single response at 100. The option value stays the full `plugin:skill` name so the forward matches. Skills come from `AgentBackend.list_skills()`, which returns `(name, description)` pairs (the description renders as a second line per option). claude reads skill names from the `system/init` stream-json probe and pulls descriptions from each `SKILL.md` front-matter (scanned across the init event's active plugin paths + `$CLAUDE_CONFIG_DIR/skills`, since the init event carries names only); codex scans `$CODEX_HOME/prompts/*.md` (each a `/<name>`, description from the file's front-matter). `agent.cached_skills()` wraps this in a TTL cache (`agent.skills_cache_ttl_seconds`, default 1h; `<= 0` disables it — probe every query) with an in-memory layer plus a JSON file under `DATA_DIR/cache`, so a picker opened before startup warm finishes is still instant instead of paying the ~0.8s cold CLI probe. Startup warm re-probes with `force=True` and overwrites the cache, so **restarting the daemon picks up newly installed/updated skills** (within a run the TTL governs; the cache alone would otherwise mask changes for up to the TTL, even across restarts).

`$stop` calls `Orchestrator.abort(chat_id)`, which cancels the drain task and clears the queue. Because every backend now spawns with `start_new_session=True` and reaps via `agent._kill_process_tree` (SIGKILL to the process group), cancelling a turn kills the agent CLI *and* its tool subprocesses instead of orphaning them.

## Compaction

A long thread's cost is dominated by re-establishing the prompt cache, so `Orchestrator` can shrink the conversation via `agent.compact` (see [backends.md](backends.md) for why that never runs under pty). Two ways in, one mechanism: `$compact` queues one on demand, and `agent.auto_compact_pct` queues one automatically ahead of an inbound message when the previous turn left the context above that share of the model's window.

The automatic path fires **on the returning message, not during the idle window before it**. Compaction is a full-context pass; a thread nobody comes back to would pay for it and get nothing. Waiting until someone actually speaks costs them that turn's latency and saves every turn after it. The reading it compares against comes from `Response.context_tokens` / `context_window_size`, which only `native` and `pty` supply — see [backends.md](backends.md) for why `ollama` withholds it. `checks.check_backend` warns when the threshold is set under a backend that cannot fire it, rather than leaving a dead setting looking live.

Telegram reaches this through the same `Orchestrator.on_message`, so it compacts automatically too, and gets `/compact` as a real bot command — Telegram delivers slash commands everywhere, so Slack's `$` prefix (a workaround for slash commands being blocked in threads) buys nothing there.

Two traps if you touch the threshold. `_due_for_compaction` **consumes** the reading, so a burst of messages queues one compaction rather than one each — the second would buy a full-context pass to be told there was nothing left to do. And the prompt has a floor: the system prompt and tool schemas are tens of thousands of tokens that no compaction touches, so a barely-used session already reads ~7% of a 1M window. A threshold set near that floor compacts constantly and saves nothing.

## Cron is NOT a frontend

`cron.py` used to implement `Frontend`, because firing a scheduled prompt needed an
agent run and the shared `Orchestrator` was the only thing that ran agents with
sessions and workspaces. `jobs/` is that thing now, so the daemon dropped the
protocol entirely: it runs shell and enqueues `Job`s, and never calls `agent.run`.

The lesson generalizes. `Frontend` is for request/response — something asks, the
agent answers, the answer goes back to the asker. If your new thing polls, or
produces work whose reply belongs somewhere other than the caller, it is a
producer: emit `Job`s and let the worker run them. Ask before making it a
`Frontend`.

## Recovery after a stop

`turns.py` is the durable half. A turn is journaled when it is *accepted*, so the
record survives SIGKILL, a `--force` past the supervisor's grace, and a panic --
a shutdown-time write would cover only a clean SIGTERM.

Two phases, and the difference is what a frontend's messages must promise:

| Phase | Meaning | Replayed as |
|---|---|---|
| `QUEUED` | Never handed to an agent | The prompt, verbatim |
| `DISPATCHED` | An agent was started | The prompt, prefixed with `orchestrator.RESUME_TEMPLATE` |

Both resume, silently. The phase decides only what the resumed turn is *told*: a
dispatched one may already have written files, posted messages, or pushed commits,
so its prompt carries a system note saying so and asking it to check the current
state before repeating anything that writes, sends, or publishes. Holding the turn
back instead was the earlier design and it was wrong in practice -- somebody who
asked for work wants it done, not handed back.

The one exception is a turn that has been replayed to its limit
(`turns.MAX_REPLAYS`): running it again is the likeliest reason the daemon keeps
going down, so that one is offered back through `notify_nudge`.

There is deliberately no third "started but has not acted yet" phase -- both
backends build their tool-event relay only when interim progress is on, so it
would need new hooks in both and would change nothing now that both phases
resume.

**Both ends of the pause are meant to be wordless.** The stop marks the message
`INTERRUPTED_EMOJI` and the resume moves it to `RUNNING_EMOJI`. Three states, three
glyphs: reusing the queue's `QUEUED_EMOJI` for a restart would make "waiting its
turn" and "interrupted" the same thing on screen. `notify_start` clears both waiting
marks, because a turn can have been queued and then interrupted, in either order.
Nothing is posted at either end.

On Slack the journaled
route carries `message_ts`, the message that *asked*, alongside the thread that
receives the reply; `restore_route` puts it back into `_pending_msg` so
`notify_start` swaps the hourglass for eyes on the original message and
`notify_complete` clears it when the reply lands. A frontend adding recovery
support should carry whatever its own progress indicator needs in the same way.

`resume_pending` runs between `_start_sandbox` and `frontend.start`: a replayed
turn spawns a jailed agent that needs the broker, the proxy and the shims, and
queueing before the listener starts is what stops a live message from overtaking
work that was already waiting. The journal is emptied before anything is replayed,
and each replay carries a counter, so a turn that kills the daemon parks instead
of being replayed at every start.

A turn stopped on purpose (`$stop`, `abort`) has its record dropped, or the stop
would come back after the next restart.

The agent can neither read nor write this file. Both sandbox profiles deny
`state/` in both directions, `tests/test_sandbox_parity.py` carries the contract,
and `sandbox._probe_write` proves the write deny at startup. That is not merely
privacy: a journal entry is replayed as a user message, so a writable journal is
a prompt the agent could schedule for itself past any approval gate.
