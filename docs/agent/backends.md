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
- **codex** (`backends/codex.py:370`): no cost field. Pricing layer at `src/claude_on_the_fly/pricing.py` looks up models in OpenRouter's registry, multiplies by tokens. Cache at `~/.claude-on-the-fly/pricing/openrouter.json`, TTL 7 days (`COTF_PRICING_TTL_SECONDS`).
- **claude under `ollama`**: also priced from that registry, because the CLI's `total_cost_usd` comes from Anthropic's table for a model Anthropic isn't serving.

`cost_for` prices four **non-overlapping** buckets: plain input, output, cache reads, cache writes. The cache arguments default to 0, which is what keeps codex (no prompt caching at all) billing identically. Don't feed it `Response.tokens_in` — that folds cache reads in for the footer's `↑N`, so passing it alongside `cache_read_tokens` bills those twice at the dearer rate. `_billable_usage` in the claude backend exists to keep the two apart.

Cache rates fall back to the *prompt* rate when a model publishes none (184 of 342 registry entries publish `input_cache_read`, only 55 publish `input_cache_write`), because for those a cache write genuinely is billed as ordinary input — falling back to zero would make the largest term on a cold thread free. A published `0` is honoured as free. Reading only `prompt`/`completion`, as this did originally, understated a measured turn by 39-43% across the models in use: cache writes were dropped entirely while cache reads were billed at up to 10x their real rate, so the two errors didn't cancel.

### Session resume

- **claude**: `--resume <uuid>` works for both create and resume.
- **codex**: assigns its own `thread_id`. Persist `<workspace>/.codex_sessions/<our-uuid>` mapping after first turn, then `resume <thread_id>` on follow-ups.

### Tool / skill counts (footer display)

- **claude**: skills populated by CLI; tools from `tool_use` blocks.
- **codex**: no skill concept in the footer (`skill_counts` always empty). Tools from `item.completed` events. `list_skills()` scans `$CODEX_HOME/prompts/*.md` for the picker.

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

Wraps the agent CLI in `ollama launch <agent> --model <X> --yes --`. Implementation: `OllamaLauncher` at `src/claude_on_the_fly/agent.py:636`. Triggered by `CLAUDE_MODE=ollama` / `CODEX_MODE=ollama`. Requires `ollama` installed and target model pulled (`ollama pull <name>`). Session resume, tool use, and Skills behave identically to native mode — only the model provider changes. For claude, the footer cost reflects Ollama's billing (`:cloud` models) or `$0` (local).

### pty (claude only)

Drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) under a PTY, surfacing `ctx N%` and `5h N% → HH:MM` in the footer (fields `claude -p` doesn't expose). Wall-clock ~1–2s slower per turn. Doctor surfaces three stale-install failure modes: missing `claude-pty` binary, missing `jq`, or missing Stop-hook / statusline-shim wiring in `~/.claude/settings.json`. Tool/skill counts are not surfaced in pty mode.

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

- `ensure_persona()` at `src/claude_on_the_fly/agent.py:140` symlinks the global `~/.claude-on-the-fly/CLAUDE.md` into every workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex) — see `PERSONA_FILENAMES` at `agent.py:137`.
- `transcript.py` handles cross-backend handoff: when the daemon switches backends, it parses the prior backend's session JSONL into a single prompt so context carries over. If you're changing session-log paths or output schemas in a backend, this is the file that will break.
- `remove_workspace_sessions()` deletes the session directory claude keys to a workspace path but keeps *outside* it (`~/.claude/projects/<hash>/`). codex keeps its mapping inside the workspace, so deleting the workspace is enough for it.
