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
provider traffic through a loopback broker. `jail` adds macOS Seatbelt restrictions for
filesystem, keychain, and network access; without `sandbox-exec`, it degrades to `env`.

An approved HTTPS host remains a covert channel because the CONNECT proxy does not
intercept TLS. A brokered CLI runs outside the jail, so its provider-side token scope is
the real authorization boundary.

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
Seatbelt and egress logs cannot observe or confine provider infrastructure.

## Unattended work

Cron and background jobs do not use interactive tool approvals. Nobody is guaranteed to
be watching their conversation, so pausing every call would turn unattended work into
automatic failure. Limit those prompts, credentials, and provider permissions directly.
