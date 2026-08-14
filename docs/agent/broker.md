# Broker, sandbox, and approval implementation

Contributor notes for the security subsystems. Operator-facing material lives in:

- [Security model](../explanation/security-model.md)
- [Tool approval model](../explanation/tool-approvals.md)
- [Enable sandboxing](../how-to/enable-sandboxing.md)
- [Manage egress](../how-to/manage-egress.md)
- [Broker a command](../how-to/broker-a-command.md)
- [`config.yaml` reference](../reference/config-yaml.md)

## Composition

`orchestrator.run` validates settings, initializes approval artifacts, and starts the
credential and command brokers when the startup sandbox mode is enabled. `SessionEgress`
and `SessionPermissions` allocate independent services and grant stores per chat session.
Their endpoint variables travel through `sandbox.session_env`, an asyncio `ContextVar`.

The credential broker is daemon-wide because provider routes are operator policy. Egress
and tool grants are per session because a CONNECT or tool request otherwise carries no
reliable chat identity.

## Credential broker

`broker.py` strips caller authentication, injects the keychain credential on the
upstream leg, and never forwards it across redirects. Routes can constrain methods and
path tails. Its loopback URLs are paired with a per-turn token bound to the originating
workspace, so discovering a port does not by itself authorize a request or let a turn
choose another cwd. `sandbox.agent_env` forwards provider base URLs but omits token-like
daemon variables.

## Egress proxy

`egress.py` handles HTTPS CONNECT without TLS interception. Packaged and operator host
lists are unioned. Private addresses, metadata names, invalid hosts, rate limits, and
operator denial fail before tunneling. Error causes must fit in the HTTP reason phrase;
common clients discard CONNECT bodies.

## Command broker

`commands.py` writes reserved PATH shims and relays argv, bounded stdin, and a narrow
environment to the real executable outside the jail. A tool's explicit `allow` prefixes
are a deny-by-default positive gate; absolute/escaping path arguments and cwd values
outside the turn's workspace are rejected before process creation; readback prefixes and
flags prevent credential material crossing back. Tool entries merge by name; removed
packaged refusals are warned. An operator override without `allow` therefore disables
that tool until its safe subcommands are listed.

The broker does not parse arbitrary CLI semantics after an allowed prefix. Generic API
subcommands remain unavailable unless explicitly listed, and provider-side credential
scope is still required because argv inspection cannot safely model every future flag.

## Tool approvals

`permissions.py` owns config parsing, request classification, per-session HTTP service,
Claude pty dialog parsing, and generated backend artifacts. `cotf_approve.py` is the
credential-free subprocess edge: MCP for Claude native, hook protocol for Codex, and
notification relay for pty.

Claude pty requires an addressable tmux pane. Dialog option numbers are parsed rather
than assumed, widen-scope choices are never selected, and an unreadable dialog is
cancelled or left unanswered rather than guessed.

Codex requires the hook trust bypass because inline hooks have no persisted trust entry.
Seatbelt therefore denies writes to Codex execution-control paths and re-grants only the
runtime paths measured as necessary.

## Per-thread session grants

`sandbox.scope_sessions` gates the whole boundary and is off by default
(`sandbox.scoped_sessions()`). Off, the granted path resolves back onto the store it
sits in: `_CLAUDE_PROJECT` becomes `_CLAUDE_PROJECTS` and `_CODEX_HOME` becomes the
shared codex home. SBPL is last-match-wins and the grant is written after the deny, so
each deny is nullified by its own re-grant and no profile line has to change. On Linux
the two masks are dropped instead, because a tmpfs and a read-write bind over the same
path would leave argv order deciding the policy.

Four profile parameters carry the session boundary, resolved per run from the workspace
and realpath'd like every other one:

| Parameter | Value | Rule |
|---|---|---|
| `_CLAUDE_CONFIG` | `envfile.claude_config_dir()` | write denied, then re-granted below |
| `_CLAUDE_PROJECTS` | `<config dir>/projects` | read denied |
| `_CLAUDE_PROJECT` | `…/projects/<workspace hash>` | read and write granted |
| `_CODEX_SESSIONS` | `<shared codex home>/sessions` | read denied |
| `_CODEX_HOME` | `DATA_DIR/codex-homes/<workspace key>` | read and write granted |

Every claude rule is written against `_CLAUDE_CONFIG` rather than `$HOME/.claude`,
because `CLAUDE_CONFIG_DIR` can move the tree outside `$HOME` where a `_HOME`-derived
rule matches nothing while the profile still loads.

## What a claude turn may write

`_CLAUDE_RUNTIME_WRITES` in `sandbox.py` is the re-grant list, and it is a
measurement: two real turns, one making a Bash tool call and one resuming with
`--continue`, diffing the config tree either side. The codex list above was built the
same way. Three buckets decide what goes in it:

| Bucket | Examples | Policy |
|---|---|---|
| Instruction-bearing | `settings.json`, `hooks.json`, `CLAUDE.md`, `commands/`, `skills/`, `agents/`, `plugins/` root | write denied; read on later invocations, so a write outlives the session |
| Conversation-bearing | `projects/<hash>/` (session JSONL **and** the per-project memory dir), `history.jsonl` | per-thread only, or denied outright |
| Runtime scratch | `shell-snapshots/`, `session-env/`, `sessions/`, `plugins/cache/`, `policy-limits.json` | granted machine-wide; none decides what the agent executes or is told |

`_CLAUDE_RUNTIME_WRITE_FILES` is split from `_CLAUDE_RUNTIME_WRITE_DIRS` because the
Linux wrap creates each mount source, and `mkdir` on a file target leaves a
*directory* called `policy-limits.json` that the CLI then cannot write. Same
distinction, and same reason, as `_CODEX_PROTECTED_DIRS`.

Claude Code's memory lives at `<config dir>/projects/<hash>/memory/`, inside the
per-thread grant, so it is isolated by the same rule as the transcript and needs no
grant of its own. Before the per-thread grant existed it was denied along with the
session file, so memory was silently off under `jail` too.

Both bases reference all four, unlike `_EXTRA_*`, so `jail_argv` always passes them. A
profile referencing an unpassed `-D` is refused outright, which is the failure worth
having: the alternative is a jail that loads with the session grant silently absent.

Three ordering constraints, all found by a live probe rather than by reading:

- On Linux, `_ensure_session_mount_sources` runs **before** `_linux_grants`. The grants
  decide whether to mask a session store by whether it exists, so creating the sources
  afterwards left the shared codex tree unmasked and then created it. Since Linux binds
  `~/.codex` read-write, a jailed turn could write a rollout straight into it: measured
  at rc 0 with the file landing on the host, while the whole suite stayed green.

- The codex pair must come *after* the `~/.codex` read allow. SBPL is last-match-wins, so
  placed before it the allow won and a jailed turn still read another thread's rollout
  while the profile looked correct.
- `codex_state.ensure_home` creates `sessions/` before the spawn. A recursive mkdir that
  cannot stat an ancestor walks up and tries to create it, which under an opaque `$HOME`
  fails at `/Users/<name>` on a path whose leaf was granted.

The claude grant is derived from `envfile.claude_config_dir()`, and `agent_env` states
`CLAUDE_CONFIG_DIR` on the child for the same reason: it is not a passthrough key, so a
deployment setting it in `DATA_DIR/.env` would leave the daemon and the CLI resolving
different stores, and the grant pointing at a directory the CLI never writes.

`CODEX_HOME` is set on the child by the codex backend rather than published as a session
override, because the jobs and cron daemons never open a session.

## Linux jail

`sandbox_linux.py` builds bubblewrap argv; `sandbox.py` owns which paths, beside the
Seatbelt profile selection. The mechanisms differ in ways that reach the rest of the
system, so they are worth stating rather than discovering:

- A denied read reports `ENOENT` and a denied write `EROFS`, where Seatbelt reports
  `EPERM` for both. `agent_guidance` therefore ships per-platform error text, and
  `_probe_deny` settles absent-versus-denied by stat'ing outside the jail first: a
  hidden path and a missing one are character-identical from inside.
- Mounts are ordered by path depth, not by argument order, so a read grant on a parent
  cannot re-expose an opaque child.
- A tmpfs is writable, so each opaque path is remounted read-only in a trailing pass.
  It has to be trailing: an early remount leaves bwrap unable to create the mount
  points beneath it.
- Write denies need something to mount over, so absent ones are materialised on the
  host first. The stand-in must parse as whatever reads it, which is why there is an
  empty-JSON placeholder as well as an empty file.
- `~/.codex` is writable with its execution-control entries mounted read-only back over
  it, inverting the Seatbelt posture. Seatbelt's sqlite regex matches files that do not
  exist yet; a mount namespace cannot, and codex creates `state_N.sqlite` on first run.

`netns_relay.py` bridges the brokered loopback ports into the namespace over unix
sockets, one per port, same port number on both ends so published URLs need no
rewriting. It fails closed: `--unshare-net` leaves no route anywhere, and only what the
relay bridges exists. A port mapper would fail open, which is why one is not used.

## Verification invariants

- Every failure path denies.
- Approval request count is checked against tool usage after each turn.
- Grants never cross sessions and die with their service.
- Sandbox mode and permission mode remain at startup values until restart.
- Shim transport timeout exceeds the broker answer window.
- `sandbox.verify_denials` probes credential paths because macOS exposes no useful
  Seatbelt denial audit stream.
- `sandbox.preflight` proves the jail starts and refuses external egress before the
  daemon serves. `verify_denials` alone cannot: it settles absent-versus-denied outside
  the jail, so on a machine with none of the probed credentials it spawns nothing.
- A missing or unusable mechanism is fatal on both platforms.

Tests for these invariants live in `test_sandbox.py`, `test_sandbox_linux.py`,
`test_netns_relay.py`, `test_approvals.py`, `test_permissions.py`, `test_cotf_approve.py`,
and `test_orchestrator.py`.

`test_sandbox_jail_live.py` is the only suite that runs the argv rather than asserting
about it, and so the only one that answers whether the kernel actually refuses. It skips
where bubblewrap or user namespaces are absent, and CI fails on a skip rather than
counting it green.
