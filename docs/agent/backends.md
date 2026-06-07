# Backends

Three agent CLIs are supported: `claude` (default), `codex`, `pi`. The dispatch layer is `src/claude_on_the_fly/agent.py`; each backend lives in `src/claude_on_the_fly/backends/`.

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

### Session resume

- **claude**: `--resume <uuid>` works for both create and resume.
- **codex**: assigns its own `thread_id`. Persist `<workspace>/.codex_sessions/<our-uuid>` mapping after first turn, then `resume <thread_id>` on follow-ups.
- **pi**: stores sessions at `~/.pi/agent/sessions/<workspace-hash>/<timestamp>_<uuid>.jsonl`. Uses `pi --session-id <uuid>` for both create and resume; auto-detects existing sessions.

### Tool / skill counts (footer display)

- **claude**: skills populated by CLI; tools from `tool_use` blocks.
- **codex**: no skill concept (`skill_counts` always empty). Tools from `item.completed` events.
- **pi**: no skill concept. Tools from `toolCall` content blocks.

### System prompt

- **claude**: `--system-prompt` flag.
- **codex**: no flag — format hint is prepended to each user message.
- **pi**: has both `--system-prompt` and `--append-system-prompt`. Use `--system-prompt` for the full prompt.

### Permission mode

- **claude**: `--permission-mode bypassPermissions` (per the experimental warning in README).
- **codex** / **pi**: no flag — built-in tools (`read`, `bash`, `edit`, `write`) all enabled by default.

### Image input

All three: frontends save images to workspace, agent reads through its own file tooling. No `-i` flag or equivalent. Models are multimodal.

## Transport modes

### ollama launch

Wraps the agent CLI in `ollama launch <agent> --model <X> --yes --`. Implementation: `OllamaLauncher` at `src/claude_on_the_fly/agent.py:416`. Triggered by `CLAUDE_MODE=ollama` / `CODEX_MODE=ollama` / `PI_MODE=ollama`. Requires `ollama` installed and target model pulled (`ollama pull <name>`). Session resume, tool use, and Skills behave identically to native mode — only the model provider changes. For claude, the footer cost reflects Ollama's billing (`:cloud` models) or `$0` (local).

### pty (claude only)

Drives `claude-pty` from [claude-interactive-p](https://github.com/CJHwong/claude-interactive-p) under a PTY, surfacing `ctx N%` and `5h N% → HH:MM` in the footer (fields `claude -p` doesn't expose). Wall-clock ~1–2s slower per turn. Doctor surfaces three stale-install failure modes: missing `claude-pty` binary, missing `jq`, or missing Stop-hook / statusline-shim wiring in `~/.claude/settings.json`. Tool/skill counts are not surfaced in pty mode.

## Persona / cross-backend handoff

- `ensure_persona()` at `src/claude_on_the_fly/agent.py:40` symlinks the global `~/.claude-on-the-fly/CLAUDE.md` into every workspace as both `CLAUDE.md` (for claude) and `AGENTS.md` (for codex) — see `PERSONA_FILENAMES` at `agent.py:37`.
- `transcript.py` handles cross-backend handoff: when the daemon switches backends, it parses the prior backend's session JSONL into a single prompt so context carries over. If you're changing session-log paths or output schemas in a backend, this is the file that will break.
