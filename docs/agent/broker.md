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

## Verification invariants

- Every failure path denies.
- Approval request count is checked against tool usage after each turn.
- Grants never cross sessions and die with their service.
- Sandbox mode and permission mode remain at startup values until restart.
- Shim transport timeout exceeds the broker answer window.
- `sandbox.verify_denials` probes credential paths because macOS exposes no useful
  Seatbelt denial audit stream.

Tests for these invariants live in `test_sandbox.py`, `test_approvals.py`,
`test_permissions.py`, `test_cotf_approve.py`, and `test_orchestrator.py`.
