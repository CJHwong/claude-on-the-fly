# Backends

Four agent CLIs are supported: `claude` (default), `codex`, `pi`, `opencode`. The dispatch layer is `src/claude_on_the_fly/agent.py`; each backend lives in `src/claude_on_the_fly/backends/`.

## Contract

Every backend implements the `AgentBackend` Protocol at `src/claude_on_the_fly/agent.py:383`:

- `run(workspace, session_uuid, prompt, platform, user_name, channel_context, timeout) -> Response` — execute one turn
- `takeover_command(workspace, session_uuid) -> str | None` — interactive resume command, or `None` if no session yet
- `session_log_path(workspace, session_uuid) -> Path | None` — live JSONL for `claude-symphony watch` to tail, or `None`

A new backend means: implement the protocol, add a builder in `agent.py:_build_*_backend()` (see `_build_claude_backend` at `agent.py:518`), and add the env-driven switch in `get_backend()` at `agent.py:425`.

## Behavioral differences that affect integration code

These are the differences that *downstream* code (footer, orchestrator, TUI) has to compensate for. If you change one, grep for the comment header below.

### Cost

- **claude** (`backends/claude.py`): native CLI emits cost in the JSONL — use it as-is.
- **codex** (`backends/codex.py:128`): no cost field. Pricing layer at `src/claude_on_the_fly/pricing.py` looks up models in OpenRouter's registry, multiplies by tokens. Cache at `~/.claude-on-the-fly/pricing/openrouter.json`, TTL 7 days (`COTF_PRICING_TTL_SECONDS`).
- **pi** (`backends/pi.py:187`): same OpenRouter lookup as codex.
- **opencode** (`backends/opencode.py`): native cost in each `step_finish` event — summed across steps, used as-is. No OpenRouter lookup.

### Session resume

- **claude**: `--resume <uuid>` works for both create and resume.
- **codex**: assigns its own `thread_id`. Persist `<workspace>/.codex_sessions/<our-uuid>` mapping after first turn, then `resume <thread_id>` on follow-ups.
- **pi**: stores sessions at `~/.pi/agent/sessions/<workspace-hash>/<timestamp>_<uuid>.jsonl`. Uses `pi --session-id <uuid>` for both create and resume; auto-detects existing sessions.
- **opencode**: mints its own `ses_…` id (we can't pre-seed our UUID). Persist `<workspace>/.opencode_sessions/<our-uuid>` -> `ses_…` after the first turn — rewritten every turn so its mtime tracks recency for handoff ordering — then `opencode run -s <ses_…>` on follow-ups. Same shape as codex.

### Tool / skill counts (footer display)

- **claude**: skills populated by CLI; tools from `tool_use` blocks.
- **codex**: no skill concept (`skill_counts` always empty). Tools from `item.completed` events.
- **pi**: no skill concept. Tools from `toolCall` content blocks.
- **opencode**: no skill concept. Tools from `tool_use` events (`part.tool`), counted once on completion.

### System prompt

- **claude**: `--system-prompt` flag.
- **codex**: no flag — format hint is prepended to each user message.
- **pi**: has both `--system-prompt` and `--append-system-prompt`. Use `--system-prompt` for the full prompt.
- **opencode**: no run flag — reads `AGENTS.md` (symlinked by `ensure_persona`), and the system prompt is prepended to the first user message (codex pattern).

### Permission mode

- **claude**: `--permission-mode bypassPermissions` (per the experimental warning in README).
- **codex** / **pi**: no flag — built-in tools (`read`, `bash`, `edit`, `write`) all enabled by default.
- **opencode**: `--dangerously-skip-permissions` (auto-approves anything not explicitly denied).

### Image input

All four: frontends save images to workspace, agent reads through its own file tooling. No `-i` flag or equivalent. Models are multimodal.

## Rough performance footprint

Rough numbers from one cold turn (a trivial one-word prompt, no tools) driven through `ollama launch <cli> --model deepseek-v4-flash:cloud` so every backend hits the same model — this isolates per-CLI overhead from model differences. n=2, single machine; wall time includes the `ollama launch` spawn and the cloud round-trip, so absolute times are network-dependent. **Treat the relative ordering as the signal, not the exact figures.**

| Backend  | Wall / cold turn | Peak process-tree RSS | Context the CLI injects | CLI runtime |
|----------|------------------|-----------------------|-------------------------|-------------|
| pi       | ~3–6s            | ~250 MB               | ~4.8k tokens            | Node        |
| claude   | ~4–5s            | ~430 MB               | ~33.5k tokens           | Node        |
| opencode | ~5s              | ~520 MB               | ~12.3k tokens           | Bun + spawns a local server |
| codex    | ~10–13s          | ~175 MB               | ~26.8k tokens           | Rust        |

Reading it:

- **Latency:** pi is fastest; codex is the slowest and most variable.
- **Memory:** codex is lightest (Rust binary, ~175 MB); opencode is heaviest (~520 MB) because it boots a Bun server per run. The Python daemon baseline is ~28 MB, so the rest is the CLI tree.
- **Context overhead** (tokens each CLI prepends before the user prompt) spans 7× — pi ~4.8k to claude ~33.5k — which drives per-turn cost: for this one-word reply, pi cost ~$0.0005 vs claude/codex ~$0.0026–0.0033 on deepseek pricing.
- **opencode shows `$0` in ollama mode** — it only emits native cost for priced providers; through ollama there's no price. In native mode (e.g. `github-copilot/*`) it reports real cost.

Warm resumes are faster than these cold numbers and skip the first-turn system-prompt re-send.

## Transport modes

### ollama launch

Wraps the agent CLI in `ollama launch <agent> --model <X> --yes --`. Implementation: `OllamaLauncher` at `src/claude_on_the_fly/agent.py:416`. Triggered by `CLAUDE_MODE=ollama` / `CODEX_MODE=ollama` / `PI_MODE=ollama` / `OPENCODE_MODE=ollama`. Requires `ollama` installed and target model pulled (`ollama pull <name>`). Session resume, tool use, and Skills behave identically to native mode — only the model provider changes. For claude, the footer cost reflects Ollama's billing (`:cloud` models) or `$0` (local).

### pty (claude only)

Drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) under a PTY, surfacing `ctx N%` and `5h N% → HH:MM` in the footer (fields `claude -p` doesn't expose). Wall-clock ~1–2s slower per turn. Doctor surfaces three stale-install failure modes: missing `claude-pty` binary, missing `jq`, or missing Stop-hook / statusline-shim wiring in `~/.claude/settings.json`. Tool/skill counts are not surfaced in pty mode.

## Persona / cross-backend handoff

- `ensure_persona()` at `src/claude_on_the_fly/agent.py:40` symlinks the global `~/.claude-on-the-fly/CLAUDE.md` into every workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex) — see `PERSONA_FILENAMES` at `agent.py:37`.
- `transcript.py` handles cross-backend handoff: when the daemon switches backends, it parses the prior backend's session JSONL into a single prompt so context carries over. If you're changing session-log paths or output schemas in a backend, this is the file that will break.
- **opencode** has no per-session JSONL — its content lives in a global SQLite db. `extract_opencode` reads it back via `opencode export <ses_id>` (resolving our uuid through the `.opencode_sessions` mapping), so handoff works in both directions. For the same reason `session_log_path` returns `None`: there's no single file for `claude-symphony watch` to tail.
