# Environment variables

Credentials belong in `~/.claude-on-the-fly/.env`. File permissions should restrict it
to the operator account. Restart affected daemons after changing it.

| Variable | Required by | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram | BotFather token |
| `SLACK_APP_TOKEN` | Slack | `xapp-` Socket Mode token |
| `SLACK_TOKEN` | Slack, jobs fallback | `xoxb-` bot or `xoxp-` user token |
| `JOBS_SLACK_TOKEN` | Optional jobs override | Token used to post job results |
| `LOG_LEVEL` | All daemons | `DEBUG`, `INFO`, `WARNING`, or `ERROR`; default `INFO` |
| `COTF_DATA_DIR` | All daemons | Data directory instead of `~/.claude-on-the-fly`; must be set in the real environment, see below |

### Multiple daemons on one machine

Every daemon serves one data directory: its `config.yaml`, `.env`, `cron.yaml`,
logs, workspaces, memory, and state (heartbeat and pid files) all hang off
`~/.claude-on-the-fly`. Setting `COTF_DATA_DIR` before launching moves all of
that to another directory, which is how a second daemon gets its own config, its
own credentials, and its own heartbeat and pid files instead of fighting the
first for all three.

It has to be a real environment variable, not a setting in `config.yaml` or
`.env`: both files live inside the data directory, so a file in the directory
cannot point at the directory. An empty value behaves like an absent one.

What is shared across daemons either way: the agent CLIs' own configuration
(`~/.claude` / `CLAUDE_CONFIG_DIR`, `~/.codex` / `CODEX_HOME`) and the local
Ollama installation. Set `CLAUDE_CONFIG_DIR` or `CODEX_HOME` as well if the
second daemon needs those isolated too.

Use a bot token for `JOBS_SLACK_TOKEN`. If the worker inherits a user token, it posts
as that user and the Slack frontend can ingest the result as a new request.

Model API keys may be inherited directly when sandboxing is off. Under `sandbox.mode:
env` or `jail`, provision supported keys in the host keychain so the credential broker
can inject them upstream. For Claude on macOS:

```bash
security add-generic-password -a "$USER" -s cotf-anthropic -w "<key>" -U
```

See [authentication boundaries](../explanation/security-model.md#authentication-boundaries)
before relying on OAuth-based backends.

`DEBUG` logs include message text and vendor payloads. Treat them as sensitive.

Legacy names such as `SLACK_USER_TOKEN`, `SLACK_BOT_TOKEN`, and environment forms of
`config.yaml` settings remain compatibility inputs, not the current configuration API.
