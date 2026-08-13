# Security model

Claude-on-the-fly assumes model output and tool arguments may be attacker-influenced.
Its controls reduce available capability; they do not make an autonomous agent trusted.

## Three independent controls

1. **Sandboxing** decides what the agent process can read, write, and hold in its
   environment.
2. **Capability policy** pre-authorizes egress hosts and credentialed commands.
3. **Tool approvals** pause selected calls for an operator decision.

Approval prompts are the softest control: an attacker can shape the request and a human
can approve it. Metadata endpoints and credential readback are therefore refused rather
than offered for approval.

## Sandbox modes

`off` inherits the daemon environment. `env` removes credentials and routes supported
provider traffic through a loopback broker. `jail` adds filesystem, credential-store, and
network restrictions: Seatbelt on macOS, bubblewrap plus a network namespace on Linux.

`jail` does not degrade. If the mechanism is missing or unusable the daemon refuses to
start, and it proves at startup both that the jail runs and that a jailed process cannot
reach the internet directly.

The two implementations hold the same contract but not the same behaviour, and the
difference is worth knowing before reading a log:

| | macOS | Linux |
|---|---|---|
| Blocked read | `EPERM`, path still exists | `ENOENT`, path is absent from the namespace |
| Blocked write | `EPERM` | `EROFS` |
| Credential store | Keychain denied | No D-Bus session bus, so libsecret and friends are unreachable |
| Reachable host ports | Every loopback port by default | Only the brokered services, always |
| `sandbox.fs` | `allow-reads` or `deny-most` | `deny-most` only; a mount namespace cannot express a denylist over a global allow |

Under either setting Linux is at or above the macOS posture.

`SSH_AUTH_SOCK` is forwarded to the agent on both platforms and the socket behind it is
unreachable on both, so a jailed turn cannot sign as you. That is free on macOS, where
the profile permits no unix socket at all, and explicit on Linux, where the socket is a
real path a mount namespace would otherwise expose.

Parity between the two is enforced by `tests/test_sandbox_parity.py`, which runs one
contract against whichever real jail the host provides. Reading the two profiles
side by side is not a control; they have drifted before.

An approved HTTPS host remains a covert channel because the CONNECT proxy does not
intercept TLS. A brokered CLI runs outside the jail, so its provider-side token scope is
the real authorization boundary.

## One thread cannot read another's conversation

Under `jail`, a turn reaches only its own thread's transcripts. This matters more than
the credential denies beside it: a token is a single secret, while a transcript is the
message text itself, including what other people wrote in other threads.

Each chat thread already gets its own workspace, so the workspace is the boundary the
jail scopes to. The two backends need different mechanisms because they store sessions
differently.

| | Claude | Codex |
|---|---|---|
| Where sessions live | `<config dir>/projects/<workspace hash>/` | `$CODEX_HOME/sessions/<date>/rollout-*.jsonl` |
| Keyed by the workspace | Yes, so the path is known before the run | No, the name is chosen at startup |
| How the jail scopes it | Denies `projects/`, grants this thread's directory | Gives each workspace its own `CODEX_HOME` |

The agent CLI is itself the jailed process, so it must keep write access to the session
file it is currently writing. A blanket deny over the whole store looks safer and is not:
it stops the CLI persisting the session, the turn still completes, and nothing surfaces
until a later resume comes back with no memory of the conversation.

A codex home holds links back to the operator's `config.toml`, `AGENTS.md`, hooks, rules,
plugins, prompts and `auth.json`. Writes through those links resolve onto the shared paths
the profile already governs, so the execution and instruction surface stays read-only
while token refresh keeps working.

Deleting a thread's workspace removes both stores, because each is named after a path
that will never exist again.

The daemon is not jailed and reads every thread's store, which is what cross-backend
handoff needs. The isolation is enforced at the jail, not by narrowing the daemon.

## Authentication boundaries

The credential broker can protect a backend only when it can inject an API key header
and redirect the client through a base URL.

| Authentication | Broker coverage |
|---|---|
| Claude with Anthropic API key | Covered |
| Claude subscription OAuth | No injectable API key |
| Codex with OpenAI API key | Covered |
| Codex with ChatGPT login | Uses its own OAuth endpoints and token store |

Provider-side tools and browsing may execute outside the local process entirely. Local
sandbox and egress logs cannot observe or confine provider infrastructure.

## Known limits

Recorded because they are deliberate or structural, not because they are
acceptable everywhere. Each is a reason to scope provider-side permissions rather
than to trust the jail alone.

**The agent can see and signal your other processes, on both platforms.** The
seatbelt profile grants `process-info*` and `signal` globally, and the Linux jail
does not unshare the PID or IPC namespaces. Linux could close this cheaply and
deliberately does not yet; macOS cannot without revisiting the profile.

**The agent can read the credential it runs on.** `~/.claude/.credentials.json`
and `~/.codex/auth.json` must be readable or the backend cannot authenticate, so
a hijacked turn can exfiltrate that token through the already-approved model
host. Structural on both platforms.

**A brokered CLI is a capability grant.** It runs outside the jail with your real
credential and only its output crosses back, so the provider-side token scope is
the boundary, not the sandbox.

**An approved HTTPS host is a covert channel.** The CONNECT proxy does not
intercept TLS, so it gates the destination and nothing else.

**On Linux, `~/.codex` is writable with a named deny list.** Config, hooks,
standing instructions, rules and plugin loading are mounted read-only over it,
but a *new* execution-control file introduced by a future codex release would be
writable until it is added to that list. The inverse (freezing everything
unknown) was tried and broke session resume within one run, because codex creates
its own state there.

**On Linux, the jail materialises placeholder files in the workspace.** An absent
`.mcp.json` gets an inert `{}` so a jailed turn cannot create one. It is visible
in `git status`, which is the intended trade: the alternative is a real hole,
since MCP config decides what later runs load.

**`jail` does not support the background jobs daemon, on either platform.** That
process runs on its own and starts no credential broker and no egress proxy, so a
jailed job has nothing on loopback to reach and no route to the internet. macOS
denies the outbound connection and Linux has no route at all; the outcome is the
same. Linux appeared to work before it had a real jail, because the mode silently
degraded. Run the jobs daemon with `sandbox.mode: env` until it grows brokers of
its own; the jail logs a warning naming this whenever it spawns without a relay.

**The Linux jail has an availability failure mode macOS does not.** It needs
bubblewrap installed and unprivileged user namespaces permitted. Where they are
not, the daemon refuses to serve rather than running unsandboxed. That refusal
path is unit-tested but has never been exercised against a real
AppArmor-restricted host.

## Unattended work

Cron and background jobs do not use interactive tool approvals. Nobody is guaranteed to
be watching their conversation, so pausing every call would turn unattended work into
automatic failure. Limit those prompts, credentials, and provider permissions directly.
