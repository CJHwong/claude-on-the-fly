# Build a safe Slack installation

This tutorial starts one Slack bot, removes model credentials from the agent process,
and makes selected tool calls wait for your approval.

## 1. Create the Slack app

Follow [Slack setup](../how-to/slack.md) through token creation. Use a bot token
(`xoxb-`): approval buttons do not work with a user-token installation.

Put the two Slack tokens in `~/.claude-on-the-fly/.env`:

```dotenv
SLACK_APP_TOKEN=xapp-...
SLACK_TOKEN=xoxb-...
```

## 2. Provision the model credential

Sandboxed agents reach providers through the credential broker. For Claude, store the
API key in the macOS keychain:

```bash
security add-generic-password -a "$USER" -s cotf-anthropic -w "<key>" -U
```

The broker supports injectable API-key authentication. OAuth limitations are listed in
the [security model](../explanation/security-model.md#authentication-boundaries).

## 3. Configure one operator and the safe modes

Start once to seed `~/.claude-on-the-fly/config.yaml`, then edit it:

```yaml
sandbox:
  mode: env

permissions:
  mode: ask
  claude_mode: default

slack:
  allowed_senders: [U_YOUR_SLACK_ID]
```

`env` removes credentials from the agent process on every platform. On macOS, switch
to `jail` after the first successful turn to add filesystem and network confinement.

## 4. Check the installation

```bash
uv run claude-tui
```

Press `d` for doctor. Fix blocking failures before starting Slack. Install tmux: with
Claude pty, approvals cannot drive the fallback script backend, and with any backend the
dashboard's watch pane shows the agent's live terminal instead of a transcript tail.

## 5. Start and verify

Start the Slack daemon, then ask the bot to run `git status`. Ask it next to run a
command Claude considers sensitive, such as `git init` in a temporary directory. The
second request should produce an approval card in the originating conversation.

Restart the daemon after changing `sandbox.mode` or `permissions.mode`. Other timing
and policy edits follow the lifecycle in the [settings reference](../reference/settings.md).

You now have three separate controls:

- the sandbox limits what the agent process can hold and reach;
- egress and command policy define pre-authorized capabilities;
- tool approvals pause selected calls for a human decision.

They complement one another; approval prompts are not a replacement for confinement.
