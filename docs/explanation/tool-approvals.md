# Tool approval model

`permissions.mode: ask` attaches a per-session approval service. Grants live in memory,
are isolated by chat session, expire, and disappear on restart or `/new`.

## Why backends ask differently

| Backend | Mechanism | Who decides a prompt is needed? |
|---|---|---|
| Claude native | Permission-prompt MCP tool | Claude |
| Claude pty | Terminal dialog relayed through a notification hook | Claude |
| Codex | Blocking `PreToolUse` hook | cotf classifier |

Under `codex exec`, the later event carrying Codex's own permission wording cannot be
answered by a human. Cotf must block earlier, before Codex has classified the call.
Consequently prompt volume across backends is not directly comparable.

## Classification is convenience, not confinement

Cotf keeps common inspection calls quiet and asks about higher-impact calls. Shell
control syntax receives exact-command scope rather than being guessed as one program.
This reduces prompt fatigue; it does not replace the sandbox, egress policy, or scoped
credentials.

## Grant scope

Grants distinguish command programs, exact compound commands, write paths, fetch hosts,
and generic tool names. Claude pty must key the exact rendered dialog because the
transcript does not contain the pending tool call while the dialog is open.

Operator-facing detail is flattened and bounded so tool-controlled input cannot draw a
fake verdict or hide the unshown half of a request.

## Failure behavior

Missing endpoints, malformed responses, timeouts, frontend errors, and unreadable pty
dialogs deny. A turn that used tools without reaching the attached gate is logged as an
unsupervised-turn error.

`claude_mode: auto` delegates the decision to a model. It reduces human prompts but is
not an operator approval mechanism.
