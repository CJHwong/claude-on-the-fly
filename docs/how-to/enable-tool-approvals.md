# Enable tool approvals

Tool approvals pause selected calls and ask in the conversation that caused them.

## Prerequisites

- Slack installations need an `xoxb-` bot token for interactive buttons.
- Telegram prompts are authorized against `telegram.allowed_user_id`.
- Claude pty needs tmux; the script fallback cannot be driven safely.

## Configure

```yaml
permissions:
  mode: ask
  claude_mode: default
  ttl_seconds: 1800
  timeout_seconds: 300
```

Restart the chat daemon after changing `mode`. Timing changes apply to subsequent
requests without replacing a session.

Claude native and pty forward Claude's own questions. Codex has no answerable human
approval channel under `codex exec`, so cotf classifies calls before they run. See the
[approval model](../explanation/tool-approvals.md) before comparing prompt volume.

`claude_mode: auto` delegates decisions to another model. Use it to reduce prompts,
not as protection. Cron and background jobs are always ungated.

## Test

Ask for a harmless inspection, then a call the selected backend normally questions.
Confirm the approval appears in the originating conversation and that denial prevents
the call. Check logs for `permissions: approvals ON` and the per-session endpoint.
