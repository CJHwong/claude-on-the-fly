Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Pane compatibility design review

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This is a third source-only review. It does not claim convergence.
I read `custody-cross-review.md` and `broker-source-review.md` before this review.
I ran no tmux commands, boundary probes, provider calls, or new tests in this round.
I made no repository changes and ran no git commands.

## Recommendation

Use host-only pane management for Codex terminal mode.
The current Codex integration already places the jail wrapper around the final agent command.
Protect the host launch files and establish the complete selected environment before that command starts.
Deny the child access to the host management socket and host launch directory.

For Claude terminal mode, keep a temporary native fallback until its external wrapper supports a checked host-owned launch interface.
Do not move the whole wrapper outside the jail without checking how it launches the actual Claude process.
A per-turn server inside the existing jail is a valid alternative if Claude's terminal features must remain available.
That alternative requires more changes to current pane discovery and cleanup.
It does not require another sandbox for trusted host tools.

This recommendation is conditional on current source, not a demonstrated live pane failure.

## Actual backend matrix

The orchestrator allocates pane metadata before every turn at `src/claude_on_the_fly/orchestrator.py:1111`.
The jobs runner does the same at `src/claude_on_the_fly/jobs/agent_runner.py:227`.
Metadata allocation does not mean every backend launches a terminal.

| Backend and operation | Current execution | Child needs the host pane socket? | Current panes directory consumer |
|---|---|---|---|
| Claude native turn | `claude -p` through `agent._exec` | No | No direct backend consumer |
| Claude native compaction | Native executor | No | No direct backend consumer |
| Claude with Ollama | Native executor with `ollama launch claude` | No | No direct backend consumer |
| Claude terminal turn | Jailed `claude-pty` wrapper | Wrapper currently needs tmux control | Wrapper receives `TMUX_TMPDIR`; its external implementation is not audited here |
| Claude terminal compaction | Same terminal wrapper and PostCompact hook | Same wrapper requirement | Same wrapper requirement |
| Codex native turn | Jailed `codex exec` as a direct child | No | No direct backend consumer |
| Codex terminal turn | Host tmux starts a script, then the wrapped interactive child | Host needs it; final agent has no visible direct management call | Host stages environment, launch script, and diagnostic output |
| Codex with Ollama | Direct `codex exec` through its launcher | No | No direct backend consumer |
| Codex compaction in any mode | Plain executor, with no interactive argv | No | No direct backend consumer |

Claude chooses its executors at `src/claude_on_the_fly/backends/claude.py:421`.
Its native argv starts at `src/claude_on_the_fly/backends/claude.py:670`.
Its compaction mode choice appears at `src/claude_on_the_fly/backends/claude.py:773`.
Ollama uses the native backend constructors at `src/claude_on_the_fly/agent.py:2009` and `src/claude_on_the_fly/agent.py:2025`.
The launcher prefix appears at `src/claude_on_the_fly/agent.py:1576`.

Codex supplies interactive argv only when `_pty` is true at `src/claude_on_the_fly/backends/codex.py:1495`.
The pane branch also requires that argv at `src/claude_on_the_fly/backends/codex.py:1149`.
The final interactive command receives `sandbox.wrap` at `src/claude_on_the_fly/backends/codex.py:1182`.
Compaction deliberately omits interactive argv, according to `src/claude_on_the_fly/backends/codex.py:1124`.

A correction to broad earlier descriptions follows from this matrix.
The unwrapped Codex tmux creation path belongs to terminal mode, not every native Codex turn.
The macOS socket and panes grants still apply independently of that backend choice.
See `src/claude_on_the_fly/sandbox.py:2705`, `src/claude_on_the_fly/seatbelt/jail.sb:50`, and `src/claude_on_the_fly/seatbelt/fs-allow-reads.sb:373`.

## Complete caller inventory

| Caller | Current access and purpose | Requirement under host-only management |
|---|---|---|
| `src/claude_on_the_fly/orchestrator.py:1111` | Chooses the chat pane and publishes its metadata | Retain host pane identity; publish only agent-required environment entries |
| `src/claude_on_the_fly/orchestrator.py:1351` | Ends the chat pane during teardown | Retain exact host target and cleanup |
| `src/claude_on_the_fly/orchestrator.py:1971` | Sweeps stale panes for this frontend | Retain the frontend prefix boundary |
| `src/claude_on_the_fly/jobs/agent_runner.py:227` | Chooses the job pane and publishes its metadata | Retain job identity and host cleanup |
| `src/claude_on_the_fly/jobs/agent_runner.py:320` | Ends the job pane | Retain per-job teardown |
| `src/claude_on_the_fly/jobs/cli.py:281` | Sweeps stale job panes | Retain jobs-only ownership |
| `src/claude_on_the_fly/backends/codex.py:943` | Creates sessions, stages files, taps output, releases startup, and checks completion | Keep all these operations on the host |
| `src/claude_on_the_fly/backends/codex.py:1077` | Deletes staged environment, script, and output files | Retain cleanup on success, failure, timeout, and cancellation |
| `src/claude_on_the_fly/backends/claude.py:1000` | Jails the external wrapper, which manages its terminal | Needs a checked child-launch hook, native fallback, or an inside-jail server |
| `src/claude_on_the_fly/tmux.py:245` | Creates and protects the socket directory | Host owns the directory; filesystem policy must exclude the jailed child |
| `src/claude_on_the_fly/tmux.py:291` | Supplies the external wrapper's socket selection hints | Retain only for paths that still use that wrapper contract |
| `src/claude_on_the_fly/tmux.py:315` | Executes exact-socket capture, resize, liveness, list, and teardown calls | Retain exact socket addressing |
| `src/claude_on_the_fly/tmux.py:411` | Discovers every current pane from the shared server | Unchanged under option A; redesign under option B |
| `src/claude_on_the_fly/permissions.py:1172` | Captures dialogs and sends answers or refusal text | Host resolves the exact owned pane socket |
| `src/claude_on_the_fly/orchestrator.py:513` | Publishes approval endpoints and a terminal session name | Preserve approval endpoints; terminal metadata must reflect the effective mode |
| `src/claude_on_the_fly/tui/screens/dashboard.py:1132` | Finds a pane and copies its operator attachment command | Retain operator attachment for hosted modes |
| `src/claude_on_the_fly/tui/screens/dashboard.py:1742` | Finds, resizes, and captures a pane | Retain transcript fallback when no pane exists |
| `src/claude_on_the_fly/sandbox.py:80` | Passes terminal-wrapper variables through environment filtering | Keep only variables needed by the effective backend |
| `src/claude_on_the_fly/sandbox.py:1638` | Resolves the globally granted host pane socket | Remove this child capability under option A |

The helpers for refusal and unreadable dialogs share the approval caller.
See `src/claude_on_the_fly/permissions.py:1204`, `src/claude_on_the_fly/permissions.py:1227`, and `src/claude_on_the_fly/permissions.py:1263`.
They need host control, not management access inside the agent.

Linux currently hides the data directory without a visible `panes` regrant.
See `src/claude_on_the_fly/sandbox.py:2195` and `src/claude_on_the_fly/sandbox.py:2251`.
Its current hosted behavior therefore needs a compatibility check before any parity claim.
Option A avoids requiring the child to read host staging files on Linux.

## A. Host-only pane management

Classification: boundary fix with targeted compatibility work.
Cost: lowest for Codex; unresolved wrapper integration cost for Claude.

Preserve the shared host server and current session names.
Preserve host capture, resize, approvals, attachment, and teardown.
Keep the wrapped final child and the existing broker model.
Do not create another service or another tool sandbox.

Required changes:

1. Remove the raw socket allow from the child policy.
2. Deny reads and writes to trusted pane launch files explicitly on both macOS bases.
3. Removing a read grant alone is insufficient under the default global-read profile.
4. Resolve the protected directory before policy construction, including supported linked layouts.
5. Keep host pane metadata until the backend selects its launch path.
6. Remove host tmux selection variables from the final child's environment.
7. Do not rely on hidden environment values as the socket access control.
8. Launch the final child with the complete selected environment, including removals.
9. Treat failed environment staging as an unavailable hosted launch.
10. Reuse the native fallback before a child starts, rather than inheriting server state.
11. Use one exact host socket selector for creation, display, approvals, and teardown.
12. Keep trusted launch scripts outside every agent write grant, including late overlay grants.

The existing delta environment is insufficient at `src/claude_on_the_fly/backends/codex.py:770`.
Failure currently continues with server state at `src/claude_on_the_fly/backends/codex.py:782`.
The full environment must reach the final launch without exposing values in arguments.
A host-owned staged file can remain part of that transport.
The launch code must clear inherited server variables before it applies the complete mapping.
A new general broker is unnecessary for this operation.

The approval helper currently uses bare tmux at `src/claude_on_the_fly/permissions.py:1190`.
Its environment retains an unrelated inherited `TMUX` at `src/claude_on_the_fly/permissions.py:1195`.
Correct that target selection in the same change.
Do not preserve accidental routing to an unrelated default server for managed hosted turns.

Regressions to prevent:

1. Removing metadata too early must not make Codex select the plain path accidentally.
2. Removing the directory grant must not block the host shell before it enters the jail.
3. Shared server state must not select an earlier account or restore a removed variable.
4. A staging failure must not start the interactive child and then replay its prompt natively.
5. Approval refusal must still let terminal turns produce their final envelope.
6. Long prompts must remain off the tmux command line.
7. Nudge retries must release their previous session before the next launch.
8. Teardown must keep the Linux relay alive until the jailed child has ended.

The existing lifecycle order appears at `src/claude_on_the_fly/orchestrator.py:1351` through `:1363`.
Preserve that order unless a separate lifecycle change has a demonstrated need.

## B. A server inside the existing jail

Classification: alternative boundary fix, with broader compatibility work.
Cost: higher than A for the current shared-server design.

Start a dedicated server under the same jail that already contains the wrapper and child.
Do not add a second jail around the trusted tool.
Use a distinct server for each live jail policy.
A shared server cannot acquire a new workspace policy from each later client.

For Claude terminal mode, this resembles the present jailed-wrapper launch.
The crucial change is preventing reuse of a server started outside that jail.
For Codex terminal mode, the current host-started server path must change.

Required behavior changes:

1. `Pane` needs the specific server identity, not only a session name.
2. Host display, approval delivery, attachment, and teardown must use that identity.
3. Pane discovery needs a host-owned registry or a bounded directory convention.
4. The current single `list-sessions` scan no longer lists every turn.
5. Startup sweeps need exact server ownership and must preserve other active turns.
6. Linux needs an explicit per-turn socket directory mount and a checked relay lifetime.
7. Shared host launch files must remain outside the child's write surface.
8. Every launched tool inherits the existing jail policy from its server.

The current single-server assumptions appear at `src/claude_on_the_fly/tmux.py:395`, `src/claude_on_the_fly/tmux.py:411`, and `src/claude_on_the_fly/tmux.py:452`.
Both frontend and job cleanup comments still describe older server behavior.
Use executable behavior, not those comments, when defining the replacement lifecycle.

Benefits: the wrapper can keep its terminal management calls within its existing boundary.
Costs: more server processes, more socket paths, new discovery state, and broader cleanup changes.
Per-turn servers can also make startup locking and operator attachment more visible operational concerns.

This alternative is preferable only if required Claude terminal features cannot fit A at reasonable cost.
It needs no generic tenant model and no new process namespace for its basic filesystem argument.

## C. Temporary native fallback

Classification: compatibility bridge during the boundary fix.
Cost: small implementation scope, with visible feature loss.

Codex already falls back before launch when a pane is unavailable.
See `src/claude_on_the_fly/backends/codex.py:1188` and `src/claude_on_the_fly/backends/codex.py:1191`.
Keep this fallback for environment staging and host-control failures.
Do not weaken the jail or approvals to preserve a terminal display.

Claude needs an explicit temporary rule for jail mode.
Its current terminal executor has no equivalent checked host-only child launch interface in this repository.
Fallback should use the native backend with the same selected provider, account, model, and effort.
It must use native approval arguments at `src/claude_on_the_fly/permissions.py:858`.
Disabling permissions is not part of the fallback.

Observable changes:

1. The operator sees the transcript instead of the interactive terminal.
2. Attachment is unavailable for the native turn.
3. Claude reports native progress and usage instead of terminal-only statusline fields.
4. Native Claude may resolve session settings differently, as documented at `src/claude_on_the_fly/backends/claude.py:749`.
5. Claude compaction uses the native path and loses its terminal display.
6. Codex compaction already uses the plain path.
7. A clear message records requested terminal mode and effective native mode.
8. A fallback must not silently change account selection or start a different conversation.

Preserve the requested profile identity when choosing a temporary execution fallback.
Do not edit operator configuration to trigger it.
Check existing session files and Codex mappings across both directions of the transition.
This preserves more continuity than changing the stored profile mode without migration analysis.

If native startup cannot preserve the selected environment, report a failed turn.
Do not use the shared server's environment as a recovery path.

## Existing tests to reuse

| Test source | Existing behavior to preserve | Required adjustment |
|---|---|---|
| `tests/test_codex_backend.py:3254` | Native and terminal argv selection | Add explicit managed-host eligibility |
| `tests/test_codex_backend.py:2937` | Hosted selection and plain fallback | Include failed full-environment setup |
| `tests/test_codex_backend.py:2744` | Long prompt staging | Keep host-only staging and exact prompt bytes |
| `tests/test_codex_backend.py:2812` | Staged file cleanup | Cover every pre-launch failure and cancellation |
| `tests/test_codex_backend.py:3215` | Environment staging failure | Replace the current inheritance expectation with safe fallback |
| `tests/test_codex_backend.py:3345` | Nudge retry cleanup | Preserve session reuse without stale files |
| `tests/test_claude_backend.py:173` | Terminal and native compaction choice | Add requested-mode versus effective-mode cases |
| `tests/test_claude_backend.py:927` | Wrapper envelopes and timeout errors | Reuse only if the wrapper remains supported |
| `tests/test_permissions.py:1545` | Approval target selection | Assert exact socket selection under inherited `TMUX` |
| `tests/test_orchestrator.py:4127` | Pane metadata and cleanup | Keep backend-specific host selection visible |
| `tests/jobs/test_agent_runner.py:657` | Job pane lifecycle | Preserve keyed jobs, unhosted jobs, and cleanup |
| `tests/test_tmux.py:300` | Exact socket addressing | Share this invariant with approval delivery |
| `tests/test_tmux.py:405` | Prefix-scoped startup sweep | Keep this under A; redesign explicitly under B |
| `tests/test_tmux.py:614` | Operator attachment address | Preserve the displayed target for hosted turns |
| `tests/test_sandbox.py:4303` | Current child socket grant | Replace its expected capability under A |
| `tests/test_sandbox_linux.py:158` | Policy mount ordering | Include host-only control path exclusions |

No test above was executed in this round.
Several tests encode current unsafe or accidental behavior.
A green unchanged suite is not the rollout gate by itself.

## Missing benign checks and rollout gates

1. Add pure launch-plan checks for all nine matrix rows and both producers.
2. Check exact selected environment with harmless markers, removals, and daemon-equal values.
3. Check that empty-environment initialization preserves quoted values without exposing them in arguments.
4. Check that a staging failure selects fallback before any interactive child starts.
5. Check that final child arguments retain the intended existing jail wrapper.
6. Check explicit host-only denies under both macOS bases and Linux mount construction.
7. Check host approval, capture, and teardown select the same socket under unrelated inherited tmux variables.
8. Check unchanged account, workspace, session UUID, and resume mapping during fallback transitions.
9. Check native approvals remain active and terminal-only hooks do not block a native turn.
10. Check attachment and transcript display accurately report the effective mode.
11. Check terminal launch, output, resize, refusal, timeout, cancellation, and cleanup with inert children when authorized.
12. Keep every live boundary check separate from benign compatibility checks and obtain the required authorization first.

A future release needs a clean boundary between old and new agents.
Existing agents retain the policy that permitted their host socket access.
A next-turn configuration update does not revoke that existing process policy.
Stop old producers and finish their old turns before claiming the new host-control boundary.
Coordinate frontend and jobs producers so a shared server does not retain an old authorized client.
No live restart or cleanup is authorized by this design review.

Rollout recommendation:

1. Correct the boundary documentation before release.
2. Ship A for Codex terminal mode after its benign launch and environment tests pass.
3. Use C for jailed Claude terminal mode until its wrapper launch contract has a source review.
4. Keep the existing `off` and `env` behavior unless the operator explicitly selects the jail change.
5. Evaluate B only if required terminal functionality justifies its broader lifecycle work.
6. Do not advertise live enforcement proof until authorized platform checks complete.

## Egress reload: challenge the global generation proposal

A single generation number for every policy and session is unnecessary for the present contract.
`src/claude_on_the_fly/egress.py:624` promises refresh on the next CONNECT.
It does not promise cancellation of every decision already awaiting an answer.
The minimal compatible choice is to document that snapshot boundary and leave existing tunnels alone.
Under that choice, the reported pending-request race is optional hardening.

If narrowing edits must invalidate pending decisions, keep the mechanism local to egress authority.
The relevant decision inputs are the host, port, allow status, never-ask status, and private-address exception.
A revision local to that policy can fence old pending approvals.
A host-specific snapshot can avoid invalidating a request because an unrelated host changed.

Do not add a global counter across cookies, command policy, account routing, and pane management.
Those authorities have different consumers and lifetimes.
Such a counter would couple unrelated reloads and create needless approval retries.
It would also need ownership rules across independent proxy objects and daemon processes.
None of those requirements follows from the current egress race.

A final connection check alone is incomplete if stale grants must never survive.
`ApprovalBroker._invoke_gate` stores the grant at `src/claude_on_the_fly/approvals.py:378`.
Pending requests also coalesce by the existing request key at `src/claude_on_the_fly/approvals.py:318`.
The stale-grant check therefore belongs before grant commit, with a matching pending-request identity.
A late caller must not join an old decision merely because its host string matches.

Choose the intended semantics before adding state:

| Contract | Smallest useful mechanism |
|---|---|
| New CONNECT reads current policy; pending decisions may finish | Document the snapshot point; no generation counter required |
| Pending decision must satisfy current effective policy | Refresh after awaits and check current host rules before use and grant commit |
| Any observed narrowing invalidates its earlier pending decision | Use a local monotonic revision and fence both grant commit and pending-request joining |
| Every global edit revokes every pending request and tunnel | Broader architecture and a new operator contract; out of scope for this bounded fix |

Returning to an identical effective policy need not invalidate a request under the second contract.
A monotonic revision matters only if the third contract is intended.
Existing `_refresh_policy` removes stored grants at `src/claude_on_the_fly/egress.py:676`.
It does not automatically observe edits during an idle approval wait.
A generation counter alone does not add that observation either.

Meaningful benign tests use a paused fake resolver or approval gate.
Edit the synthetic policy, trigger or perform refresh, and release the gate.
Check current decision, stored grant, and a second concurrent request separately.
Also check unrelated-host edits, unchanged policy, private-exception removal, and policy restoration.
Reuse `tests/test_egress.py:1295` and existing approval coalescing tests.
No global generation system is justified before those semantics and tests exist.

## Skeptical compatibility pass for the whole agent

This pass freezes new vulnerability discovery and challenges the proposed fixes against existing consumers.
It adds no runtime claims.

### Initial server state and exact child environment

A host-owned server is still initial state from an earlier launch.
Do not assume that hiding its socket repairs its inherited environment.
The full environment must be selected at the final child launch, not only at the new-session client.
The current code records this distinction at `src/claude_on_the_fly/tmux.py:21`.
The current delta file fails to represent removals at `src/claude_on_the_fly/backends/codex.py:770`.

The compatibility check needs two harmless simulated server environments.
Include an old value absent from the next mapping and a new value equal to the daemon default.
The final child must receive exactly the intended selected mapping, apart from documented runtime additions.
Check values containing spaces, quotes, newlines, empty strings, and non-ASCII text.
Check the handling of environment names that a shell cannot export.
No value belongs in process arguments or diagnostic logs merely to simplify this transport.

A persistent server can remain shared under A because it supplies a terminal rather than account state.
This argument fails if the final launcher still inherits unspecified server variables.
A new server for every turn alone also fails if its initial environment is wrong.
Under B, the server must start from the selected environment and the existing jail before any terminal child starts.

### Native versus terminal behavior is a product contract

Native fallback preserves an execution path that already exists.
It does not preserve every feature of a terminal turn.
Claude's comments explicitly distinguish interactive settings and compaction behavior at `src/claude_on_the_fly/backends/claude.py:749`.
The native and terminal approval paths also differ at `src/claude_on_the_fly/permissions.py:838`.

A compatibility claim must therefore include final text, approvals, resume, compaction, usage, and visible terminal state.
A successful final reply is insufficient evidence that every terminal feature survives.
The operator must see that the requested terminal mode used native execution.
The fallback must not disable approvals or change the selected provider identity.

For native modes, metadata propagation is not evidence of a genuine terminal requirement.
Remove unnecessary child capabilities only after checking the final execution path.
Keep host pane identity available until host selection and teardown finish.

### Runtime caches need measured placement, not a blanket deny

Source grants still include shared plugin caches and shell snapshots.
See `src/claude_on_the_fly/sandbox.py:1716`, `src/claude_on_the_fly/sandbox.py:2208`, and `src/claude_on_the_fly/sandbox.py:2221`.
Those grants have compatibility history. They cannot be removed safely from names alone.
The Codex state comment explains why freezing unknown files broke resume at `src/claude_on_the_fly/sandbox.py:2496`.

A private cotf cache can prevent later host sessions from consuming turn-written payloads.
That proposal needs a supported placement mechanism for each backend.
Do not assume that both CLIs offer a cache-only relocation option.
Moving the entire configuration directory also moves settings, login selection, hooks, plugin manifests, and session state.
The repository records a Claude onboarding regression from configuration-directory changes at `docs/agent/security-findings.md:64`.

The smallest supported changes remain:

1. Keep measured state writes that the current backend needs.
2. Keep trusted host launch inputs outside those writes.
3. Describe writable plugin payloads honestly until supported private caches exist.
4. Use immutable shared plugin payloads only if updates can occur through an explicit host-owned workflow.
5. Do not claim that a denied manifest makes its existing payload immutable.

A cache migration needs synthetic plugin fixtures for read, update, startup, and resume behavior.
It also needs separate account and configuration checks before any broader rollout.
No backend account call or cache execution is performed in this review.

### Narrow fixes and product choices

| Proposal | Relationship to the current model | Decision needed |
|---|---|---|
| Hide the host pane socket and launch files from final children under A | Preserves trusted host terminal management | Compatibility gates for the external Claude wrapper |
| Establish the complete selected child environment | Preserves existing account and environment selection intent | Safe failure behavior must replace inheritance |
| Use exact socket addressing for managed approvals | Preserves the intended approval target | Preserve any separately documented unmanaged path explicitly |
| Correct persistence, platform parity, and lifetime wording | Preserves behavior | No new architecture |
| Add targeted Keychain service denies | Extends the existing credential custody intent | Required Security framework consumers need compatibility evidence |
| Move shared plugin caches to private cotf storage | Preserves the desired host persistence boundary | Supported placement and update behavior need a product choice |
| Temporarily use native execution for a requested terminal mode | Preserves confinement and usable agent execution | Visible feature loss and settings differences need an explicit release decision |
| Use one inside-jail server per live policy | Preserves the existing jail rather than adding another | Discovery, performance, attachment, and cleanup changes need a product choice |
| Restrict every process and signal channel | Changes an accepted current capability | Justify it from account secrecy or another concrete requirement |
| Revoke pending approvals on every policy edit | Strengthens the current next-CONNECT contract | Choose the intended invalidation semantics first |

### Assurances to withdraw or qualify pending evidence

1. Withdraw any claim that every backend mode currently uses the same terminal containment path.
2. Withdraw any claim that a path-scoped host socket is automatically a restricted management interface.
3. Withdraw any claim that mode `0600` excludes a same-user jailed process with an explicit path grant.
4. Qualify the claim that global plugins or other executable payloads cannot persist through writable caches.
5. Qualify Linux parity claims for dotenv files, socket visibility, and terminal staging.
6. Do not state that a file-read deny proves every Keychain service channel is closed.
7. Do not equate hidden provider tokens with the absence of usable broker capabilities.
8. Do not describe environment delivery as secret isolation without checking the applicable process channel.
9. Keep accepted shared memory and transcript behavior distinct from credential custody assurances.
10. Describe startup probes as evidence for their tested paths and operations, not every authority channel.

## Targeted Keychain service compatibility design

Priority: high for credential custody review.
Confidence: high for the file/service distinction; current-host effect remains unverified.
Classification: candidate boundary fix, with required compatibility work.
No Keychain query, Security framework call, service lookup, or real credential operation is executed here.

Apple describes Security Server as a daemon for Keychain access and root certificate trust management.
Its APIs can request cryptographic work while keys remain in another process.
Apple also describes a separate Security Agent for authentication dialogs.
See [Security Server and Security Agent](https://developer.apple.com/library/archive/documentation/Security/Conceptual/Security_Overview/Architecture/Architecture.html).
This archived architecture establishes the distinction between file access and service-mediated work.
It does not establish the behavior of cotf's current profile on the current host.

Pinned nono denies specific security-service Mach lookups after its general lookup allow.
See `crates/nono/src/sandbox/macos.rs:555` through `:579` in the supplied snapshot.
Cotf permits `mach*` in both bases and separately denies Keychain files.
See `src/claude_on_the_fly/seatbelt/fs-allow-reads.sb:26`, `src/claude_on_the_fly/seatbelt/fs-deny-most.sb:41`, and `src/claude_on_the_fly/seatbelt/jail.sb:58`.
Neither comparison alone proves credential disclosure.

The required consumer inventory should precede any rule change:

| Consumer | Intended location | Required check |
|---|---|---|
| Credential broker's selected Keychain lookup | Trusted daemon outside the jail | Targeted child service denies must leave this existing host path intact |
| Claude subscription access-token handoff | Daemon reads; child receives the selected access token | Preserve selected-token delivery without restoring child Keychain authority |
| Codex brokered login | Trusted broker refreshes its protected file | It should not require an agent Keychain exception merely because the broker exists |
| Public certificate trust for model and proxy TLS clients | Inside the child runtime | Inventory the actual TLS and certificate APIs used by each installed backend |
| Corporate certificate or client-certificate workflows | Deployment dependent | Determine whether the child requires trust lookup or private-key authority |
| Package verification and local code signing | Tool dependent | Separate public validation from private signing-key operations |
| Authorization dialogs | Generally not part of unattended agent operation | Identify any required interactive workflow before denying its service path |

The first two cotf flows appear at `src/claude_on_the_fly/sandbox.py:113` and `src/claude_on_the_fly/sandbox.py:152`.
The broker remains a trusted host component throughout this proposal.
No additional sandbox around that broker follows from targeted child service denies.

Source-only validation can establish the proposed rule order and the expected caller split.
Mocked tests can establish daemon lookup, selected token delivery, and error behavior.
They cannot establish that a particular Security framework operation works under a real service deny.

A later authorized compatibility stage should use a fresh operating-system account or an isolated test machine.
It should use synthetic keys and certificates, with no operator Keychain or provider account.
Check one targeted service-deny group at a time against the complete required-consumer matrix.
Include public certificate verification, expected login handoff, and failure without interactive dialogs.
Keep private-key signing disabled unless the product explicitly authorizes that child capability.
Check both filesystem bases and final runtime profile ordering.

If required certificate trust breaks, identify the exact required operation before adding an exception.
Do not restore broad credential service access merely to silence an unrelated startup error.
Conversely, do not retain a service deny that silently breaks a supported trust workflow.

Release gating needs two distinct results.
First, the intended credential custody rule must hold for the synthetic store.
Second, all required public trust and agent startup consumers must remain usable.
Until both exist, report a relevant unresolved candidate rather than a confirmed bypass or completed fix.
