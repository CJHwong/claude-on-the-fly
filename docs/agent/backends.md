# Backends

Two agent CLIs are supported: `claude` (default) and `codex`. The dispatch layer is `src/claude_on_the_fly/agent.py`; each backend lives in `src/claude_on_the_fly/backends/`.

## Contract

Every backend implements the `AgentBackend` Protocol at `src/claude_on_the_fly/agent.py:594`:

- `run(workspace, session_uuid, prompt, platform, user_name, channel_context, timeout) -> Response` — execute one turn
- `takeover_command(workspace, session_uuid) -> str | None` — interactive resume command, or `None` if no session yet
- `session_log_path(workspace, session_uuid) -> Path | None` — live JSONL for the TUI's watch pane to tail, or `None`
- `list_skills() -> list[tuple[str, str]]` — `(name, description)` pairs for the Slack picker; empty when the backend has no skill concept

A new backend means: implement the protocol, add a builder in `agent.py:_build_*_backend()` (see `_build_claude_backend` at `agent.py:786`), and add the env-driven switch in `get_backend()` at `agent.py:706`.

## Behavioral differences that affect integration code

These are the differences that *downstream* code (footer, orchestrator, TUI) has to compensate for. If you change one, grep for the comment header below.

### Cost

- **claude** (`backends/claude.py`): native CLI emits cost in the JSONL — use it as-is.
- **codex** (`backends/codex.py:370`): no cost field. Pricing layer at `src/claude_on_the_fly/pricing.py` looks up models in OpenRouter's registry, multiplies by tokens. Cache at `~/.claude-on-the-fly/pricing/openrouter.json`, TTL 7 days (`agent.pricing_ttl_seconds`).
- **claude under `ollama`**: also priced from that registry, because the CLI's `total_cost_usd` comes from Anthropic's table for a model Anthropic isn't serving.

`cost_for` prices four **non-overlapping** buckets: plain input, output, cache reads, cache writes. The cache arguments default to 0, which is what keeps codex (no prompt caching at all) billing identically. Don't feed it `Response.tokens_in` — that folds cache reads in for the footer's `↑N`, so passing it alongside `cache_read_tokens` bills those twice at the dearer rate. `_billable_usage` in the claude backend exists to keep the two apart.

Cache rates fall back to the *prompt* rate when a model publishes none (184 of 342 registry entries publish `input_cache_read`, only 55 publish `input_cache_write`), because for those a cache write genuinely is billed as ordinary input — falling back to zero would make the largest term on a cold thread free. A published `0` is honoured as free. Reading only `prompt`/`completion`, as this did originally, understated a measured turn by 39-43% across the models in use: cache writes were dropped entirely while cache reads were billed at up to 10x their real rate, so the two errors didn't cancel.

### Session resume

- **claude**: `--resume <uuid>` works for both create and resume.
- **codex**: assigns its own `thread_id`. Persist the authenticated mapping in the
  daemon-owned `~/.claude-on-the-fly/codex-sessions/` store after the first turn, then
  `resume <thread_id>` on follow-ups. The store is outside the agent-writable workspace,
  reads reject symlinks, and new records are atomic owner-only (`0600`) files.

### Tool / skill counts (footer display)

- **claude**: skills populated by CLI; tools from `tool_use` blocks.
- **codex**: no skill concept in the footer (`skill_counts` always empty). Tools from `item.completed` events. `list_skills()` scans `$CODEX_HOME/prompts/*.md` for the picker.

### Interim progress

When enabled, native and Ollama turns from both backends can forward mid-turn
narration through the shared progress sink. Claude consumes its stream line by line
in `agent._consume`; Codex observes its `--json` JSONL chunks incrementally and keeps
partial lines until the next chunk arrives. An `agent_message` is held until a tool
event proves it is narration, then the existing `InterimProgress` relay applies its
warm-up and pacing policy. The final Codex message is discarded from the relay when
`turn.completed` arrives because `Response.body` posts it once as the answer.

Progress delivery is best effort: a parser or frontend failure is logged and never
turns a successful agent run into an error. Claude pty has no line-oriented stream,
and Telegram still inherits the frontend no-op, so both remain unsupported.

### System prompt

- **claude**: `--system-prompt` flag.
- **codex**: no flag — format hint is prepended to each user message.

### Permission mode

- **claude**: `--permission-mode bypassPermissions` (per the experimental warning in README).
- **codex**: no flag — built-in tools (`read`, `bash`, `edit`, `write`) all enabled by default.

### Image input

Both: frontends save images to workspace, agent reads through its own file tooling. No `-i` flag or equivalent. Models are multimodal.

## Rough performance footprint

Rough numbers from one cold turn (a trivial one-word prompt, no tools) driven through `ollama launch <cli> --model deepseek-v4-flash:cloud` so both backends hit the same model — this isolates per-CLI overhead from model differences. n=2, single machine; wall time includes the `ollama launch` spawn and the cloud round-trip, so absolute times are network-dependent. **Treat the relative ordering as the signal, not the exact figures.**

| Backend  | Wall / cold turn | Peak process-tree RSS | Context the CLI injects | CLI runtime |
|----------|------------------|-----------------------|-------------------------|-------------|
| claude   | ~4–5s            | ~430 MB               | ~33.5k tokens           | Node        |
| codex    | ~10–13s          | ~175 MB               | ~26.8k tokens           | Rust        |

Reading it:

- **Latency:** codex is the slower and more variable of the two.
- **Memory:** codex is lighter (Rust binary, ~175 MB vs claude's ~430 MB Node tree). The Python daemon baseline is ~28 MB, so the rest is the CLI tree.
- **Context overhead** (tokens each CLI prepends before the user prompt) drives per-turn cost: for this one-word reply, both landed at ~$0.0026–0.0033 on deepseek pricing.

Warm resumes are faster than these cold numbers and skip the first-turn system-prompt re-send.

## Transport modes

### ollama launch

Wraps the agent CLI in `ollama launch <agent> --model <X> --yes --`. Implementation: `OllamaLauncher` at `src/claude_on_the_fly/agent.py:636`. Triggered by `agent.claude.mode: ollama` / `agent.codex.mode: ollama`. Requires `ollama` installed and target model pulled (`ollama pull <name>`). Session resume, tool use, and Skills behave identically to native mode — only the model provider changes. For claude, the footer cost reflects Ollama's billing (`:cloud` models) or `$0` (local).

### pty (claude only)

Drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) under a PTY, surfacing `ctx N%` and `5h N% → HH:MM` in the footer (fields `claude -p` doesn't expose). Wall-clock ~1–2s slower per turn. Doctor surfaces three stale-install failure modes: missing `claude-pty` binary, missing `jq`, or missing Stop-hook / statusline-shim wiring in `~/.claude/settings.json`. Tool/skill counts are not surfaced in pty mode.

**pty spawns trust their workspace first.** claude records a per-directory trust decision and skips the dialog only in non-interactive mode — `-p`, or a non-TTY stdout ([`claude --help`](https://docs.claude.com/en/docs/claude-code/cli-reference), verified on 2.1.220). Native mode therefore never sees it and pty always does, because handing claude a TTY is what pty is for. An untrusted directory does not fail the turn, it stops on `❯ 1. Yes, I trust this folder` and waits for a keystroke nobody sends, so the turn ends at the caller's timeout with no output. No flag suppresses it interactively and no subcommand grants it (`claude project` only purges), which leaves claude's own project-state file as the only lever. `pty_install.ensure_workspace_trusted` sets `projects.<workspace>.hasTrustDialogAccepted` there as the first statement of `_exec_pty`, before the spawn — after would be useless, since claude waits rather than failing.

Bounded three ways. Only paths under `DATA_DIR/workspaces` qualify (`cotf_owns_workspace` resolves before comparing, so `..` cannot smuggle one in), so a session pointed at an operator's own checkout is left alone. Only that one key is written, through a temp file and `os.replace` at `0600`, and a state file that fails to parse is reported rather than replaced — the file is claude's, reaches megabytes, and rewriting it from a failed parse would discard the lot. `st_mtime_ns` is compared before the replace and the read retried (three attempts) when it moved, because claude rewrites this file on its own schedule; that is a re-read, not a lock, since claude takes no lock either. A successful grant is memoized per process, so a daemon does not re-parse a megabyte per turn.

It grants no privilege: cotf created the directory and already runs claude with `--permission-mode bypassPermissions`. The seatbelt jail and the approval gate are the controls, and neither is touched. Two limits worth knowing: on the first-run path where the file does not exist yet there is no mtime to compare, so a file claude creates inside that window would be overwritten; and the process memo does not notice a trust key stripped by hand, which stays memoized until restart. A failed grant is logged and the spawn proceeds anyway, degrading to the old hang rather than refusing the turn.

**The state file is not inside `CLAUDE_CONFIG_DIR` by default.** It is `$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set, and `~/.claude.json` at *home root* when it is not — not `~/.claude/.claude.json`, which on a machine that once set the variable may exist as an unrelated stale file with no `projects` key at all. `pty_install.claude_state_file` encodes that split and resolves the variable through `envfile`, so it reads what the daemon receives rather than what the viewing shell exports.

**Compaction under pty needs claude-interactive-p's `PostCompact` hook.** claude-pty's envelope is written by its **Stop** hook, and a compaction fires no Stop hook: it produces no assistant message. Without a second writer the envelope never appears, the TUI drops back to waiting for input, and claude-pty's poll loop has no wall-clock cap of its own — so the run only ends when the caller's timeout does, an hour by default. `hooks/postcompact_envelope.sh` writes it instead, gated on `trigger == "manual"`: Claude Code's *own* mid-turn compaction lands as `trigger: "auto"` while the real turn is still running, and an envelope there would end the turn early and return the summary in place of the answer. `checks.check_pty_hooks` warns (does not block) when the hook is absent, so an older install keeps running ordinary turns and only loses compaction.

`ClaudeBackend.compact` runs in whichever mode the backend is in — no cross-mode shortcut. Reaching for `claude -p` from a pty backend would forfeit what pty is for (an operator can `tmux attach` to a live turn, and a compaction is the longest, priciest thing a thread does, so the worst one to make invisible), and the two resolve their own effort / fast-mode / output-style settings, so a `-p` compaction could summarize a conversation under settings it never ran under.

Whether a compaction happened is reported in-band in both modes, under the same `compact` key. Native `-p` emits `{"type":"system","subtype":"status","status":"compacting"}` and then a `compact_result`, folded in by `agent._fold`; the pty PostCompact hook writes the equivalent subtree into its envelope. Without that signal a compaction is indistinguishable from a turn that died — both end `subtype: "success"` with an empty `result` — which is why `run()` checks for one before reaching for `NUDGE_PROMPT`. The token numbers come from the transcript either way, out of the newest `compact_boundary` record's `compactMetadata` (`preTokens` / `postTokens` / `durationMs`); those count the conversation, not the billed prompt, which also carries the uncompactable system prompt and tool schemas.

### Compaction

Both backends compact, by unrelated mechanisms.

**claude** takes `/compact` as a prompt. The CLI intercepts it, emits `status: "compacting"` then a `compact_result`, and writes a `compact_boundary` into the transcript carrying `preTokens` / `postTokens` / `durationMs`. It is a standalone operation: no turn, no reply.

**codex** has no compaction command we can send. `thread/compact/start` belongs to the app-server protocol, which `exec` doesn't speak — and sending `/compact` as a prompt is the trap: `exec` *recognizes* it (a bogus slash command answers `Unknown command`) and replies `Context compacted.`, but the context is untouched. Measured on one thread: 45,730 → 46,335 → 46,357 tokens, with none of the compaction bookkeeping the binary defines appearing in the rollout. The turn compacts in memory and `exec` exits without writing it back, so the next `resume` rebuilds from an unmodified rollout. **Never send it.**

What works is the threshold codex checks itself before each turn (`run_auto_compact`, in exec's own code path). `CodexBackend.compact` passes `-c model_auto_compact_token_limit=<low>` for that one run, so the check fires and the user's `~/.codex/config.toml` is never touched. Same thread: 46,357 → 18,507, and it survived later plain resumes.

Two consequences of codex's route. It needs a turn to hang off, so it spends one cheap exchange where claude spends none. And it publishes no in-band signal, so success is judged by whether the prompt actually shrank — reporting success off the trigger alone would repeat the exact lie `/compact` tells. That also means no duration, which is why `Compaction.summary()` omits the timing when it is absent rather than printing "in 0s".

### Context readings

Both modes report prompt size and window size, which is what the auto-compact gate thresholds on. pty reads them off the statusline (`context_window.total_input_tokens` / `context_window_size`). Native derives them in `_native_context_fields`: all three `usage` terms summed, paired with the widest `modelUsage[…].contextWindow`. codex reports both in its rollout instead of on stdout — `last_token_usage.input_tokens` for the turn's own prompt and `model_context_window` for the window, read by `transcript.extract_codex_prompt_tokens` (which skips the `input_tokens: 0` turn a compaction reports, since that describes the pass rather than the context it left behind). **ollama withholds them**: the claude CLI fills in `contextWindow` from its own table even when ollama is serving another vendor's model (200000 observed for `glm-5.2:cloud`), so the figure describes a model that is not answering. Cost has a real substitute in the OpenRouter registry; a context window has none, so reporting nothing switches the auto-compact gate off there instead of thresholding against a made-up denominator. `$compact` is unaffected. Note `Response.tokens_in` is **not** usable for this — it omits `cache_creation_input_tokens` by design, and on a cold cache that is most of the prompt (22k of 40k on a measured turn), which is exactly the case the feature exists for.

## Persona / cross-backend handoff

- `ensure_persona()` in `agent.py` symlinks a persona into every workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex) — see `PERSONA_FILENAMES`. The source is `~/.claude-on-the-fly/CLAUDE.md` unless the caller resolved a per-chat one through `agent.persona_for` (Slack channel, Telegram chat, or job key) through its `personas:` table.
- `transcript.py` handles cross-backend handoff: when the daemon switches backends, it parses the prior backend's session JSONL into a single prompt so context carries over. If you're changing session-log paths or output schemas in a backend, this is the file that will break.
- `remove_workspace_sessions()` deletes the session directory claude keys to a workspace path but keeps *outside* it (`~/.claude/projects/<hash>/`) and removes matching Codex mappings from the daemon-owned store.
