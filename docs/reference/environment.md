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
