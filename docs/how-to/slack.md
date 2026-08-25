# Slack

## Setup (5 minutes)

### Create the app

Generate a manifest for your install. It asks three questions (token kind, app
name, slash command) and explains each. No clone needed:

```bash
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack --manifest
```

From a checkout it's `uv run claude-slack --manifest`.

Non-interactively, for scripts and headless installs, pass the answers as flags
and it prints the manifest to stdout instead of asking:

```bash
uvx --from git+https://github.com/CJHwong/claude-on-the-fly claude-slack \
  --manifest --mode bot --name "COF (yourname)" --command /cof-yourname \
  > slack_manifest.json
```

`--mode` is required in that form. `--command` is optional: leave it out and no
slash command is declared.

1. Go to https://api.slack.com/apps
2. Click "Create New App" -> "From a manifest"
3. Select your workspace
4. Switch to JSON tab, paste the generated manifest
5. Click "Create"

Why it's generated rather than a file you copy: a bot install and a user install
need different scope blocks, and the slash command has to be unique in the
workspace. Slack doesn't namespace slash commands, so if two installed apps
declare the same one, the most recent install wins for everybody and the other
silently stops firing. The suggested command carries your login name for that
reason (`/cof-yourname`); answer `none` if you'd rather have no slash command.

### Get your tokens

6. Left sidebar -> "Socket Mode" -> Toggle ON
7. Left sidebar -> "Basic Information" -> scroll to "App-Level Tokens" -> Create a token (name: anything, scope: `connections:write`)
8. Copy the `xapp-...` token
9. Left sidebar -> "Install App" -> "Install to Workspace" -> Allow
10. Copy one bearer token into `SLACK_TOKEN` — the kind is inferred from the prefix:
    - **User OAuth Token** (`xoxp-...`) — Claude replies as you, sees every channel you can.
    - **Bot User OAuth Token** (`xoxb-...`) — Claude replies as the app. The bot must be invited to each channel, but DMs work out of the box. Pick this for a normal Slack bot.

### Run

Tokens go in `~/.claude-on-the-fly/.env`:

```bash
SLACK_APP_TOKEN=xapp-...
SLACK_TOKEN=xoxb-...   # xoxp- to reply as you, xoxb- to reply as the app
```

Everything else goes in `~/.claude-on-the-fly/config.yaml`, which is seeded with a
commented template on first start. All of it is optional:

```yaml
slack:
  # Senders allowed to trigger Claude. Users (U…/W…) and bots (B…) share one list;
  # "*" allows any human. The token's own id is always allowed.
  allowed_senders: [U111, U222, B07JPABE2]
  # Denied senders, users or bots. Wins over the allowlist.
  blocked_senders: [U999]
  # Senders that trigger Claude but get no reply posted back.
  silent_senders: [B07JPABE2]
  # Optional pre-agent gate for an allowed automation bot. The patterns are
  # yours: write the ones that describe your bot's messages.
  bot_policies:
    B07JPABE2:
      mode: selective
      process_if:
        - explicitly_mentions_agent
        - name: ticket_activity
          match: 'new (ticket )?(comment|reply|mention)'
        - name: support_escalation
          match: 'support escalation|priority: ?urgent|severity: ?s[12]'
      drop_before_ai:
        - name: discovery_booked
          match: 'discovery booked'
        - name: payment_notification
          match: 'payment received|overdue invoice'
      audit_dropped_events: true
  # Bot token only: the slash command you declared in the manifest. Must match it
  # exactly. Unset means no slash command -- open the skill picker from a message's
  # "..." menu instead. Registered at startup, so a change needs a restart.
  slash_command: /cof-yourname
  # Background jobs are on by default under `$job`, run by the `claude-jobs` worker
  # in a fresh session, which replies in the same thread when it finishes. Sent on
  # its own it lists what this channel already has queued. Needs claude-jobs
  # running. Set this to rename the trigger, or set it empty ("") to turn
  # background jobs off entirely.
  job_command: "$job"
```

Then:

```bash
uv run claude-slack
```

The sender lists are read on every message, so adding someone takes effect on their
next one without a restart.

## How it works

- With a **user token**, anyone who DMs you triggers Claude (if they can DM you, they're trusted), and your own ID is always allowed
- With a **bot token**, the auto-allowed ID is the bot's, not yours — add your own `U…` id to `slack.allowed_senders` (or use `"*"`; the quotes are required by YAML) or your DMs will be dropped
- In channels, only allowed senders (auto-allowed ID + `slack.allowed_senders`) can trigger Claude via @mention
- Bot posts (HubSpot, Jira, etc.) are ignored unless their bot ID (`B…`) is in `slack.allowed_senders`; trusted bot posts trigger Claude without an @mention. Don't list this app's own bot ID (it would loop)
- Senders listed in `slack.silent_senders` still trigger Claude, but their reply is not posted back — useful for alert/automation bots you want handled quietly
- A `slack.bot_policies` entry can narrow an allowed bot before agent dispatch. Under `selective`, a message reaches the agent only if it mentions the agent or matches one of your `process_if` patterns; everything else is dropped without consuming an agent turn
- Claude responds in a thread — as you with a user token, or as the app with a bot token
- Each thread = one Claude session with memory
- The app must be invited to private channels (`/invite @your-app-name`)

See the [`slack` reference](../reference/config-yaml.md#slack) for every field.
