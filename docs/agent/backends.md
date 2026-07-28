# Backends

Two agent CLIs are supported: `claude` (default) and `codex`. The dispatch layer is `src/claude_on_the_fly/agent.py`; each backend lives in `src/claude_on_the_fly/backends/`.

## Contract

Every backend implements the `AgentBackend` Protocol at `src/claude_on_the_fly/agent.py:594`:

- `run(workspace, session_uuid, prompt, platform, user_name, channel_context, timeout) -> Response` — execute one turn
- `takeover_command(workspace, session_uuid) -> str | None` — interactive resume command, or `None` if no session yet
- `session_log_path(workspace, session_uuid) -> Path | None` — live JSONL for `claude-symphony watch` to tail, or `None`
- `list_skills() -> list[tuple[str, str]]` — `(name, description)` pairs for the Slack picker; empty when the backend has no skill concept

A new backend means: implement the protocol, add a builder in `agent.py:_build_*_backend()` (see `_build_claude_backend` at `agent.py:786`), and add the env-driven switch in `get_backend()` at `agent.py:706`.

## Behavioral differences that affect integration code

These are the differences that *downstream* code (footer, orchestrator, TUI) has to compensate for. If you change one, grep for the comment header below.

### Cost

- **claude** (`backends/claude.py`): native CLI emits cost in the JSONL — use it as-is.
- **codex** (`backends/codex.py:370`): no cost field. Pricing layer at `src/claude_on_the_fly/pricing.py` looks up models in OpenRouter's registry, multiplies by tokens. Cache at `~/.claude-on-the-fly/pricing/openrouter.json`, TTL 7 days (`COTF_PRICING_TTL_SECONDS`).

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

## Persona / cross-backend handoff

- `ensure_persona()` at `src/claude_on_the_fly/agent.py:140` symlinks the global `~/.claude-on-the-fly/CLAUDE.md` into every workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex) — see `PERSONA_FILENAMES` at `agent.py:137`.
- `transcript.py` handles cross-backend handoff: when the daemon switches backends, it parses the prior backend's session JSONL into a single prompt so context carries over. If you're changing session-log paths or output schemas in a backend, this is the file that will break.
- `remove_workspace_sessions()` deletes the session directory claude keys to a workspace path but keeps *outside* it (`~/.claude/projects/<hash>/`). codex keeps its mapping inside the workspace, so deleting the workspace is enough for it.
