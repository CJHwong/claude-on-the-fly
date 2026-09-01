# `config.yaml` reference

Path: `~/.claude-on-the-fly/config.yaml`. Values shown as “unset” use the code default.
Lifecycle terms are defined in [Configuration files and lifecycle](settings.md).

## `sandbox`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `mode` | string / `off` | `off`, `env`, `jail`; an unrecognised value refuses to start, an absent or empty one means `off` | Restart required |
| `fs` | string / allow reads | `deny-most` narrows home-directory reads; jail only | Next turn |
| `extra_paths` | list / empty | Up to three read grants for `deny-most`; an entry that resolves to the home directory, an ancestor of it, or a credential store or file the profile denies is logged at ERROR and dropped | Next turn |
| `broker_only_loopback` | boolean / false | Limit jail loopback to published broker/proxy ports | Next turn |
| `scope_sessions` | boolean / false | Give each chat thread its own agent session store; jail only | Next turn |

In `jail` mode, the agent may read the global Claude configuration needed to start but
cannot persist turn-created global `~/.claude` settings, hooks, or plugins. The
workspace-created `CLAUDE.md`/`AGENTS.md` persona symlinks remain intentional and are
still loaded by the backend CLIs; this read-side policy is about untrusted handoff and
configuration writes, not those persona links.

## `agent`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `backend` | string / `claude` | `claude` or `codex` | Next turn |
| `claude.mode` | string / `native` | `native`, `pty`, or `ollama` | Next turn |
| `claude.model` | string / unset | Passed to Claude in native/pty mode | Next turn |
| `claude.effort` | string / unset | `low`, `medium`, `high`, `xhigh`, `max`; native mode only, and unset lets the CLI read its own `effortLevel`; an unaccepted level is ignored with a warning | Next turn |
| `codex.mode` | string / `native` | `native` or `ollama` | Next turn |
| `codex.model` | string / unset | Passed to Codex native mode | Next turn |
| `codex.effort` | string / unset | `minimal`, `low`, `medium`, `high`, `xhigh`; native mode only, and unset lets codex read its own `model_reasoning_effort`; an unaccepted level is ignored with a warning | Next turn |
| `ollama.model` | string / unset | Required when either backend mode is `ollama` | Next turn |
| `ollama.effort` | string / unset | Effort for the ollama-served model, whichever backend runs under it; wins over the backend's own `effort` key | Next turn |
| `ollama.context_window` | integer / unset | Window that `ctx N%` and `auto_compact_pct` measure against in ollama mode; unset reports no reading | Next turn |
| `auto_compact_pct` | integer / unset | `1..100`; invalid disables automatic compaction | Immediate |
| `skills_cache_ttl_seconds` | number / `3600` | `<=0` probes on every query; invalid uses default | Immediate |
| `pricing_ttl_seconds` | integer / `604800` | `0` always refreshes; negative never expires | Immediate |
| `pane` | boolean / true | Host each turn in its own tmux server so the dashboard's watch pane mirrors the agent's terminal; `false`, `0`, `no` or `off` switches it off, anything else leaves it on. Inert without tmux | Next turn |
| `pty.auto_install` | boolean / false | Install missing claude-pty without prompting | Startup |
| `pty.auto_refresh` | boolean / true | Re-splice incomplete pty hooks | Startup |

With `pane` off, every turn runs as a plain child: the watch pane falls back to
tailing the session transcript, and a codex turn runs `codex exec` rather than its
interactive UI. Native `claude -p` is never hosted either way, because its pane would
show stream JSON.

Automatic compaction requires a reliable context-window reading. Native and pty
derive one. Ollama cannot, so it is inert there until `ollama.context_window`
supplies one; manual `$compact` remains available either way.

## `watchdog`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `stale_seconds` | number / `90` | How long a live process may go without a heartbeat before `claude-watchdog` restarts it; non-numeric, zero, or negative warns and uses the default | Immediate |

Read per run by a one-shot process, so an edit applies on the next scheduler
tick with nothing to restart.

The threshold is deliberately higher than the dashboard's 15 second staleness
window. That one decides a colour, where being early is free; this one decides a
restart, which costs every turn in flight, and the heartbeat coroutine can be
briefly starved by a poll cadence or a slow tracker call.

`claude-watchdog` acts only on a daemon whose process is alive and whose
heartbeat has gone stale. It never starts a stopped daemon: nothing on disk
separates a crash from a deliberate stop or from the middle of an upgrade. See
[Recover a wedged daemon](../how-to/recover-a-wedged-daemon.md).

## `interim`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `progress` | boolean / false | Forward the agent's mid-turn narration into the thread as it is produced | Immediate |
| `warmup_seconds` | number / `300` | Silence before a turn's first progress message; `0` posts from the first line; negative or invalid uses default | Immediate |
| `min_gap_seconds` | number / `300` | Shortest gap between two progress messages; `0` posts every line as it arrives; negative or invalid uses default | Immediate |

Interim progress needs a line-by-line stream, so it is inert under `agent.claude.mode:
pty` and on a frontend that does not implement progress delivery (today, Telegram).
Claude native/ollama and Codex native/ollama provide the required stream. It posts only
in DMs and group DMs, is paced by `warmup_seconds` and `min_gap_seconds` so a short turn
produces nothing and a long one a periodic digest, and it does not count against
`slack.reply_soft_limit`.

## `egress`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allow` | list of hostnames / packaged list | Tunnel without asking | Immediate, next CONNECT |
| `private_allow` | list of hostnames / empty | Permit explicitly listed names to resolve to private or loopback addresses | Immediate, next CONNECT |
| `never_ask` | list of hostnames / packaged list | Refuse without offering approval | Immediate, next CONNECT |

Operator lists are unioned with packaged lists. They cannot subtract packaged model
hosts or metadata denials. A malformed operator list falls back to packaged entries.
`allow` alone never bypasses public-address validation; use `private_allow` only for a
known local service. Every hostname is resolved and pinned before tunnelling. An allowed
host is a covert channel: TLS payloads are not inspected.

## `commands`

`tools` is a list merged over packaged tools by `name`.

| Tool field | Type / default | Meaning |
|---|---|---|
| `name` | non-empty string / required | Executable shimmed onto the agent PATH |
| `readback` | list of command prefixes / empty | Commands refused because they reveal or change credentials |
| `readback_flags` | list of strings / empty | Flags that make any command reveal a credential |
| `allow` | list of leading subcommand prefixes / empty | Positive command allowlist; empty or absent denies every invocation |
| `env_passthrough` | list of names / empty | Additional daemon variables forwarded to the real CLI |

The whole section requires a chat-daemon restart. Removing a packaged refusal is
allowed but logged as a warning. Arguments and flags after an allowed prefix are passed
through, so the broker is not a full CLI-semantics parser; scope the credential itself.
Operator overrides replace packaged entries by name. They do not inherit the packaged
`allow` list, so an override that omits it disables that tool.

## `permissions`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `mode` | string / `off` | `off` or `ask` | Restart required |
| `claude_mode` | string / `default` | `default`, `acceptEdits`, `manual`, `auto` | Next turn |
| `ttl_seconds` | positive number / `1800` | Lifetime assigned to new grants | Next grant |
| `timeout_seconds` | positive number / `300` | Operator answer window for subsequent requests | Next turn |

`bypassPermissions` and `dontAsk` are rejected under `ask`. `auto` delegates decisions
to a model and is not a security boundary. Cron and background jobs remain ungated.

## `slack`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allowed_senders` | list / token identity | Humans or explicitly listed bot IDs allowed to trigger | Immediate |
| `blocked_senders` | list / empty | Deny list; wins over allowed | Immediate |
| `silent_senders` | list / empty | Trigger without posting a reply | Immediate |
| `bot_policies` | mapping / empty | Per-allowed-bot pre-agent gate; see below | Immediate |
| `stats` | string / `summary` | `off`, `summary`, `detailed`; invalid uses summary | Immediate |
| `slash_command` | string / unset | Bot-token Slack command | Restart required |
| `job_command` | string / `$job` | Prefix for background work; empty disables | Restart Slack |
| `alert_target` | string / unset | Slack channel or DM id for cron/job failure alerts; unset disables alerts | Restart jobs and cron |
| `session_cap` | positive integer / `1000` | Retained thread sessions; invalid uses default | Immediate |
| `reply_soft_limit` | positive integer / `10` | Replies before `$continue` is required | Immediate |
| `reply_limit_notice_seconds` | non-negative number / `0` | Seconds the `$continue` notice is held before posting, so it lands unread instead of being read on arrival by a sender who is about to leave; `0` posts it immediately | Immediate |
| `mention_notice_seconds` | non-negative number / `0` | Seconds an untagged channel message waits before the bot tells the sender, in an ephemeral message only they see, that it only sees messages tagging it; once per person per thread, and only for someone who has tagged the bot there; `0` disables the notice and records no per-thread state for it | Immediate |
| `personas` | mapping / empty | Per-chat instructions file, replacing the data-root `CLAUDE.md`; keys are channel id, channel name, sender id, `dm`, or `default` | Immediate |

Persona values are paths relative to the data root and must resolve inside it. A value
that escapes it, does not exist, or is not a string is refused with an ERROR naming the
key, and that chat falls through to its next key, then `default`, then the data-root
`CLAUDE.md`. `telegram` and `jobs` take a `personas` mapping too, keyed by chat id and
job key. See [Persona](persona.md) for the full key order.

Use `allowed_senders: ["*"]` to allow every human sender. The quotes are required:
a bare `*` starts a YAML alias and makes the configuration invalid.

`*` allows any human but never implicitly allows bots. With a bot token, list your own
human Slack ID or your DMs are ignored.

`bot_policies` is keyed by an allowed bot's `B…` id. Supported modes are `all`
(every message reaches the agent), `selective` (`process_if` decides), and `drop`
(nothing does).

`process_if` and `drop_before_ai` are ordered lists of your own patterns, because
which of a bot's messages deserve an agent turn is specific to that bot and that
workspace. Each entry is a mapping with a `name` and a `match`, where `match` is a
regular expression applied to the message text with `re.IGNORECASE` and no other
flag. `DOTALL` is deliberately off, so `.` does not cross newlines in a message
flattened from blocks and attachments; write `(?s)` in your own pattern if you want
it. The first matching rule wins and its `name` becomes the audit reason.

`explicitly_mentions_agent` is the one bare string allowed, and only in
`process_if`. It is a structural test rather than a text match, so there is no
pattern an operator could write for it.

Under `selective` an event matching nothing is dropped. `drop_before_ai` does not
decide that. It labels it: a matched rule replaces `unmatched` in the audit line so
a known routine event is distinguishable from a message no rule describes.

When `audit_dropped_events` is true (the default), the normal Slack log records only
the bot id, channel id, event timestamp, and decision reason — not the message text.
It defaults on because this gate discards work before the agent sees it, and a
pattern that does not match what its author expected should be a log line rather
than silence. An invalid policy logs an error once per configuration and preserves
the existing trusted-bot behavior instead of silently discarding work.
`silent_senders` remains independent: it suppresses the reply only after an allowed
event has run.

## `telegram`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allowed_user_id` | integer / required | Sole authorized user and fallback approval DM | Immediate |
| `stats` | string / `summary` | `off`, `summary`, or `detailed` | Immediate |
| `alert_target` | integer / unset | Telegram chat id for cron/job failure alerts; unset disables alerts | Restart jobs and cron |
| `personas` | mapping / empty | Per-chat instructions file, keyed by chat id or `default` | Immediate |

Changing `allowed_user_id` immediately revokes the previous ID for messages and approval
taps. An invalid edit retains the last valid startup ID and logs an error.

## `suggestions`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `enabled` | boolean / `false` | Ask the agent to end each chat reply with follow-up buttons instead of the static shortcut list | Next turn |

While enabled, the agent's own follow-up questions render as buttons and the static
shortcut list is suppressed; a reply without a suggestions block shows no buttons at all.

## `jobs`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `queue_kind` | string / `file` | Queue adapter shared by producer and worker | Restart Slack and jobs |
| `concurrency` | integer / `1` | Concurrent agent processes; below 1 uses 1 | Restart jobs |
| `poll_interval_s` | number / `2.0` | Idle queue polling interval | Restart jobs |
| `timeout` | number / agent default | Per-job wall clock; `<=0` means unlimited | Restart jobs |
| `workspace_keep_days` | integer / `30` | Retention for finished one-shot job workspaces; `0` keeps them forever. A job with a session key keeps its workspace indefinitely | Restart jobs |
| `personas` | mapping / empty | Per-job instructions file, keyed by the job key or `default` | Immediate |

A timeout carried by a cron job overrides the worker default for that job.

## `logs`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `keep_days` | integer / `30` | Retention; `0` disables pruning, invalid uses default | Next prune/startup |
| `host_tag` | string / short hostname | Host component in log filenames | Startup and daily rollover |

## `upgrade`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `command` | string / derived from the install | Shell command `claude-tui upgrade` runs to fetch new code | Immediate |

Leave `command` unset and the command follows how the copy was installed: a git checkout
runs `git pull --ff-only && uv sync` from the repository root, and a `uv tool install`
runs `uv tool upgrade <tool>`. Any other shape refuses to guess and asks for this key.
See [Upgrade safely](../how-to/upgrade-safely.md).
