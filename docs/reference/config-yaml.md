# `config.yaml` reference

Path: `~/.claude-on-the-fly/config.yaml`. Values shown as “unset” use the code default.
Lifecycle terms are defined in [Configuration files and lifecycle](settings.md).

## `sandbox`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `mode` | string / `off` | `off`, `env`, `jail`; an unrecognised value refuses to start, an absent or empty one means `off` | Restart required |
| `fs` | string / allow reads | `deny-most` narrows home-directory reads; jail only | Next turn |
| `extra_paths` | list / empty | Up to three read grants for `deny-most`; an entry that resolves to the home directory, an ancestor of it, or a credential store or file the profile denies is logged at ERROR and dropped | Next turn |
| `write_paths` | list / empty | Up to three write grants for `deny-most`, readable as well as writable. Refuses everything `extra_paths` refuses, plus entries reaching a shell rc, a git config, a systemd or autostart unit, a LaunchAgent, a directory on the daemon's PATH, or an agent config tree (`~/.claude`, `~/.codex`, `~/.agents`). A refused entry is logged at ERROR and dropped. The list catches common mistakes; it is not a substitute for granting the narrowest tree that works. Inside a grant, `.git/hooks`, `.git/config` and the file behind a linked `cron.yaml` or `config.yaml` stay read-only unless named as their own entry | Next turn |
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
| `claude.effort` | string / unset | `low`, `medium`, `high`, `xhigh`, `max`; applies in native **and** pty mode, and unset lets the CLI read its own `effortLevel`; an unaccepted level is ignored with a warning | Next turn |
| `codex.mode` | string / `native` | `native`, `pty`, or `ollama`; `pty` runs codex's interactive UI in a hosted pane, and degrades to `codex exec` with a log line when there is no pane | Next turn |
| `codex.model` | string / unset | Passed to Codex native mode | Next turn |
| `codex.effort` | string / unset | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; applies in native **and** pty mode, and unset lets codex read its own `model_reasoning_effort`; an unaccepted level is ignored with a warning | Next turn |
| `codex.chatgpt_via_broker` | boolean / `false` | Codex signed in with ChatGPT only. The credential broker holds `auth.json`, adds the token to each model call, and refreshes it; the jail hides the file from the turn. Starts the broker even with the sandbox off. Startup fails if `auth.json` holds no ChatGPT login. Not for `ollama` mode or an API key | Restart |
| `ollama.model` | string / unset | Required when either backend mode is `ollama` | Next turn |
| `ollama.effort` | string / unset | Effort for the ollama-served model, whichever backend runs under it; wins over the backend's own `effort` key | Next turn |
| `ollama.context_window` | integer / unset | Window that `ctx N%` and `auto_compact_pct` measure against in ollama mode; unset reports no reading | Next turn |
| `auto_compact_pct` | integer / unset | `1..100`; invalid disables automatic compaction | Immediate |
| `skills_cache_ttl_seconds` | number / `3600` | `<=0` probes on every query; invalid uses default | Immediate |
| `pricing_ttl_seconds` | integer / `604800` | `0` always refreshes; negative never expires | Immediate |
| `pane` | boolean / true | Host each turn in its own tmux server so the dashboard's live view mirrors the agent's terminal; `false`, `0`, `no` or `off` switches it off, anything else leaves it on. Inert without tmux | Next turn |
| `pty.auto_install` | boolean / false | Install missing claude-pty without prompting | Startup |
| `profiles` | mapping / unset | Named overrides of the keys above, one per cron entry. Same nesting and same key names; an omitted key inherits the global value. Validated at daemon start | Next turn |
| `pty.auto_refresh` | boolean / true | Re-splice incomplete pty hooks | Startup |

`pane` and the per-backend `mode` answer different questions. `mode` picks what runs
— `claude.mode: pty` for claude's TUI, `codex.mode: pty` for codex's — and `pane`
picks whether it is mirrored. Keeping them apart means a break in one backend's
interactive path costs that backend its UI rather than taking the other's mirror
away with it.

With `pane` off, every turn runs as a plain child and the live view falls back to
tailing the session transcript. Native `claude -p` and `codex.mode: native` are never
worth hosting either way: their panes would show stream JSON and plain lines.

Automatic compaction requires a reliable context-window reading. Native and pty
derive one. Ollama cannot, so it is inert there until `ollama.context_window`
supplies one; manual `$compact` remains available either way.

### Profiles

A profile answers the same questions the block above does, for one cron entry at a
time. A `cron.yaml` entry names one with `profile: cheap`; an entry that names none
runs on the settings above, unchanged.

```yaml
agent:
  backend: codex
  codex:
    mode: native
    model: gpt-5
    effort: high

  profiles:
    cheap:
      codex:
        model: gpt-5-mini
        effort: low
    local:
      codex:
        mode: ollama
      ollama:
        model: qwen3:30b
```

Three rules decide what a profile does.

1. **Anything it leaves out is inherited.** `cheap` above changes the model and the
   effort and keeps everything else, `local` changes the mode and lets the `ollama`
   block supply the rest.
2. **A profile beats the matching environment variable.** Everywhere else in this
   file the environment wins, so a deployment cannot lose a daemon-wide default to a
   file nobody edited. A profile is neither daemon-wide nor a default.
3. **It overlays the same key space, so it is inert in the wrong place.** A `codex:`
   block under a claude backend does nothing, exactly as the global `agent.codex.model`
   does nothing there. Set `backend` in the profile too, or move the override under
   the backend that is running.

Every profile is resolved and its CLI checked when the daemon starts, so an
uninstalled backend or a mode typo refuses to start rather than failing on the first
fire of whichever entry names it. Profiles that differ only by model share one check.

The model is part of the session identity, so a keyed entry whose profile changes its
model starts a fresh transcript rather than resuming one that model never wrote.
Effort is not, so raising it leaves the transcript alone.

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
| `progress` | boolean / false | Show the agent's mid-turn narration in one progress message in the thread | Immediate |
| `warmup_seconds` | number / `300` | Silence before a turn's progress message first appears; `0` shows it from the first line; negative or invalid uses default | Immediate |
| `min_gap_seconds` | number / `300` | Shortest gap between two updates of the progress message; `0` updates it on every line; negative or invalid uses default | Immediate |

Interim progress needs a line-by-line stream, so it is inert under `agent.claude.mode:
pty` and on a frontend that does not implement progress delivery (today, Telegram).
Claude native/ollama provides the required stream, and so does every Codex mode: Codex narrates from its rollout, which a hosted pane does not change. It posts only
in DMs and group DMs, is paced by `warmup_seconds` and `min_gap_seconds` so a short turn
produces nothing and a long one a periodic update, and it does not count against
`slack.reply_soft_limit`.

A turn normally has one progress message, and each update appends to it, so the
message grows into a timeline of the turn. Each update adds how long the turn has run,
then the agent's newest 3 lines of that gap, each cut to 200 characters. When the
message can hold no more (Slack caps a progress block at 2,900 characters and the
message text at 3,800 bytes, and CJK reaches the byte cap first) the update starts a
new message carrying only itself, and later updates append to that one. A failed edit
does the same. Progress messages stay in the thread after the turn ends, whether it
answered, failed, timed out, or was stopped with `$stop`: the reply says what the agent
concluded, and these say how it got there. The next turn starts a new message and never
appends to an earlier turn's.

## `egress`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allow` | list of hostnames or IP addresses / packaged list | Tunnel without asking | Immediate, next CONNECT |
| `private_allow` | list of hostnames or IP addresses / empty | Permit explicitly listed names to resolve to private or loopback addresses | Immediate, next CONNECT |
| `never_ask` | list of hostnames or IP addresses / packaged list | Refuse without offering approval | Immediate, next CONNECT |

Operator lists are unioned with packaged lists. They cannot subtract packaged model
hosts or metadata denials. An entry that is not a hostname or an IP address is dropped
with an error naming it, and the rest of the list applies. A value that is not a list
falls back to packaged entries. Link-local addresses stay refused even in `private_allow`.
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
| `allow` | list of leading subcommand prefixes / empty | Positive command allowlist; empty or absent denies every invocation except a help request (`<words> --help`, `-h` or `--version` last with no earlier flag, or a leading `help`) |
| `allow_read_only` | list of leading subcommand prefixes / empty | Prefixes admitted only when the invocation reads |
| `allow_paths` | list of paths / empty | Trees outside the workspace that absolute path arguments may reach |
| `boolean_flags` | list of strings / empty | Flags that never take the next token as a value when the subcommand is read |
| `value_flags` | list of strings / empty | Flags that always take the next token as a value when the allowlist reads the subcommand; the readback check ignores it. A flag cannot be in both lists |
| `env_passthrough` | list of names / empty | Additional daemon variables forwarded to the real CLI |

The whole section requires a chat-daemon restart. Removing a packaged refusal is
allowed but logged as a warning. Arguments and flags after an allowed prefix are passed
through, so the broker is not a full CLI-semantics parser; scope the credential itself.
A flag before the subcommand is refused unless it is listed in `boolean_flags` or
`value_flags`, and the refusal tells the agent to move it after the subcommand.
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
| `reply_soft_limit` | positive integer / `10` | Turns admitted per thread before `$continue` is required; a turn counts when it reaches the agent, so messages queued behind a running one are counted too | Immediate |
| `reply_limit_notice_seconds` | non-negative number / `0` | Seconds the `$continue` notice is held before posting, so it lands after the sender's burst rather than between two of their messages; `0` posts it immediately | Immediate |
| `mention_notice_seconds` | non-negative number / `0` | Seconds an untagged channel message waits before the bot tells the sender, in an ephemeral message only they see, that it only sees messages tagging it; one notice per burst of untagged messages, once the sender has been quiet this long, with no limit on repeats (a sender who keeps chatting untagged after tagging gets one after each pause), and only for someone who has tagged the bot there; the notice has a button that runs the sender's untagged messages since their last tag, oldest first, as if they had tagged; `0` disables the notice and records no per-thread state for it | Immediate |
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
| `diagnose_failures` | boolean / `false` | Experimental. Appends deterministic signals read from the run's own transcript to a failed job's alert. codex and claude; another backend alerts exactly as before | Live |
| `personas` | mapping / empty | Per-job instructions file, keyed by the job key or `default` | Immediate |

A timeout carried by a cron job overrides the worker default for that job.

## `logs`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `keep_days` | integer / `30` | Retention; `0` disables pruning, invalid uses default | Next prune/startup |
| `host_tag` | string / short hostname | Host component in log filenames | Startup and daily rollover |

## `daemons`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `restart_command` | string / unset | Command that starts a frontend, when a service manager owns it | Immediate |

Leave it unset and the supervisor starts each daemon itself, as a child of whatever ran
the command. That is right for a deployment you drive with `claude-tui start` and a
terminal.

Set it when a service manager owns the daemons, which is the usual shape on a server.
Stopping works either way, because systemd and launchd leave a deliberately stopped
service alone. Starting does not: without this, `claude-tui start`, `claude-tui restart`
and the dashboard's upgrade leave the manager's unit inactive while an unsupervised copy
serves traffic.

`{frontend}` is replaced with the frontend name, so one template covers every unit. The
command runs through a shell, and the supervisor then waits for the daemon's heartbeat,
so a command that prints nothing is fine.

```yaml
daemons:
  restart_command: systemctl --user restart cotf-{frontend}
```

## `upgrade`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `command` | string / derived from the install | Shell command `claude-tui upgrade` runs to activate new code | Immediate |
| `prepare_command` | string / derived from the install | Shell command that runs first, with the daemons still serving | Immediate |

An upgrade is up to two commands, split by whether the daemons have to be down for them.
`prepare_command` runs first, while every daemon still serves, so it is where the network
round trip belongs. `command` replaces the code the daemons are running, so they stop for
it and start again afterwards.

Leave both unset and they follow how the copy was installed. A git checkout gets
`git fetch` then `git merge --ff-only && uv sync`, from the repository root. A
`uv tool install` gets `uv tool upgrade <tool>` in one step, because uv builds the
replacement environment itself. Any other shape refuses to guess and asks for `command`.

Set `prepare_command` only when the split is safe for your command. A daemon still serving
reads whatever the prepare step changes, so a fetch is fine and a checkout is not. Setting
`command` alone keeps the single-step behaviour: an opaque command is not split for you.

See [Upgrade safely](../how-to/upgrade-safely.md).
