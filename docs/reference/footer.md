# Response Footer

Controlled per channel via `{SLACK,TELEGRAM}_STATS_MODE`:

- `off` — no footer
- `summary` (default) — single stats line
- `detailed` — stats line + tool-use breakdown

```
$0.0471 | 3.6s | ↑1523 ↓42 | ctx 21% | claude-sonnet-4-6
🔧 8 (Read×4 Bash×3 Grep×1)
```

When the current prompt size and model context window are available, the footer
includes `ctx N%`. PTY mode uses Claude Code's statusline percentage directly;
native Claude and Codex modes derive the percentage from the backend-reported
prompt size and context window. If either value is unavailable, the percentage
is omitted.

Ollama mode has no window it can read: claude prints its own table's figure for
a model it is not talking to. Set `agent.ollama.context_window` to the window you
want measured against and `ctx N%` appears there too. Leave it unset and the
field stays omitted.

PTY mode can additionally include `5h N% → HH:MM` (5-hour rate-limit budget
with reset time). Tool/skill counts are not surfaced in PTY mode.
