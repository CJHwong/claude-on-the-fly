# Response Footer

Controlled per channel via `{TELEGRAM,SLACK,GMAIL}_STATS_MODE`:

- `off` — no footer
- `summary` (default) — single stats line
- `detailed` — stats line + tool-use breakdown

```
$0.0471 | 3.6s | ↑1523 ↓42 | claude-sonnet-4-6
🔧 8 (Read×4 Bash×3 Grep×1)
```

In pty mode (claude backend only), the footer can additionally include `ctx N%` (context-window usage) and `5h N% → HH:MM` (5-hour rate-limit budget with reset time). Tool/skill counts are not surfaced in pty mode.
