# `config.yaml` reference

Path: `~/.claude-on-the-fly/config.yaml`. Values shown as “unset” use the code default.
Lifecycle terms are defined in [Configuration files and lifecycle](settings.md).

## `sandbox`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `mode` | string / `off` | `off`, `env`, `jail`; invalid values log and resolve to `off` | Restart required |
| `fs` | string / allow reads | `deny-most` narrows home-directory reads; jail only | Next turn |
| `extra_paths` | list / empty | Up to three read grants for `deny-most` | Next turn |
| `broker_only_loopback` | boolean / false | Limit jail loopback to published broker/proxy ports | Next turn |

## `agent`

| Key | Type / default | Values and effect | Lifecycle |
|---|---|---|---|
| `backend` | string / `claude` | `claude` or `codex` | Next turn |
| `claude.mode` | string / `native` | `native`, `pty`, or `ollama` | Next turn |
| `claude.model` | string / unset | Passed to Claude in native/pty mode | Next turn |
| `codex.mode` | string / `native` | `native` or `ollama` | Next turn |
| `codex.model` | string / unset | Passed to Codex native mode | Next turn |
| `ollama.model` | string / unset | Required when either backend mode is `ollama` | Next turn |
| `auto_compact_pct` | integer / unset | `1..100`; invalid disables automatic compaction | Immediate |
| `skills_cache_ttl_seconds` | number / `3600` | `<=0` probes on every query; invalid uses default | Immediate |
| `pricing_ttl_seconds` | integer / `604800` | `0` always refreshes; negative never expires | Immediate |
| `pty.auto_install` | boolean / false | Install missing claude-pty without prompting | Startup |
| `pty.auto_refresh` | boolean / true | Re-splice incomplete pty hooks | Startup |

Automatic compaction requires a reliable context-window reading. It is inert for
Claude routed through Ollama; manual `$compact` remains available.

## `egress`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allow` | list of hostnames / packaged list | Tunnel without asking | Immediate, next CONNECT |
| `never_ask` | list of hostnames / packaged list | Refuse without offering approval | Immediate, next CONNECT |

Operator lists are unioned with packaged lists. They cannot subtract packaged model
hosts or metadata denials. A malformed operator list falls back to packaged entries.
An allowed host is a covert channel: TLS payloads are not inspected.

## `commands`

`tools` is a list merged over packaged tools by `name`.

| Tool field | Type / default | Meaning |
|---|---|---|
| `name` | non-empty string / required | Executable shimmed onto the agent PATH |
| `readback` | list of command prefixes / empty | Commands refused because they reveal or change credentials |
| `readback_flags` | list of strings / empty | Flags that make any command reveal a credential |
| `env_passthrough` | list of names / empty | Additional daemon variables forwarded to the real CLI |

The whole section requires a chat-daemon restart. Removing a packaged refusal is
allowed but logged as a warning. The broker does not try to authorize arbitrary CLI
semantics; scope the credential itself.

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
| `stats` | string / `summary` | `off`, `summary`, `detailed`; invalid uses summary | Immediate |
| `slash_command` | string / unset | Bot-token Slack command | Restart required |
| `job_command` | string / `$job` | Prefix for background work; empty disables | Restart Slack |
| `session_cap` | positive integer / `1000` | Retained thread sessions; invalid uses default | Immediate |
| `reply_soft_limit` | positive integer / `10` | Replies before `$continue` is required | Immediate |

Use `allowed_senders: ["*"]` to allow every human sender. The quotes are required:
a bare `*` starts a YAML alias and makes the configuration invalid.

`*` allows any human but never implicitly allows bots. With a bot token, list your own
human Slack ID or your DMs are ignored.

## `telegram`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `allowed_user_id` | integer / required | Sole authorized user and fallback approval DM | Immediate |
| `stats` | string / `summary` | `off`, `summary`, or `detailed` | Immediate |

Changing `allowed_user_id` immediately revokes the previous ID for messages and approval
taps. An invalid edit retains the last valid startup ID and logs an error.

## `jobs`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `queue_kind` | string / `file` | Queue adapter shared by producer and worker | Restart Slack and jobs |
| `concurrency` | integer / `1` | Concurrent agent processes; below 1 uses 1 | Restart jobs |
| `poll_interval_s` | number / `2.0` | Idle queue polling interval | Restart jobs |
| `timeout` | number / agent default | Per-job wall clock; `<=0` means unlimited | Restart jobs |

A timeout carried by a cron job overrides the worker default for that job.

## `logs`

| Key | Type / default | Effect | Lifecycle |
|---|---|---|---|
| `keep_days` | integer / `7` | Retention; `0` disables pruning, invalid uses default | Next prune/startup |
| `host_tag` | string / short hostname | Host component in log filenames | Startup and daily rollover |
