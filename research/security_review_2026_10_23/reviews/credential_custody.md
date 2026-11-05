Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Custody design review

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This third round ranks design choices against their current consumers.
It does not claim convergence before 2026-10-22 18:41:03 UTC.

I read `containment-source-review.md` and `broker-source-review.md`.
I checked the lifecycle source for all four services below.
I did not run tmux, token readback, credential reads, or boundary probes.
I made no repository changes and ran no git operations.
One later lifecycle check used mocked callbacks only.
It made no socket or terminal calls.

## Evidence and severity labels

**Source verified** means the cited source establishes the stated flow or missing check.
**Synthetic confirmed** means a harmless marker fixture proves the stated behavior.
**Runtime unverified** means no permitted runtime check establishes the claimed boundary effect.

**High priority** identifies a source flow across the intended host boundary.
It is not a claim of a demonstrated exploit.
**Medium priority** identifies a demonstrated state or identity defect with an unproved provider consequence.
**Optional** identifies a narrower contract that the current design does not clearly promise.

## Precise custody table

| Service | Caller secret and lifetime | Binding and intended authority | Rotation and revocation | Live request behavior |
|---|---|---|---|---|
| Credential broker | One ordinary path token per broker object; one additional token per isolated account route. Production objects normally follow daemon lifetime. No clock expiry. | Ordinary token reaches configured ordinary routes. Isolated token reaches its selected route. Destination, method set, and exact tail set constrain calls. Request bodies and queries remain caller-controlled. No workspace or process binding. | New broker objects create new tokens. No public method revokes one account token. `stop()` closes the listener and client session. It does not regenerate stored tokens. Restarting the same object therefore differs from creating a new daemon instance. | Credential injection occurs for each admitted request. Dynamic sources can recover once after 401. No turn registry cancels that request when its issuing turn ends. `stop()` delegates handler cleanup to the web runner, then closes the client session. Local cancellation cannot undo an accepted remote action. |
| Command broker | Production issues a fresh token for each turn or job. The token has no clock expiry; explicit teardown removes it. A separate object-lived base token exists for local diagnostics and tests. | The token map binds production tokens to a canonical workspace. It grants configured command operations, subject to prefix, readback, path, and cwd checks. It does not select the model account or prove which process sent the request. Host tool credentials remain the daemon's authority. | Turn and job `finally` blocks call `revoke_token()`. `stop()` clears the workspace map. A later turn receives a new token. The private base token remains on the object and is not published to agent environments. | Revocation removes admission authority for later requests. It does not look up or cancel a subprocess already launched for that token. The subprocess receives an independent process group and a bounded run timeout, normally 120 seconds. |
| Egress proxy | No bearer token. Endpoint reachability selects the proxy. A chat proxy persists for its conversation session. Jobs receive a proxy for each job. | The proxy's host policy and grant store belong to that session. An admitted CONNECT opens an opaque byte tunnel to a pinned address and requested port. It injects no provider credential. Caller identity is not authenticated by the HTTP headers. | A new conversation session replaces its proxy. Job completion stops its proxy. Host policy reload can remove stored grants. No bearer rotation exists because no bearer exists. | Grant expiry and policy edits affect admission decisions, not established tunnel bytes. `stop()` explicitly cancels all tracked handlers and closes tunnels. Normal chat turn completion leaves the session proxy alive. |
| Tool approval service | No bearer token. Endpoint reachability selects the service. One service and grant store persist for a conversation session. | The service fixes workspace, conversation, and terminal session at construction. It returns allow/deny decisions or relays a terminal dialog. The frontend decides who may answer. Caller `source` selects a convenience filter. The service does not itself run the requested host command. | Conversation session replacement stops the old listener and creates a new service. Timing edits affect subsequent questions. No bearer rotation exists. Existing grants keep their recorded expiry. | `/decide` requests wait through the approval gate. `/notify` creates detached relay tasks. `stop()` closes the web runner but does not explicitly cancel those detached tasks. No turn generation check appears before delayed terminal answers. |

Credential broker evidence:

1. Token creation: `src/claude_on_the_fly/broker.py:332` and `:333`.
2. URL and account route selection: `broker.py:359` and `:489`.
3. Scope and request construction: `broker.py:427`, `:532`, and `:639`.
4. Cleanup and request retry: `broker.py:396` and `:578`.
5. Shared operator route policy: `orchestrator.py:1838` and `:1883`.
6. Account selection checks: `codex_auth.py:106` and `:437`.

Command broker evidence:

1. Object token and workspace map: `commands.py:1047`.
2. Production token issuance: `commands.py:1063` and `orchestrator.py:1134`.
3. Revocation: `commands.py:1088`, `orchestrator.py:1362`, and `jobs/brokers.py:129`.
4. Private base token handling: `commands.py:1077` and `:1181`.
5. Subprocess environment and launch: `commands.py:1327` and `:1338`.
6. Run timeout: `commands.py:70` and `:1355`.

Egress evidence:

1. Conversation session lifetime: `orchestrator.py:394` through `:415`.
2. Job proxy lifetime: `jobs/brokers.py:110` and `:131`.
3. Unauthenticated headers: `egress.py:557` and `:561`.
4. Address selection and tunnel: `egress.py:584`, `:592`, and `:614`.
5. Handler cancellation: `egress.py:433` and `:469`.
6. Grant invalidation on policy reload: `egress.py:667`.

Tool approval evidence:

1. Conversation session lifetime: `orchestrator.py:451` through `:488`.
2. Fixed workspace and terminal target: `permissions.py:510` and `:519`.
3. Caller classification and decision: `permissions.py:547` and `:590`.
4. Endpoint registration and stop: `permissions.py:600` and `:615`.
5. Detached relay lifetime: `permissions.py:711` through `:731`.
6. Delayed answer delivery: `permissions.py:643` through `:657`.

All short source paths above are under `src/claude_on_the_fly/`.

## Credentials and grants are different lifetimes

The path token is not the upstream provider credential.
Keychain routes load their credential at broker startup and hold it in `_creds`.
See `broker.py:366`.
Those credentials do not automatically follow later Keychain rotation.
Dynamic ChatGPT sources instead read their profile on each request, with a file-stat cache.
See `codex_auth.py:247` and `:276`.
Their refresh logic can rotate upstream tokens without changing the route capability.
See `codex_auth.py:298`.

`Broker.stop()` clears `_creds`, but it does not securely erase Python strings.
It also does not explicitly erase each dynamic source's cached content.
The claim should be listener revocation and ordinary object cleanup, not secure memory erasure.

| Grant | Store scope | Default duration | Exact admitted scope | Effect of expiry or removal |
|---|---|---|---|---|
| Credential route scope widening | One daemon-wide route approval store | 3600 seconds | A method on one route, or one tail on that route | A later scope miss asks again or denies. No cancellation of an admitted upstream call. |
| Egress host approval | One conversation session, or one job | 3600 seconds | Canonical host plus requested port | A later CONNECT asks again or denies. An established tunnel remains until closed or proxy stop. |
| Tool approval | One conversation session | 1800 seconds | Requester's subject key, normally a command, path, patch, or dialog identity | A later decision asks again. Work already approved remains independent of grant removal. |

Sources: `approvals.py:45`, `:49`, `:75`, `:148`, `:163`, and `:378`.
Route subjects appear at `broker.py:440` and `:453`.
Host subjects appear at `egress.py:646`.
Tool defaults appear at `permissions.py:67` and `:68`.
Tool subject construction appears at `permissions.py:337`.

The tool identity defects in the broker review weaken the last table's exactness.
A cached grant can be no more precise than its subject identity.
Longer random endpoint tokens do not repair a lossy approval digest.

## Failure behavior needs qualified language

Once an approval gate runs, frontend errors and timeouts deny.
See `approvals.py:353` through `:365`.
Cancellation settles joined requests without adding a grant at `approvals.py:332`.

Invalid permission configuration has a different contract.
`permissions.configured()` returns default mode `off` after a parse error.
See `permissions.py:179` through `:187`.
`SessionPermissions.env_for()` then returns no approval wiring at `orchestrator.py:455`.
The source calls this compatibility behavior, not fail-closed enforcement.
An agent-controlled configuration change is not established here.
This still matters when an operator expects `ask` to remain strict after a timing edit.

Likewise, loss of the Codex pane environment is an explicit fallback to the server's inherited environment.
See `backends/codex.py:782`.
The marker checks establish why that fallback cannot guarantee the selected environment.
The existing unhosted fallback can preserve the selected environment more faithfully.

## Ranking the custody proposals

| Rank | Proposal | Severity and evidence | Class | Minimal outcome |
|---|---|---|---|---|
| 1 | Separate the agent from host tmux management and trusted launch files | High priority, source verified; live boundary impact remains unverified | Boundary fix | Keep the existing shared view if desired. Deny direct agent management access and protect pre-jail launch inputs. |
| 2 | Establish the exact selected environment at pane launch | High priority for custody, synthetic marker behavior confirmed; credential consequence unverified | Boundary fix with compatibility work | Remove stale inherited values. Use the existing safe fallback if environment setup fails. |
| 3 | Preserve exact approval identity | Medium priority, pure identity collisions confirmed in the peer report | Boundary fix | Hash complete original input. Keep display cleanup outside identity. |
| 4 | Bind delayed terminal answers to one current pane instance | Mocked lifecycle defect confirmed; live pane delivery remains unverified | Candidate boundary fix | Do not deliver an old answer to a replacement pane merely because its name matches. |
| 5 | Separate cookie state between isolated account routes | Medium priority for state separation, synthetic confirmed; provider security severity undetermined | Boundary fix for existing account state separation | Preserve same-route continuity while removing automatic cross-account cookie sharing. |
| 6 | Correct the custody and revocation documentation | Low implementation risk, source verified | Compatibility and documentation | State the actual four service contracts and grant lifetimes. |
| 7 | Add authentication to session egress and approval endpoints | Source verified absence; impact depends on endpoint reachability and intended session separation | Optional hardening unless stronger attribution is promised | Port discovery alone cannot choose another session's service. |
| 8 | Expire ordinary provider capabilities after each turn | Current daemon lifetime is intentional in source | Optional hardening | Reduce retained authority if the operator selects that new lifetime contract. |
| 9 | Cancel already-admitted broker work when a turn stops | Current revocation usually fences later admission | Optional lifecycle hardening | Track active work if stronger local cancellation is required. Never promise remote rollback. |

The tmux priority concerns its management interface, not the absence of another sandbox around trusted tools.
Per-turn host tmux servers alone do not reduce that interface to selected operations.
The containment review also notes startup-order uncertainty and different Linux reachability.
Those limits remain in force.

The delayed terminal answer candidate deserves a focused design review.
The service persists across turns, while its terminal name comes from the conversation session.
See `orchestrator.py:459`, `orchestrator.py:482`, and `permissions.py:1295`.
An outstanding relay can retain an old dialog across an approval wait.
No current turn or pane generation check appears before `answer_dialog()`.
I checked the delayed callback with mocked dialog and answer functions.
The request, web runner, and its cleanup method were mocks.
The gate paused until after `PermissionService.stop()` returned.
The detached relay remained pending, then invoked the mocked answer callback after release.
This proves the missing stop fence in that lifecycle.
It does not prove live terminal input or delivery to a replacement turn.
No actual terminal, socket, account, or credential participated.
Cancelling detached relays at teardown and checking current pane identity would address different parts of that lifecycle.

The focused check ran through the repository's isolated test harness:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests .venv/bin/python -m pytest -q -c pyproject.toml -p no:cacheprovider -p conftest <review_scratch>/test_custody_protocol_choices.py -k stop_does_not_fence
1 passed, 4 deselected in 10.58s
```

## Cookie choice: revise the first recommendation

My first report favored a globally disabled jar.
The peer review confirms that this alone does not control cookie headers.
The compatibility evidence also does not establish that every supported route is stateless.
I therefore separate the smallest compatible correction from the simpler eventual design.

### First step: separate existing account state

Allocate separate client session state for each isolated account route.
Keep the default ChatGPT route outside every named account's jar.
Preserve existing same-route cookie continuity and existing header behavior in that first change.
This repairs the demonstrated proxy state mixing without claiming complete alternate-credential custody.

One session per route is a conservative separation.
Do not merge routes merely because they share an upstream hostname.
Aliases of one account may retain separate jars without weakening custody.
Shared alias state is not required unless a consumer demonstrates that need.

Account identity changes within a route need an explicit reset rule if managed cookie state remains.
Using the route prefix forever can retain state from a replaced login.
Using every refreshed access token as the identity would discard state during routine rotation.
The current `HeaderSource` protocol exposes headers and recovery, not an account identity epoch.
See `broker.py:130`.
Do not invent a generic identity-merging scheme without a required cookie-dependent consumer.

This limits the first-step recommendation.
Separate jars are the compatibility-first choice, not a complete proof of managed identity semantics.
Their rollout must check login replacement and refresh as separate events.
If no safe replacement rule exists, a stateless policy avoids this retained-state problem.
That choice still needs the provider compatibility checks below.

This first step adds session lifecycle work.
Every created session needs cleanup, including partial startup failures.
Connection pool sharing is optional; it should not complicate the first correction.

### Eventual simpler choice: verified stateless routes

Use `DummyCookieJar` where supported calls need only explicit injected headers.
That removes cookie storage and the identity-reset problem.
It also removes same-route continuity, which is the compatibility change to assess.

The built-in Anthropic route uses header authentication at `broker.py:648`.
The ChatGPT source injects bearer and account headers at `codex_auth.py:269`.
Existing successful ChatGPT measurements used the current shared jar.
See `docs/agent/broker.md:102`.
They cannot prove compatibility with a disabled jar.
No real provider check or private endpoint cookie contract was available in this review.

### Header policy remains separate

| Choice | Stored cookie effect | Caller `Cookie` effect | Response `Set-Cookie` effect | Recommendation |
|---|---|---|---|---|
| Separate jars, existing headers | Stops automatic cross-account storage sharing | Still passes | Still exposes | Most compatible first correction for the demonstrated defect |
| `DummyCookieJar`, existing headers | Stores nothing | Still passes | Still exposes | Suitable only as a storage policy; do not label it full cookie custody |
| Separate jars, strip both headers | Keeps provider state inside its selected jar | Rejects alternate caller cookie authority | Keeps returned cookies in the broker | Managed route option if cookie continuity is required and compatible |
| `DummyCookieJar`, strip both headers | Stores nothing | Removes caller cookies | Removes returned cookies | Simplest final contract for a verified stateless credential route |

The harmless protocol tests confirm the middle two header facts.
Neither a disabled jar nor response-only filtering controls all three channels.
No real ChatGPT takeover is demonstrated.
An affinity cookie and an authentication cookie do not have the same security consequence.

The route contract must state whether authentication belongs exclusively to injected headers.
If that is the contract, cookie stripping is a justified separate change after compatibility checks.
If a custom route intentionally forwards client cookies, the custody assurance must state that exception.
A general cookie configuration system is unnecessary until a concrete route needs more than these defaults.

## Credential URL lifetime: intentional today

The orchestrator explicitly shares provider routes because they are operator policy.
See `orchestrator.py:1883`.
Jobs follow the same design at `jobs/brokers.py:34`.
Provider traffic can remain shared even when command execution uses turn-scoped workspace tokens.
Those lifetimes solve different authorization problems.

Per-turn credential tokens would reduce the usefulness of a retained URL after completion.
They would not prove request origin or stop another caller during the valid interval.
A process identifier in a grant does not authenticate a loopback caller.
Moving a value between argv and environment does not establish secrecy by itself.
This round performs no readback checks and makes no transport secrecy claim.

For ordinary provider routes, preserve daemon lifetime unless the user chooses a narrower contract.
For named accounts, preserve the stated rule that selecting one account does not silently select another.
Delivery, retained authority, and account selection are separate design questions.
Do not use token expiry as a substitute for fixing shared state or trusted launch inputs.

## Rollout acceptance criteria

1. Document the current lifetime table before describing any proposed stronger lifetime.
2. Keep selected host tools trusted and retain their existing operation policy.
3. Protect host tmux launch inputs and use one exact host target for launch, display, approvals, and cleanup.
4. Check that the actual agent child receives its intended jail wrapper.
5. Prove exact environment equality with harmless variables, including absent values and values equal to daemon defaults.
6. Make staging fail and check that the selected safe fallback preserves the intended environment.
7. Check two isolated account cookie stores on a hostname-based synthetic server.
8. Preserve same-route continuity in the compatible first step.
9. Check request cookies, response cookies, and broker storage independently.
10. Check account replacement before enabling a managed jar contract across login changes.
11. Preserve compression, streaming, redirects, 401 recovery, and account header replacement.
12. Check exact approval identities with harmless complete strings and complete patch text.
13. Check a delayed synthetic dialog result against an expired pane generation without sending terminal input.
14. Check later-request revocation separately from active command, active stream, and established tunnel behavior.
15. Keep platform-specific unverified cases visible. Pure policy tests do not establish kernel enforcement.
16. Require separately authorized provider compatibility checks before adopting a new stateless private-provider contract.

These are acceptance criteria for later implementation.
They do not authorize new live probes during this review.

## Unresolved user design choices

1. Should ordinary provider capabilities remain daemon-lived or expire with each turn?
2. Does stop mean no new work, prompt local cancellation, or both?
3. Must all provider authentication remain in the broker, including authentication cookies?
4. Which custom routes require cookie continuity or deliberate cookie forwarding?
5. Should an invalid `ask` configuration stop the turn, retain the last valid policy, or use today's disabled fallback?
6. Must terminal approval results bind to a single turn, even while standing grants remain session-wide?
7. Should the shared pane address remain an operator feature, or can each jailed turn own its own server?

Those choices do not prevent this review from ranking the existing boundary fixes.
The source and benign checks support the recommendations above within their stated limits.
