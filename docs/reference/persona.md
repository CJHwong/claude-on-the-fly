# Persona (CLAUDE.md)

Customize Claude's identity and behavior by placing a `CLAUDE.md` at the data root:

```bash
~/.claude-on-the-fly/CLAUDE.md
```

This file is automatically symlinked into every workspace. Claude Code loads it as project instructions. Use it for:

- Bot identity and tone (e.g., "You are Avery, an AI assistant for the EPD team")
- Team directory references
- Custom behavioral rules

The symlink is re-created on every message, so even if removed mid-session it self-heals.

If no `CLAUDE.md` exists, Claude runs with the default system prompt only.

## Per-chat persona

One Slack channel, one Telegram chat, or one background job can run different
instructions than the rest. Write the files anywhere under the data root and name
them in a `personas:` mapping in that frontend's own `config.yaml` section.

A match **replaces** the data-root `CLAUDE.md` for that chat — the two do not
stack, so anything shared has to be repeated in each file. `default` catches every
chat the other keys do not name. With no match at all, the data-root `CLAUDE.md` is
used, which is the behavior of a deployment that configures none of this.

```yaml
slack:
  personas:
    C07ABCDEF: personas/oncall.md    # #ops-alerts
    U012ABCDEF: personas/private.md  # my own DMs
    dm: personas/guest.md            # everyone else's DMs
    default: personas/team.md        # every other channel

telegram:
  personas:
    "123456789": personas/private.md  # chat id

jobs:
  personas:
    daily-digest: personas/digest.md  # a cron entry name
    stale-prs/42: personas/nudge.md   # producer entry: <entry>/<item key>
    default: personas/jobs.md         # every unkeyed or unlisted job
```

Keys, tried in this order:

| Chat | Order |
| --- | --- |
| Slack channel | channel id, channel name, `default` |
| Slack DM or group DM | channel id, sender's user id, `dm`, `default` |
| Telegram | chat id (quote it — YAML would read a bare number as an integer), `default` |
| Background job | the job key, `default` |

For Slack, prefer the channel id: it survives a rename, and it is the `C…` in any
Slack link (`/archives/C07ABCDEF/p17…`). Since the id says nothing about which
channel it is, name the channel in a comment. A channel-name key also works and
stops matching, silently, the moment someone renames the channel.

Only cron sets a job key: the entry name for a scheduled prompt, or
`<entry>/<item key>` for a producer entry. It is matched exactly as the producer set
it, slashes included. A job filed by `$job` or `claude-jobs enqueue` has no key at
all and only ever matches `default`.

A Slack channel is never keyed on the sender, only on the channel. Its workspace is
per thread while its sender changes per message, so a sender key would flip the
persona mid-conversation.

Values are paths relative to the data root and must stay inside it. A value that
points outside, does not exist, or is not a string is refused: the daemon logs an
ERROR naming the key, and that chat falls through to its next key, then `default`,
then the data-root `CLAUDE.md`. A chat with no instructions at all is the worse
failure.

Everything here is read per message, so an edit lands on the next one — no restart,
and no need to start a new thread.

For codex backend compatibility, the file is also symlinked as `AGENTS.md` in each workspace.

These persona links are intentional. The sandbox's symlink rejection applies to
untrusted attachment/outbox handoffs and daemon state; it does not reject the
workspace links that COTF creates for Claude Code and Codex to load their instructions.
