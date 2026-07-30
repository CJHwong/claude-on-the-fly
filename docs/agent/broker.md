# Credential broker + seatbelt jail

Keeps API keys out of the agent. A bypassPermissions agent that gets prompt-injected
will exfiltrate any secret it can reach, so the defense is architectural: the key
value never enters the agent process, and a local broker injects it at the point of use.

Two layers, both opt-in via `COTF_SANDBOX` (default `off`):

| mode   | env curation | seatbelt jail | platform |
|--------|--------------|---------------|----------|
| `off`  | no (inherit full env) | no | all (no change) |
| `env`  | yes (allowlist only) | no | all |
| `jail` | yes | vendored profile (egress→loopback, keychain denied) | macOS |

## How it fits together

1. `broker.start_default_broker()` reads the real key from the keychain, starts a
   loopback reverse proxy, and publishes `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/anthropic`
   into the daemon env.
2. `sandbox.agent_env()` forwards only an allowlist to the spawned agent (essentials +
   `*_BASE_URL` + proxy vars). Every `*_API_KEY` / `*_TOKEN` is dropped by omission.
3. The agent's SDK sends plain HTTP to the base-url. The broker strips any caller-supplied
   auth, injects the real key on the broker→upstream leg, and never re-injects on redirect.
4. In `jail` mode the agent is also wrapped in `sb -j`: external egress is denied (loopback
   allowed, so the broker and local dev servers work), and keychain reads are denied.

## Provisioning credentials

The broker reads generic-password keychain items named `cotf-<provider>`:

```bash
security add-generic-password -a "$USER" -s cotf-anthropic -w "<your key>" -U
```

A route activates only if its item exists (`routes_from_keychain`), so the broker starts
with whatever is provisioned. Add providers by extending `DEFAULT_ROUTES` with a `Route`
(prefix, upstream, header, keychain_service, base_url_env_var).

To hand the agent a subset of a credential rather than its full use, set `methods` and/or
`allowed_tails` on the `Route`. Both are exact-match sets; empty (the default) means no
restriction, so existing routes are unchanged. A request that misses either gets a 403 and
never reaches upstream:

```python
Route(
    prefix="/openai",
    upstream="https://api.openai.com",
    header="authorization",
    value_prefix="Bearer ",
    keychain_service="cotf-openai",
    base_url_env_var="OPENAI_BASE_URL",
    methods=frozenset({"POST"}),
    allowed_tails=frozenset({"v1/chat/completions"}),
)
```

## Narrowing the filesystem and loopback (jail mode)

These knobs only take effect under `COTF_SANDBOX=jail` and each defaults to the current
behavior when unset:

- `COTF_SANDBOX_FS=deny-most` swaps the read-permissive base (`my.sb`) for `my.deny-most.sb`,
  which makes `$HOME` opaque and re-grants only the project dir, `~/.claude`, `~/.cache/uv`,
  and up to three operator paths. Reads outside `$HOME` (system libraries, the toolchain)
  stay allowed so binaries run. This is coarser than `my.sb` by design: an agent that reads
  a home dotfile outside the granted set (e.g. `~/.gitconfig`) is denied. Note the agent's own
  binary and interpreters often live under `$HOME` (`~/.local/bin`, mise/nvm/npm paths); grant
  those via `COTF_SANDBOX_EXTRA_PATHS` or the agent cannot exec and the run fails to start.
- `COTF_SANDBOX_EXTRA_PATHS` (colon-separated, capped at 3) are extra read grants for
  deny-most. Seatbelt has no arrays, so a fourth path is dropped with a warning; nest under
  a shared parent or edit the profile if you need more.
- `COTF_SANDBOX_BROKER_ONLY_LOOPBACK=1` narrows egress from all loopback ports to just the
  broker's port (read from the published `*_BASE_URL`), closing the arbitrary-local-sink
  path. If no broker base-url is present it leaves loopback open, so the agent is never
  locked out of a broker it needs.

## Seatbelt (jail mode)

Nothing to install. The profiles live in `src/claude_on_the_fly/seatbelt/`
(`my.sb` base + `my.jail.sb`, vendored from agent-seatbelt). `sandbox.wrap`
invokes `sandbox-exec` against them directly; the jail imports the base and
overrides only egress + keychain. macOS only; on other platforms jail mode
degrades to `env` (curation without the seatbelt). Re-vendor by copying the two
`.sb` files from agent-seatbelt.

## Egress proxy: gating ordinary HTTPS, and asking on the fly

The broker only covers providers it holds a key for, addressed by path prefix.
That shape cannot gate anything else: the agent asks for a prefix, and an
unmapped prefix carries no host, so the broker has nothing to allowlist or ask
about. Under `jail` that left `git`, `pip`, `curl`, and `gh` with nowhere to go.

`egress.py` fills the gap. `HTTPS_PROXY` points the agent at a loopback CONNECT
proxy, so every outbound connection arrives as a cleartext `CONNECT host:443`
naming its destination. Unknown hosts become an operator question instead of a
failed run.

```
agent ──CONNECT api.github.com:443──▶ egress proxy
                                        │  in COTF_EGRESS_ALLOW? ──▶ tunnel
                                        │  never-ask / private? ────▶ 403
                                        └─ else ask operator ──┬─ approve ─▶ tunnel
                                                               └─ deny ────▶ 403
```

**No TLS interception, deliberately.** Gating a host needs only the CONNECT
line. Reading the body would need a private CA, a synthesized leaf per host, and
every client in the sandbox trusting it, which buys credential injection the
broker already does, at the cost of a CA the agent could be tricked into
trusting and a trust-store problem for any Go binary on macOS (Go reads the
system store and ignores `SSL_CERT_FILE`). So the proxy learns *where*, never
*what*.

Consequence, stated plainly: an approved host is a covert channel. This bounds
which hosts the agent reaches and nothing about what it sends there.

## Runtime approvals

`approvals.py` turns a denial into a question. The requester blocks on
`ApprovalGate.request`, the operator answers on the attached frontend, and an
approval writes a scoped grant the requester consults from then on. Two
requesters use it: the egress proxy (unknown host) and the broker (a
`methods`/`allowed_tails` scope miss).

| Env | Effect |
|-----|--------|
| `COTF_EGRESS_ALLOW` | *Extra* hosts tunnelled without asking, on top of the built-ins. Front-load what the job needs. |

`egress.DEFAULT_ALLOWED_HOSTS` is always allowed: `api.anthropic.com`,
`api.openai.com`, `chatgpt.com`, `ab.chatgpt.com`. The criterion is narrow — a
host earns a place only if a supported backend cannot complete a turn without it,
so the model APIs and nothing else. Gating those would stop every fresh
deployment on an approval for the agent's own first LLM call.

Package registries, `github.com`, telemetry, and self-update endpoints are
deliberately excluded, because each is a decision worth making explicitly: a
package install is arbitrary code execution, `github.com` grants writes, and
telemetry is optional by definition.
| `COTF_APPROVAL_CHAT_ID` | Telegram only: pins prompts to one chat. Unset routes them to the session's own chat. |

### Where the prompt lands, and who may answer

Two separate questions, deliberately. **Routing** puts the prompt in the thread
whose agent triggered it, so you approve where the work is happening.
**Authorization** is re-checked on the click: `_on_approval_action` tests the
clicker against the frontend's allowed senders, so a bystander in a shared
channel can read the prompt but cannot answer it. That split is what makes
in-thread routing safe.

Slack buttons require a bot token (`xoxb-`), because Slack only delivers
interaction payloads to a bot-token install. A user-token deployment posts a
prompt nobody can answer, so `ask_approval` refuses up front instead.

There is no configured fallback channel, deliberately. An approval is an
interactive act, so work with no conversation behind it (cron, the job queue)
has nobody to ask and is denied. A fallback would quietly let an unattended job
acquire network access nobody was present to grant. Telegram differs only
because its fallback is the allowed user's own DM, which has exactly one person
in it; Slack has no equivalent.

### One proxy per session

`SessionEgress` starts a separate CONNECT proxy, with its own grant store, for
each `(chat_id, session_uuid)`. Two reasons that are really one:

- **Attribution.** A CONNECT carries a hostname and nothing else, so a shared
  proxy cannot tell which of several concurrent chats made it. The port is the
  only available label, and per-session ports are what let a prompt name its
  originating conversation at all.
- **Grant scope.** A grant lives on one `ApprovalBroker`'s store. Share the
  broker and approving a host in one chat silently authorizes it for every other
  chat and for cron. One store per session confines it, and `/new` earns a fresh
  proxy rather than inheriting the old session's grants.

The per-session proxy URL reaches the spawn through a `ContextVar`
(`sandbox.session_env`), set in `Orchestrator._process`. A ContextVar rather than
a parameter because the spawn is several frames down inside a backend; asyncio
copies the context per task, so each turn's agent sees only its own proxy.

The credential broker stays daemon-wide by contrast: its routes are operator
config, not something a session earns.

Requests are built from what the *proxy observed*, never from text the agent
authored, because an agent under injection would otherwise write its own
justification. The prompt shows the host, the port, and the resolved address.

Three limits keep the ask channel from becoming the weak point:

- **never-ask** refuses some subjects without asking: cloud metadata hostnames,
  private/link-local IP literals, and any name that *resolves* into private
  space. An operator tapping approve on a phone is the softest link in the
  chain, so the things an injection payload wants most are never offered.
- **rate limit** treats a burst as one signal, not N decisions. Past 10 requests
  in 10 minutes the gate denies without asking.
- **expiry** on every grant (1h default), and the store dies with the process.
  Nothing persists. Revocation is `stop()`.

Every failure path denies: timeout, a frontend exception, no configured channel,
no `ask_approval` on the frontend. Concurrent duplicate requests collapse onto
one question so a retrying agent can't spam the operator.

The `COTF_EGRESS_ALLOW` list is checked *before* the private-address guard, so
naming `localhost` there reaches the agent's own dev server. That is a config
act; the approval path can never grant private space.

### Bootstrapping the allowlist

Run permissive and read the log. Every decision is logged at INFO through
`logs.py`, which is the grant ledger, so a week of real work tells you the host
set to put in `COTF_EGRESS_ALLOW` instead of guessing it up front.

## Command broker: credentialed CLIs run outside the sandbox

Env curation handles secrets in the environment. It does nothing for credentials
on disk under `$HOME`, which is where `gh`, `aws`, `kubectl`, `acli` keep theirs.
Denying those files used to cost the whole capability, and the agent routed around
it (see "The architectural limit" below). `commands.py` closes that: a shim
on the agent's PATH forwards the invocation to a broker outside the sandbox, which
runs the real binary with the real credential and returns only the output.

```
sandbox                                  daemon
PATH=<DATA_DIR>/shims:$PATH
  gh pr list ──shim──▶ 127.0.0.1:<port>/run
                         ├─ refuse credential readback
                         ├─ log the full argv (WARNING)
                         ├─ run real gh, narrow env, capped output
                         └─ {stdout, stderr, rc}
```

Verified live: inside the jail `cat ~/.config/gh/hosts.yml` is EPERM while
`gh auth status` still names the account. Reporting the account requires reading
that exact file, so the credential was read outside and only output crossed back.

**Setup step that is easy to miss.** The shim only gets used if the agent has no
better option. A provider-side GitHub connector is a better option from the model's
point of view, and it wins: with `github@openai-curated-remote` enabled, a live run
produced zero shim invocations and answered from MCP instead. Disable the connector
in the agent's own client config, or this whole path is dead weight and GitHub
traffic stays invisible. See "The architectural limit" below for the evidence and
the exact setting.

### No action policy, deliberately

The broker forwards whatever it is given. It does **not** allow/deny/ask per
subcommand, because `gh api --method DELETE /repos/o/r` defeats any subcommand
denylist — a parser here would be a boundary that can be walked around, plus a
second enumerate-the-bad list. The scope lever is the **token**: use a
fine-grained PAT limited to what the agent needs.

The one refusal is **credential readback**, a command whose output *is* the
secret. That is not policy, it is closing the door the broker opens: forwarding
`gh auth token` would put the token straight into the sandbox. Refused for `gh`:
`auth token`, and `--show-token` anywhere in argv. Argv parsing skips flag values,
so `gh --repo o/r auth token` cannot dodge it.

### Adding a tool

One `SHIMMED_TOOLS` entry plus a credential deny in `my.sb`. No new machinery. A
tool whose binary is not on PATH is skipped rather than shimmed, so a missing
binary stays "command not found".

Currently shimmed: `gh`. Denied but **not** shimmed, so unavailable under `jail`
until someone adds the entry: `~/.config/acli`, `~/.local/share/acli`,
`~/.config/gws`, `~/.config/gcloud`, `~/.sentryclirc`, plus the pre-existing
`~/.aws/credentials`, `~/.kube`, `~/.docker`, ssh private keys. That order is
deliberate: deny first, shim as needed.

### Why loopback and not a unix socket

A unix socket was the first choice and it does not work. Verified against real
`sandbox-exec` runs: of every candidate SBPL filter (`literal`, `subpath`, `path`,
`network*`, `remote unix-socket (path-literal …)`, `system-socket`) only
`(remote unix)` permits the connect, and it is not path-scoped — it opens *every*
unix socket on the machine, including the Docker socket and the ssh-agent this
profile otherwise protects. A loopback port can be scoped to one endpoint
(`COTF_SANDBOX_BROKER_ONLY_LOOPBACK`); a unix socket allow cannot.

Loopback carries no authentication, which is not a new exposure: the credential
files the broker reads are already readable by any same-UID process on the host.
The sandbox is the only thing being constrained, so a token here would be theatre.

### The shim is not the boundary

The agent can still run `/opt/homebrew/bin/gh` by absolute path. That is useless
because the profile denies the credential, and *that* deny is the boundary. The
shim restores capability under the deny; it does not create the isolation.

## What the log records, and what it cannot

`LOG_LEVEL=INFO` (the default) carries every gate decision. `LOG_LEVEL=DEBUG`
adds the inputs behind each one. Diagnosing a run means reading these in order.

| Question | Line | Level |
|---|---|---|
| Was the jail actually applied? | `sandbox: jailed sh (fs=my.sb, loopback=[...], project=...)` | INFO |
| Were the credential denies in force? | `sandbox: 5/6 probed credential paths confirmed denied under my.sb (1 absent, untested)` | INFO |
| Which env reached the agent? | `sandbox: env curated, 10 forwarded [...], 76 dropped by omission` | DEBUG |
| Did the CLI go through the shim? | `commands: shim invocation gh argv0='/…/shims/gh' cwd='…'` | DEBUG |
| What did it run, and as what? | `commands: RUN gh ['pr','list']` then `commands: gh runs /opt/homebrew/bin/gh with env [...]` | WARNING / DEBUG |
| Was a credential readback blocked? | `commands: REFUSE gh ['auth','token'] (credential readback, cwd=…)` | WARNING |
| Which host, and *why* was it allowed? | `egress[chat 42]: allow CONNECT pypi.org:443 via 151.101.128.223 (operator approved)` | INFO |
| Was a human actually in the loop? | `pre-approved host` / `standing grant` / `operator approved` in that same line | INFO |
| Why was a host refused? | `refuse … (never-ask policy)` / `(DNS failed: …)` / `(resolves to non-public …)` / `(host is not DNS-safe)` | WARNING |
| Did the agent send its own key? | `broker: stripped caller-supplied auth header(s) ['x-api-key']` | WARNING |
| Which keychain item was injected? | `broker: allow POST api.anthropic.com/anthropic/v1/messages [cotf-anthropic] -> 200` | INFO |

Every session-scoped line carries a `[chat <id>]` tag, because the proxy and the
grant store are per-session and two chats reaching the same host are otherwise
indistinguishable.

### Two things the log will never tell you

- **The agent's own denied reads.** macOS cannot report a seatbelt denial. A bare
  `deny` writes nothing to the unified log, `(with report)` is rejected for deny
  actions, and three separate log predicates over a real violation return
  nothing. Tested, not assumed. `sandbox.verify_denials` is the substitute: it
  attempts the reads itself at startup under the same profile, so a run records
  that the boundary *was in force* even though it cannot record what the agent
  tried. An absent path is reported as `absent`, never folded into the denied
  count, so a machine with no credentials cannot look like a tested boundary.
- **Where a brokered command's traffic went.** The real `gh` runs outside the
  jail, so its `api.github.com` calls never appear as CONNECTs. The argv is the
  record for that path.

### Message content is redacted by default

`COTF_LOG_CONTENT` is off. With it off, prompts and agent replies are logged as a
length (`<412 chars redacted>`) and an over-long argv token is clipped
(`'I have reviewed this and the approach seems wron…<+310>'`). The log directory
is shaped for a file syncer, so anything written there should be assumed to leave
the machine and to sit there for `COTF_LOG_KEEP_DAYS`; a prompt is the user's data
rather than a diagnostic. Turn it on while working a message-handling bug, then
turn it back off. Clipping is by token *length*, not by flag name, so a new
content-carrying flag is covered without being enumerated.

## Scope and caveats

- **Wired into the chat orchestrator** (`orchestrator.run`: telegram/slack/gmail). Symphony
  and scheduler share the spawn-site env curation but do **not** auto-start the broker. Do
  not set `COTF_SANDBOX` for them until the broker is started in their run loops too,
  or curation removes the key with no base-url to replace it and breaks LLM auth.
- **Auth model:** the broker only covers a backend that authenticates with an
  injectable key *header* it can be pointed at via a base-url. That is narrower
  than it sounds, and it is verified rather than assumed:
  - `claude` with `ANTHROPIC_API_KEY`: covered.
  - `claude` via OAuth (claude.ai login): **not covered**, no header key exists.
  - `codex` with an API key: covered.
  - `codex` via ChatGPT login: **not covered.** Its `~/.codex/auth.json` holds
    OAuth tokens with `OPENAI_API_KEY: null`, so it never reads the env var,
    ignores `OPENAI_BASE_URL`, and reaches its own endpoint directly. A live run
    confirmed this: codex returned a real completion while the broker logged zero
    requests.

  This is the argument for the egress proxy rather than more broker routes. The
  CONNECT gate does not care how a client authenticates, and a live run shows it
  gating that same OAuth codex on `chatgpt.com:443` and `ab.chatgpt.com:443`.
- **Codex starts under `jail`.** It previously failed with
  `failed to initialize in-process app-server client: Operation not permitted`,
  which looked like a design limit and was not: the cause was `~/.codex` missing
  from the profile's write allowlist, and one grant fixed it. Worth remembering as
  a diagnostic habit — that message is a filesystem denial from a local helper
  process, not a network one, and the denial itself is invisible in the log (see
  above), so bisecting the profile is the only way to find it.
- **Residual risk:** a hijacked agent cannot steal a key it never holds, but it can still
  drive the allowlisted broker routes as a confused deputy (e.g. smuggle data inside an
  allowlisted API call). The allowlist is the security knob; keep routes minimal.
- **The approval channel is itself an attack surface.** An agent that can request
  permission can engineer the request, so injection becomes social engineering:
  "I need `evil.example` to run the integration tests" reads fine on a phone at
  11pm. The never-ask tier is the structural answer, since it is the only part
  that cannot be talked around. Keep it wide.
- **Egress gating is by host, not by action.** An approved `api.github.com` grant
  permits any call to it, including a force-push, because the proxy does not read
  the request. Pair a grant with a credential scope that is narrow on its own.

### The architectural limit: the boundary is the process, not the session

Everything in this document constrains **syscalls the agent process makes**. A
seatbelt profile, a CONNECT gate, and a curated env all sit on that one seam, and
on that seam they hold — verified with real runs, see the log table above.

Anything the agent accomplishes **without a syscall** is outside all three gates.
The model provider can act on the agent's behalf inside the already-open,
necessarily-approved TLS session to its own API, and no host policy can see it: the
work happens on the provider's servers and only the result comes back, inside a
tunnel this project cannot read and must not block.

This is not one gap to be patched. It is the shape of the thing, and it has at
least two distinct channels. Both were observed live on 2026-07-30 under
`COTF_SANDBOX=jail`:

**1. Server-side tool connectors (MCP).** Asked for recent open PRs, codex called
`mcp__codex_apps__github` → `_search_prs` and got real private PRs from private org
repos. The command broker logged **zero** `gh` invocations and the egress log
showed **324 CONNECTs, all to `chatgpt.com` / `ab.chatgpt.com`, and none to any
GitHub host**. The credential was the provider's connected GitHub app, which this
project never holds. The declared toolset also included writes
(`_create_pull_request`, `_update_pull_request`, `_update_issue`), so this is not a
read-only leak.

**2. Provider-side browsing (`web_search` / `web_fetch`).** Asked for the latest
`httpx` version on PyPI, codex called `run` with
`{"open":[{"ref_id":"https://pypi.org/pypi/httpx/json"}]}` and a `search_query`.
`pypi.org` was fetched **by the provider's servers**. No CONNECT, no approval
prompt, no log line, and nothing for the CONNECT gate to refuse, because no
connection to `pypi.org` was ever attempted from this machine.

Channel 2 is the more instructive one: it means an approved model host is not just
a covert channel for exfiltration (already documented above) but a general-purpose
**inbound** fetch capability. Pre-approving `chatgpt.com` is unavoidable — codex
cannot complete a turn without it — and doing so hands the agent an ungateable
proxy for both directions. The same would apply to `api.anthropic.com` for any
server-side tool surface offered there.

### What actually mitigates it

Not a local hook. An earlier version of this document claimed codex
`pre_tool_use` hooks (`~/.codex/hooks.json`) were "where an MCP tool name can
actually be refused." That is too optimistic to rely on: it might intercept a
named MCP tool, but `web_search` / `web_fetch` are model-side tools declared in the
request and resolved server-side, so a local hook is not guaranteed to see them at
all. Betting the boundary on it would be a guess.

The lever that works is **refusing to declare the capability**, which lives in the
provider's client config, not in this project. Verified today:

```
[plugins."github@openai-curated-remote"]
enabled = false
```

With the connector enabled, codex used MCP and the shim went unused. With it
disabled and nothing else changed, the same prompt produced:

```
commands: RUN gh ['api', 'user', '--jq', '.login']
commands: RUN gh ['api', 'search/issues?q=is:pr+is:open+author:@me&sort=cr…<+27>', …]
commands: gh exited 0 (395 B stdout, 0 B stderr)
```

Same three PRs, `rc=0` in 20s, **zero** `mcp__codex_apps` calls in the rollout, and
the whole interaction visible in the command log. So the credential broker and the
connector are substitutes, and only one of them is observable. Disabling the
connector is what makes the shim the path the agent actually takes.

Two caveats on that mitigation. It is **operator config in the agent's own client**,
so this project cannot enforce it, only document it and check for it. And the
plugin's `SKILL.md` stays readable in `~/.codex/plugins/cache/` when disabled — the
agent still reads it and still says it is "using the GitHub skill", it just has no
MCP tool to reach for and falls back to `gh`. Removing the cache is not required.

Provider-side browsing has no equivalent lever in this project's control. Treat any
task whose confidentiality depends on the agent *not* being able to fetch a URL as
out of scope for this sandbox.
