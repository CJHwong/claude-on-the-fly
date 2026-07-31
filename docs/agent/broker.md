# Credential broker + seatbelt jail

Keeps API keys out of the agent. A bypassPermissions agent that gets prompt-injected
will exfiltrate any secret it can reach, so the defense is architectural: the key
value never enters the agent process, and a local broker injects it at the point of use.

Two layers, both opt-in via `sandbox.mode` in `~/.claude-on-the-fly/config.yaml`
(default `off`; the `COTF_SANDBOX` environment variable still works and still wins):

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

These knobs only take effect under `sandbox.mode: jail` and each defaults to the current
behavior when unset:

- `sandbox.fs: deny-most` swaps the read-permissive base (`fs-allow-reads.sb`) for `fs-deny-most.sb`,
  which makes `$HOME` opaque and re-grants only the project dir, `~/.claude`, `~/.cache/uv`,
  and up to three operator paths. Reads outside `$HOME` (system libraries, the toolchain)
  stay allowed so binaries run. This is coarser than `fs-allow-reads.sb` by design: an agent that reads
  a home dotfile outside the granted set (e.g. `~/.gitconfig`) is denied. Note the agent's own
  binary and interpreters often live under `$HOME` (`~/.local/bin`, mise/nvm/npm paths); grant
  those via `sandbox.extra_paths` or the agent cannot exec and the run fails to start.
- `sandbox.extra_paths` (a YAML list, capped at 3) are extra read grants for
  deny-most. Seatbelt has no arrays, so a fourth path is dropped with a warning; nest under
  a shared parent or edit the profile if you need more.
- `sandbox.broker_only_loopback: true` narrows egress from all loopback ports to just the
  broker's port (read from the published `*_BASE_URL`), closing the arbitrary-local-sink
  path. If no broker base-url is present it leaves loopback open, so the agent is never
  locked out of a broker it needs.

## Seatbelt (jail mode)

Nothing to install. The profiles live in `src/claude_on_the_fly/seatbelt/`
(`jail.sb` plus a filesystem base, derived from agent-seatbelt). `sandbox.wrap`
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
                                        │  in egress.allow? ────────▶ tunnel
                                        │  never-ask / private? ────▶ 403
                                        └─ else ask operator ──┬─ approve ─▶ tunnel
                                                               └─ deny ────▶ 403
```

### A refusal has to fit in the status line

**No client surfaces a CONNECT response body.** Verified against curl, httpx and
stdlib urllib: each reads the status line and discards the rest, so `curl` reports
only `(56) CONNECT tunnel failed, response 403`. A carefully worded body reaches
nobody.

The reason phrase does get through, and httpx and urllib put it straight into the
exception text an agent reads. So each of the four causes carries a short phrase
saying which one it was and whether retrying can help:

| Cause | What the agent sees |
|---|---|
| never-ask host | `403 Forbidden by egress policy: host permanently blocked, cannot be approved` |
| operator declined | `403 Forbidden by egress policy: host not approved, an operator declined it` |
| DNS failed, or resolves private | `403 Forbidden by egress policy: no usable public address, retrying will not help` |
| not a valid hostname | `403 Forbidden by egress policy: not a valid hostname, check the URL you built` |

Only "operator declined" involved a human, which is the distinction that matters:
an agent told a human said no will reasonably retry or look for another route, and
for the other three there is nothing to retry and no other route to find. The full
instruction still goes in the body for anything that does read one.

The phrase never interpolates the requested host — they are module constants, so no
agent-controlled bytes reach the status line where a CR/LF would be header
injection. A 204-character phrase arrived intact on all three clients, so the
one-line forms above are well inside what works.

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

The `egress.allow` list in the policy file (see "The policy file" below) is
tunnelled without asking. Bundled: `api.anthropic.com`, `api.openai.com`,
`chatgpt.com`, `ab.chatgpt.com`. The criterion is narrow — a host earns a place
only if a supported backend cannot complete a turn without it, so the model APIs
and nothing else. Gating those would stop every fresh deployment on an approval
for the agent's own first LLM call.

Package registries, `github.com`, telemetry, and self-update endpoints are
deliberately excluded, because each is a decision worth making explicitly: a
package install is arbitrary code execution, `github.com` grants writes, and
telemetry is optional by definition.

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

`egress.allow` is checked *before* the private-address guard, so naming
`localhost` there reaches the agent's own dev server. That is a config act; the
approval path can never grant private space.

### Bootstrapping the allowlist

Run permissive and read the log. Every decision is logged at INFO through
`logs.py`, which is the grant ledger, so a week of real work tells you the host
set to put in `egress.allow` instead of guessing it up front.

## The policy file

Both host lists and the brokered-command list live in one file:

```
~/.claude-on-the-fly/config.yaml
```

It is seeded from a commented template (`settings.py`, `config.yaml` inside the
package) the first time any daemon starts, so the first thing you open explains its
own schema. `settings.check_operator_settings` runs at startup and names anything
wrong with it — including a misspelled section, which YAML accepts happily and
which would otherwise do nothing with no diagnostic anywhere.

**Saving it is enough.** `settings.py` re-parses on a change to the file's mtime or
size, so an edit lands at the next read rather than the next restart — which for the
allowlist means the next CONNECT, and for `permissions.ttl_seconds` the next grant.
The exceptions are the fields read once at startup, where acting on the new value
means binding a socket, rewriting the agent's PATH, or constructing a service that
was never built: `commands:` and `permissions.mode`. Those are listed in
`settings.RESTART_REQUIRED`, and changing one gets you a message on the frontend
naming it, on the next turn. Reporting instead of applying is deliberate — tearing
down a credential-holding broker mid-turn trades a config annoyance for a class of
mid-turn failure, and both fields are set once per deployment.

```yaml
egress:
  allow:
    - pypi.org               # uv installs
    - files.pythonhosted.org # ...and the wheels they fetch
  never_ask:
    - internal.corp.example
commands:
  tools:
    - name: kubectl
      readback: [config view]
```

Three properties worth knowing:

- **Merged per section, not per file.** A malformed `egress:` block logs an ERROR
  naming itself and falls back to the bundled hosts, while `commands:` still
  loads. Whole-file fallback would have been simpler and wrong: a typo in a host
  list would silently revoke a brokered tool, and the only clue would be a CLI
  that stopped working for reasons nothing connects to the edit.
- **`never_ask` is a union, never a subtraction.** Your entries are added; you
  cannot remove a bundled one. The bundled entries are the cloud metadata
  endpoints, which hand instance credentials to anything that reaches them, so no
  config edit — and no agent that somehow gets a write — can re-open one.
- **Hosts are validated at load.** A URL, a `host:port`, or anything that is not a
  hostname is an error naming the file, not a silently dead entry that resurfaces
  months later as an approval prompt for a host you believe you already allowed.

The file lives under `DATA_DIR`, which is deliberately absent from the seatbelt
write allowlist. It decides what runs outside the sandbox holding real credentials
and which hosts skip the operator prompt, so an agent that could write it would
make the whole thing a suggestion.

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

Config, not code. Add it under `commands:` in the policy file:

```yaml
commands:
  tools:
    - name: kubectl
      readback:
        - config view        # also refuses `kubectl --context x config view`
      readback_flags: [--token]
      env_passthrough: [KUBECONFIG, NO_COLOR]
```

`readback` entries are the leading words of a command rather than nested lists,
because this file's whole job is refusing the right commands and `[[config,
view]]` is easy to get subtly wrong. Matching is against the leading non-flag
tokens, so a global flag cannot push a refused pair out of the prefix.

Your entries **merge by name** over the bundled ones, so adding one tool keeps the
vetted refusals on the others. Overriding a bundled entry is allowed and logged; an
override that *drops* a refusal the bundled entry had is logged at WARNING naming
what is no longer refused, because that is the one edit here that hands the agent a
credential. A malformed section falls back to the bundled defaults and logs at
ERROR rather than starting with no tools at all, since silently losing every shim
is what sends the agent looking for another route to the same capability.

A tool whose binary is not on the daemon's PATH is skipped rather than shimmed, so
a missing binary stays "command not found" instead of becoming a confusing broker
error. `snowsql` is an example: denied, not installed, so nothing to shim.

### What happens to a CLI you have *not* listed

It is not shimmed, so PATH inside the jail resolves to the real binary. It runs,
and then fails on its own credential store, because the profile denies about 48 of
them. That is the intended outcome — but it is also indistinguishable, from the
agent's side, from a broken install.

So the sandbox note in the system prompt names the tools that *are* brokered, and
gives the remedy for one that is not: add it to the `commands:` section. It
explicitly says the remedy is **not** a read grant, because granting the credential
path is the fix that either leaves the tool broken or hands its token to the
session — the failure this whole subsystem exists to undo. It also tells the agent
not to reach the same service another way, which is the observed failure mode: a
denied `gh` once became a provider-side GitHub integration, over an approved host,
with a credential this project never held.

Currently bundled: `gh` and `acli`. `acli` carries no token readback because
`acli auth` has no token-printing subcommand at all (checked against
`acli auth --help`); what it refuses is `auth logout` / `login` / `switch`, which
change the operator's own auth state from inside an agent turn.

Denied but **not** shimmed, so unavailable under `jail`: `~/.aws`, `~/.kube`,
`~/.docker`, `~/.config/gcloud`, ssh private keys, `~/.config/gws`,
`~/.sentryclirc`, and the infrastructure and PaaS stores listed in the profile.
That order is deliberate: deny first, shim only what is needed. For `aws`,
`kubectl`, `terraform`, and `vault` it is deliberate and permanent — the shim runs
the binary outside the jail with the operator's full credential and no action
policy, and `terraform apply` or `kubectl delete` against production is not a
trade worth making for convenience. Use `sandbox.mode: env` for a run that needs
one of those.

### Why loopback and not a unix socket

A unix socket was the first choice and it does not work. Verified against real
`sandbox-exec` runs: of every candidate SBPL filter (`literal`, `subpath`, `path`,
`network*`, `remote unix-socket (path-literal …)`, `system-socket`) only
`(remote unix)` permits the connect, and it is not path-scoped — it opens *every*
unix socket on the machine, including the Docker socket and the ssh-agent this
profile otherwise protects. A loopback port can be scoped to one endpoint
(`sandbox.broker_only_loopback`); a unix socket allow cannot.

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
| Was the jail actually applied? | `sandbox: jailed sh (fs=fs-allow-reads.sb, loopback=[...], project=...)` | INFO |
| Were the credential denies in force? | `sandbox: 5/6 probed credential paths confirmed denied under fs-allow-reads.sb (1 absent, untested)` | INFO |
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

### Message content is redacted above DEBUG

At `INFO` and higher, prompts and agent replies are logged as a length
(`<412 chars redacted>`) and an over-long argv token is clipped
(`'I have reviewed this and the approach seems wron…<+310>'`). At `LOG_LEVEL=DEBUG`
both come through.

The level is the control, not a separate switch, because `DEBUG` already carries
message content by routes no redaction helper touches: `raw slack event` dumps the
whole event including `text`, and slack_bolt logs full request and response
payloads. A dedicated flag would have implied you could run at `DEBUG` without
content in the file, which was never true. So the rule is simply: **a DEBUG log
has everything in it and is not shareable**; an INFO log is.

That matters because this directory is shaped for a file syncer, so anything
written here should be assumed to leave the machine and to sit there for
`logs.keep_days`. A prompt is the user's data rather than a diagnostic.

Argv clipping is by token *length*, not by flag name, so a new content-carrying
flag is covered without being enumerated.

## Scope and caveats

- **Wired into the chat orchestrator** (`orchestrator.run`: telegram/slack/gmail). Symphony
  and scheduler share the spawn-site env curation but do **not** auto-start the broker. Do
  not set `sandbox.mode` for them until the broker is started in their run loops too,
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
`sandbox.mode: jail`:

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

## Tool permissions

Off by default (`permissions.mode: off` in `~/.claude-on-the-fly/config.yaml`).
With it off, argv is byte-identical to a build without this feature, `--permission-mode
bypassPermissions` included. Switching it to `ask` routes permission questions to
whichever frontend owns the session, with approve/deny buttons.

**What you are asked is not the same on every backend.** This is the central thing to
know, and it is not a design preference: it is what each CLI actually exposes.

| Backend | Mechanism | Whose question | cotf classifies? |
|---|---|---|---|
| claude native | `--permission-prompt-tool` via a stdio MCP shim | claude's own | no |
| claude pty | `Notification` hook, answered as a keystroke | claude's own | no |
| codex | `PreToolUse` hook injected with `-c` | **cotf's** | yes |

For claude, the CLI decides what deserves a human and cotf forwards it. Measured on
2.1.220: it asked about `chmod`, `find -delete`, `sudo`, `curl` and `git init`, and did
not ask about `ls`, `cat`, `echo`, `git status` or `Read`.

For codex there is nothing forwardable, which is not the same as codex having no
approval system. It has one; under `codex exec` none of it reaches a human.

- `codex exec` overrides `approval_policy` to `never` whatever you pass. Measured:
  request `untrusted`, codex reports `never`.
- Set `approvals_reviewer` and approvals do fire, and the `PermissionRequest` hook
  arrives carrying codex's own wording, e.g. *"Allow curl network access to check the
  HTTP status code from example.com?"*.
- But the reviewer is a model. `approvals_reviewer` accepts `user`, `auto_review` and
  `guardian_subagent`; `user` is inert under exec (no event at all, the sandbox simply
  denies), and the other two each spawn a guardian subagent that answers
  `{"outcome":"allow"}`.
- And that hook cannot be answered. `block`, `denied` and `approved` were all ignored
  and the command ran. It also fires 25ms *after* `PreToolUse`, the only hook that can
  block:

  ```
  13:29:00.026  PreToolUse         can block, does not yet know approval is wanted
  13:29:00.051  PermissionRequest  knows, cannot block
  ```

So the gate has to sit where codex has not yet formed an opinion, and cotf decides what
is worth interrupting you for, in `permissions.worth_asking`. That ordering is the whole
reason the two backends differ; it is not a preference. The approval card says which:
`claude asked:` versus `cotf asked:`.

**That classifier is a convenience filter, not a boundary.** It exists so a turn of
`ls` and `git status` does not cost twenty taps. It is defeated by anything that makes
the first word stop predicting what runs, which is why compound commands are refused
outright rather than parsed. The boundary is unchanged and elsewhere: the seatbelt
profile, the CONNECT proxy, and the credential broker.

### pty is gated through its own dialog

Interactive claude accepts `--permission-prompt-tool`, resolves it, starts the server
and answers `tools/list` on it, and then never calls it: it draws its own terminal
dialog instead. So pty gets the permission mode but not the prompt tool, and the dialog
is relayed instead.

1. cotf sets `CLAUDE_PTY_TMUX_SESSION` to a name it chooses, because claude-pty's own
   default is PID-based and unpredictable from outside. That is how the daemon knows
   which pane it may type into.
2. A `Notification` hook with matcher `permission_prompt`, installed via `--settings`,
   posts to the daemon and returns. It cannot decide anything: claude is blocked on the
   dialog, not on the hook.
3. The daemon captures the pane, parses the dialog, asks the operator, and types the
   answer back.

**The dialog is the only source of what is being asked.** The obvious alternative does
not exist: at the moment the dialog is up, claude's transcript contains no `tool_use`
record at all (measured at 12 lines, none of them a tool call, while finished
transcripts in the same directory hold two). The assistant message is written after the
permission resolves.

That has two consequences.

**Grant scope is the exact dialog**, keyed as `pty:<tool>:<sha256(body)[:12]>`. A
terminal hard-wraps, and one real capture broke a file path across two lines, so the
body cannot be trusted as an identity and program-level scoping like `bash:git` is not
available here. Hashing the whole prompt means a grant matches only an identical one:
less reuse than the other backends, and no way to over-widen. The operator reads the
full text; only the digest reaches the grant log.

**The option digits are read, never assumed.** Two real dialogs put No at 3 only because
both offered a widen-scope option in the middle; one without it puts No at 2. A "Yes"
whose label carries an "and also" clause is rejected outright, so
`2. Yes, and don't ask again for: chmod a+w *` can never be typed. If the option list
cannot be resolved unambiguously, cotf types nothing at all and says so, because a
guessed digit could approve something the operator never saw.

**A refusal costs an extra model turn.** Pressing the refuse option ends the turn with no
final assistant message, so claude's Stop hook never fires, no envelope is written, and
claude-pty waits until it gives up (measured: `PTY_EXIT=1` after the full timeout).
cotf therefore sends the keystroke *and* injects a short fixed message, which lets the
turn end normally and is also the only way the reason reaches the model. The wording is
fixed rather than operator-supplied, since that text is injected into a live session.

### claude_mode is the volume knob

`claude_mode` is the `--permission-mode` handed to claude under `ask`. `default` ships.

`auto` is accepted and **is not a safety net.** It hands the decision to a model rather
than to you. Measured on sonnet 5 against a deliberately nasty set, it approved all six
without asking once, at 2.6 to 3.1 seconds and one model call each: `sudo -n whoami`, a
write into `/etc`, `chmod -R 777`, `find -delete`, `ls ~/.ssh`, and `curl`. Pick it for
fewer interruptions, not for protection. It is also unavailable on some models
(haiku 4.5 prints `auto mode unavailable for this model`) and silently falls back.

`bypassPermissions` and `dontAsk` are **refused** with an error naming both settings.
Both were measured at zero prompts, so pairing either with `ask` would give a daemon
that reports approvals as on and gates nothing.

### Why codex needs a trust bypass, and what makes it safe

`permissions.codex_argv` passes `--dangerously-bypass-hook-trust`. It has to: codex
persists no trust entry for a hook supplied via `-c`, and without the bypass the hook is
**silently skipped and the command runs anyway**. Nine candidate trust-key spellings
were tried, `trusted_hash` is not derivable from the command string, and a run with the
bypass writes no trust entry to fall back on.

That flag is only safe because `~/.codex` is deny-default for writes. The directory
has to stay partly writable or codex will not start, so both profiles deny it wholesale
and re-grant only what a real `codex exec` turn was measured writing: `sessions/`,
`tmp/`, `cache/`, `log/`, `shell_snapshots/`, `plugins/cache/`, `models_cache.json`,
`version.json`, `installation_id`, the sqlite family with their `-wal`/`-shm` siblings,
and `auth.json`.

`auth.json` is granted deliberately. Denying the write protects nothing, since reading
it is already permitted by design, and it would break ChatGPT token refresh the first
time one expired.

This started as the opposite shape, a blanket grant with three files denied, and that
list was already incomplete: `plugins/`, `history.jsonl` and `AGENTS.md` stayed
writable. `AGENTS.md` is the sharp one, being standing instructions codex reads on
every later run, so an injected agent could leave itself orders that outlive the
session. Enumerating the dangerous files loses that race every time codex adds one.

Two granted paths remain execution-adjacent and cannot be closed, which is a known
limit rather than an oversight:

- `shell_snapshots/` holds a shell script codex writes and then sources.
- `plugins/cache/` holds plugin payloads. Scoped to `cache/` and the staging directory,
  so a manifest at the `plugins/` root stays denied.

### The ungated-turn guard

A startup self-test would cost a model call every boot and only prove the hook worked
then. Instead every turn compares its own tool count against how many decisions the
service was asked for. Tools used and nothing asked means the turn ran unsupervised, and
that is logged at ERROR. This is the only place those two facts meet, and codex's
failure mode is silent by construction: an untrusted or crashed hook is treated as no
opinion, so the operator sees an ordinary turn.

### Scope of a grant

Subjects are derived by the daemon from the payload the backend handed it, never from
anything the agent wrote about its own call.

- `bash:<program>` for a simple command, so approving `git` covers a turn of git work.
- `bash-exact:<command>` for anything containing shell control characters or that fails
  to lex. `ls && curl evil.example` must never be covered by a grant given to `ls`.
- `write:<relative path>` inside the workspace, `write-outside:<absolute path>` for an
  escape. Per path, not one shared bucket: approving a write to `~/.zshrc` must not
  authorise the next one to `~/.ssh/config`.
- `fetch:<host>`, and `tool:<Name>` for everything else.

Operator-facing text is flattened to one line and length-capped with the cap announced.
Tool input is entirely agent-authored, so without flattening an agent could draw a fake
verdict line under the real one, and a silent truncation is how you approve the half of
a command you were not shown.

### What is deliberately not done

- **Cron and the background job queue are never gated.** Nobody is watching a cron
  thread, so gating it would fail every tool call.
- **`approvals_reviewer` is not enabled in production.** It spawns a guardian subagent
  per escalation, fed the agent history, returning `{"outcome":"allow"}`. Real spend for
  a decision you never see and cannot override.
- **No codex `app-server` port.** `PermissionRequest` lives there and is the better
  mechanism, but moving off `codex exec` is a separate project.

### Also worth knowing

Seeding never overwrites, so an existing `~/.claude-on-the-fly/config.yaml` will not
grow a `permissions:` block on upgrade. Defaults apply and approvals stay off until you
add one.

`codex` does not start at all under `sandbox.fs: deny-most`; it fails with
`Operation not permitted` before any network call. That is pre-existing and unrelated to
approvals, verified against a baseline with the permission changes stashed.

pty approvals need claude-pty's **tmux** backend, not its `script` one, because the
script backend has no addressable pane. claude-pty picks tmux only when tmux is on PATH
and `CLAUDE_PTY_NO_TMUX` is not `1`.

cotf handles both halves. It sets `CLAUDE_PTY_NO_TMUX=0` in the spawn environment, so an
operator who exports that variable for their own use does not silently lose approvals,
and `check_pty_tmux_for_approvals` refuses at startup when tmux is missing, which cotf
cannot fix for you. If a pane is missing anyway, the relay says so explicitly rather
than stalling in silence, and distinguishes "the session does not exist, so claude-pty
took the script backend" from "the session is live but the prompt did not render as
expected", since those need different fixes.

The check reports ok when approvals are off. The script backend is perfectly fine when
nothing is being gated.
