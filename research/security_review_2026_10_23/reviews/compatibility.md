Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Compatibility objections to provisional repair families

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This is a fourth review round. It does not claim convergence.
The root reviewer retains the minimum review window through 2026-10-22 18:41:03 UTC.

I read `lead-operations-review.md` and froze new vulnerability research.
This round uses source and existing regression coverage only.
I ran no runtime checks, tmux commands, provider operations, credential reads, or exploit payloads.

## Main objection to the provisional grouping

The bounded-repair family contains changes with very different compatibility costs.
Exact approval identities and a known AWS file scheme are local corrections.
Endpoint authentication changes transport contracts across multiple clients and launch paths.
Do not ship those as one undifferentiated hardening change.

Management-plane separation also should not wait for a complete diagnostics feature.
The host terminal socket, trusted launch inputs, and final environment form one concrete authority chain.
Improving operator visibility does not mediate that chain.

The smallest independent corrections can proceed without waiting for terminal redesign.
That does not make a parser admission result proof of credential disclosure.
Evidence labels must remain separate from implementation order.

## Workflow baseline

| Workflow | Current intended behavior | Source |
|---|---|---|
| Chat, Claude native | Claude chooses which calls need questions; cotf relays them | `permissions.py:10` |
| Chat, Claude terminal | Claude renders a dialog; cotf reads it and delivers the selected answer | `permissions.py:623` |
| Chat, Codex | Cotf's hook classifies calls before execution; inspection exemptions reduce questions | `permissions.py:444` |
| Tool approvals off | Approval artifacts do not change normal execution | `permissions.py:838` |
| Unattended jobs | No human tool-approval workflow; capability checks still apply | `config.yaml:290`, `jobs/brokers.py:26` |
| Gated egress | Unknown public hosts need a grant; allow entries skip questions | `sandbox.py:521` |
| Open egress | Any public host works without a question; unconditional refusals remain | `sandbox.py:523` |
| Egress off under jail | No general internet proxy; credentialed model routes can remain usable | `sandbox.py:542` |
| Sandbox off | General egress and command brokers do not start; optional ChatGPT brokerage remains independent | `orchestrator.py:1867` |
| Deliberate file roots | A tool can use paths inside the workspace and configured approved roots | `commands.py:834` |
| Deliberate host grants | Host approval permits an opaque tunnel for that host and port | `egress.py:646` |

Tool approvals and sandboxing are independent settings.
See `orchestrator.py:436` and `orchestrator.py:1859`.
Egress off means no proxy. It does not mean open internet under the jail.
No proposed change should silently change those meanings.

## 1. Precise approval keys

| User workflow | Must remain unchanged | Intentional narrowing | Main compatibility objection |
|---|---|---|---|
| Chat, Claude native | Forward Claude's own question and return its original tool input | Different command or patch source text needs a distinct grant | Do not add cotf classification to this path |
| Chat, Codex | Keep inspection exemptions and the current hook protocol | Syntax-distinct questioned commands stop sharing canonicalized grants | More prompts do not justify restoring lossy identity |
| Chat, Claude terminal | Preserve option selection, refusal messages, and originating conversation | Different complete captured dialogs stop sharing capped-display keys | A complete capture still may not equal complete underlying tool input |
| Approvals off | Keep existing backend flags and no approval service | None | Do not activate approvals as a side effect |
| Unattended jobs | Keep the documented ungated tool workflow | None | Do not add questions that no operator can answer |
| Write and Edit grants | Keep destination-path scope | None in this repair | Exact content hashing would be a separate product behavior change |
| Fetch grants | Keep existing host scope | None in this repair | Full-URL keys would remove intentional host reuse |
| Generic tools | Keep the current explicit tool-name scope | None in this repair | Do not silently convert every tool to full-input scope |
| Gated, open, or off egress | Keep independent host and port grants | None | Tool identity changes must not alter network policy |

The current Bash implementation already rejects program-only grants at `permissions.py:310`.
Exact source hashing therefore changes equivalence reuse, not the existence of standing grants.
Identical input still receives the same key and configured lifetime.
Whitespace, quotes, and line endings can now cause another question.
That cost follows from preserving shell semantics without another shell parser.

The documentation and one test name still describe program grants.
See `docs/explanation/tool-approvals.md:27` and `tests/test_permissions.py:328`.
Their current assertions and implementation use `bash-exact`.
Correct every duplicate description in the same change.

Existing regression coverage:

1. `tests/test_permissions.py:200` checks simple subject shape.
2. `tests/test_permissions.py:207` checks different command subjects.
3. `tests/test_permissions.py:226` checks compound scope beyond the first program.
4. `tests/test_permissions.py:235`, `:256`, `:274`, and `:278` cover deliberate path, patch, host, and generic-tool scopes.
5. `tests/test_permissions.py:476` and `:490` distinguish Codex classification from Claude forwarding.
6. `tests/test_cotf_approve.py:109` checks unchanged returned tool input.
7. `tests/test_approvals.py:90` and `:151` cover cached grants and expiry.
8. `tests/test_permissions.py:990` and `:1068` cover terminal subjects and display caps.

Missing coverage: quote context, significant whitespace, patch content spaces, and equal-length terminal tails.
The current tests do not establish those distinctions merely because they hash nearby strings.

Claim that would be too strong:
“Exact keys guarantee identical effects or make approvals a containment boundary.”
External state can change between identical calls.
The jail, command broker, and provider permissions retain their separate jobs.

## 2. AWS file-argument grammar

| User workflow | Must remain unchanged | Intentional narrowing | Main compatibility objection |
|---|---|---|---|
| Chat through command shim | Keep existing AWS read prefixes and output handling | An outside binary-file input receives the existing path refusal | Do not disable all AWS file inputs |
| Unattended jobs | Keep the same deterministic command policy | The same outside input receives the same refusal without a prompt | Do not convert path refusal into an unattended approval request |
| Native and terminal backends | Use the same broker grammar | None beyond the path decision | Do not implement separate backend-specific path policies |
| Workspace file inputs | Continue to admit ordinary approved fixture paths | None | Absolute workspace paths must remain equivalent to relative ones |
| Explicit file roots | Retain access within configured roots | Paths outside those roots remain refused through the newly recognized scheme | Do not silently shrink or remove operator roots |
| Read-only API prefixes | Preserve the existing method and prefix decisions | None | A file grammar repair must not authorize additional commands |
| Egress gated, open, or off | Keep current selected trusted-tool behavior | None to egress modes | The trusted host command broker is not an ordinary proxy client |
| Sandbox off without command brokerage | Keep direct CLI behavior | None | A parser patch cannot claim protection where no broker mediates the call |

Add supported binary-file parsing before the existing containment decision.
Do not generalize RFC URL authority or percent-decoding rules without checking this tool's grammar.
Do not reject all strings containing a colon as a substitute for parsing.
Preserve the guide's requirement to review additional tools' path syntax.
See `docs/how-to/broker-a-command.md:162`.

Explicit roots grant writing as well as reading under the current contract.
See `docs/how-to/broker-a-command.md:172`.
Do not describe this parser repair as converting those roots into read-only access.

Existing regression coverage:

1. `tests/test_commands.py:477` checks refusal through a synthetic broker request.
2. `tests/test_commands.py:533` checks static symlink containment.
3. `tests/test_commands.py:1406` preserves absolute workspace arguments.
4. `tests/test_commands.py:1425` preserves explicit roots and outside refusal.
5. `tests/test_commands.py:1664` and `:1700` cover introduced paths and allowed roots.
6. `tests/test_commands.py:939`, `:953`, and `:966` cover bundled AWS command policy.
7. `tests/jobs/test_brokers.py:114` covers job workspace tokens and revocation.

Missing coverage: the documented AWS binary-file scheme across those same location decisions.
A pure parser fixture is sufficient to check admission logic.
It is insufficient to prove what the installed CLI later reads or returns.

Claim that would be too strong:
“Cotf exposed the operator credential, and this small parser change proves all file readback is impossible.”
The established result is outside-file admission.
Disclosure remains unverified, and generic tool grammar remains intentionally bounded.

## 3. Authenticated egress and approval endpoints

| User workflow | Must remain unchanged | Intentional narrowing | Main compatibility objection |
|---|---|---|---|
| Claude native approvals | Keep the shared generated MCP configuration and original decision payload | Requests need the selected session capability | Never bake a session token into the shared config file |
| Codex hook approvals | Preserve hook silence on allow and blocking output on deny | Requests need the selected session capability | Missing capability must not become a crashed or permissive hook |
| Claude terminal notifications | Preserve asynchronous notification and host-owned target selection | Notification posts need the selected session capability | Add authentication to both decision and notification paths |
| Tool approvals off | Keep the service absent | None | Do not generate unnecessary endpoint authority |
| Gated egress in chat | Preserve allow entries, host questions, grant lifetime, and session replacement | A discovered port alone no longer opens a tunnel or submits a question | Standard client proxy authentication needs compatibility evidence |
| Open egress in chat | Keep public destinations free of host questions | Caller authentication still applies | Open destinations and unauthenticated callers are separate choices |
| Egress off | Keep the proxy absent | None | Never start a proxy merely to distribute its token |
| Gated unattended jobs | Allowed hosts work; unknown hosts deny without a question | Job proxy requests need their issued capability | Do not reuse a chat capability or create a human prompt |
| Open unattended jobs | Public hosts work without questions | Job proxy requests need their issued capability | Authentication must not accidentally restore gated destination policy |
| Native and hosted children | Receive the selected endpoint and capability | Stale inherited capabilities stop working | Terminal environment staging must preserve the exact selected values |
| Linux jail | Keep fixed broker relay destinations | The relay carries an authenticated protocol | Authentication does not require caller-selected relay destinations |
| Manual operator diagnostics | Keep an explicit supported authenticated test path | Anonymous direct endpoint requests stop working | Document how diagnostics obtain authority without printing it |

This is optional caller-attribution hardening under the intended shared assistant model.
It has broader compatibility cost than the local parser and identity corrections.
The root should not classify it as mandatory hostile-tenant isolation.

Approval authentication can use the existing per-session service lifetime.
The service and grant store survive multiple turns at `orchestrator.py:458`.
A per-turn token is not required merely to keep grants per session.
On `/new`, replace both endpoint authority and the existing store.
See `orchestrator.py:465`.

The generated helper already inherits per-session endpoint variables.
Keep authentication in that selected environment and request headers.
Do not put its value into shared shim files, process arguments, or diagnostic output.
If standard proxy clients require URL user information, define redaction before publishing that form.
It is a capability even if it is not the provider credential.

Retain `NO_PROXY` for local helper traffic.
The proxy's loopback exemptions serve backend startup at `egress.py:488`.
Requiring proxy authentication must not route ordinary local helper traffic into CONNECT handling.

Existing regression coverage:

1. `tests/test_permissions.py:528` checks the decision service transport.
2. `tests/test_permissions.py:1351` checks asynchronous notifications.
3. `tests/test_cotf_approve.py:29`, `:38`, and `:309` cover hook protocol and failure behavior.
4. `tests/test_cotf_approve.py:349` and `:399` cover notification selection and absent endpoints.
5. `tests/test_egress.py:629` and `:642` cover proxy variables and loopback exemptions.
6. `tests/test_egress.py:701` and `:737` cover per-session grant and port separation.
7. `tests/test_orchestrator.py:3338`, `:3395`, and `:3451` cover endpoint reuse, replacement, and combined routing variables.
8. `tests/jobs/test_brokers.py:142` and `:173` cover unattended refusal and relay lifecycle.

All transport fixtures need explicit current capabilities after authentication changes.
Add missing, wrong, stale, and current capability cases for both approval paths and CONNECT.
Keep standard proxy client support a separate compatibility stage.
Unit tests with hand-built headers do not establish browser, SDK, Git, package manager, or model backend support.

Claim that would be too strong:
“Authenticated loopback endpoints isolate hostile users or prevent misuse by the active agent.”
An intended client holds its capability and can use its permitted operations.
Authentication does not inspect tunnel content or repair host management authority.

## 4. Pending policy rechecks

| User workflow | Must remain unchanged | Intentional narrowing | Main compatibility objection |
|---|---|---|---|
| Gated chat, operator waiting | Keep one host question and its configured answer window | A newly forbidden host cannot use an older pending answer | Do not repeat a question after an unrelated host edit |
| Gated chat, allow entry removed during DNS | Follow the normal unknown-host approval path | The old pre-approval no longer applies | A fresh operator grant can still authorize that public host |
| Open chat | Public hosts still need no question | Current never-ask and private-address restrictions apply after DNS | Do not turn open mode into gated mode |
| Gated unattended jobs | Unknown public hosts still deny without a question | Current prohibitions also apply after waits | No job can wait for a nonexistent human gate |
| Open unattended jobs | Public hosts remain usable | Current unconditional restrictions win | Keep job policy independent of chat grants |
| Egress off | Keep no proxy or pending proxy work | None | Do not infer direct internet from off mode |
| Explicit private hosts | Keep the separate private opt-in | Removing the opt-in invalidates a pending private connection | Revalidate the chosen address, not a second DNS answer |
| Established tunnels | Keep the current tunnel lifetime | None in the first repair | Closing every open stream is a separate cancellation feature |
| WebFetch tool grants | Keep their existing host scope | None | Egress current-policy checks must still apply independently |
| Native and terminal backends | Use the same proxy decision | None | Do not depend on the backend's approval format |

A final current-policy check after DNS and operator waits is the sufficient first change.
It should reject current unconditional prohibitions and clear the failed request's stored grant.
See `egress.py:627`, `:639`, `:650`, and `:665`.

Do not add a global policy revision merely to reject a currently forbidden host.
A revision system would also impose historical invalidation semantics.
That stronger policy needs a separate requirement and more lifecycle state.
It can otherwise make unrelated edits interrupt valid work.

The first repair must preserve numeric address pinning.
Rechecking current private-host permission does not require a second hostname lookup.
Existing mixed-answer protection remains valuable at `egress.py:723`.

Existing regression coverage:

1. `tests/test_egress.py:239`, `:259`, and `:285` cover pre-approval, fresh approval, and grant reuse.
2. `tests/test_egress.py:352` covers mixed public and private resolution.
3. `tests/test_egress.py:451`, `:471`, and `:485` cover open mode and unconditional refusals.
4. `tests/test_egress.py:1295` covers stored-grant changes after policy refresh.
5. `tests/test_egress.py:1347` and `:1379` cover separate allow and private permission.
6. `tests/jobs/test_brokers.py:142` covers unattended unknown-host refusal.

Missing coverage: edits while a fake resolver or fake operator gate remains pending.
Add in-memory event-controlled fixtures, plus unchanged-policy and unrelated-edit controls.
No real network destination or provider is necessary.

Claim that would be too strong:
“Policy reload revokes all networking immediately.”
This change addresses pending decisions.
It does not close existing tunnels or undo accepted remote actions.

## Ordering objections and a smaller delivery sequence

The lead's three option families are useful categories, not a sufficient delivery order.
Use dependency and evidence strength to choose each change.

1. Correct inaccurate guarantees immediately, using current evidence labels.
2. Ship small identity and known parser corrections independently, with their focused regression cases.
3. Resolve the terminal management boundary, protected launch inputs, and exact final environment as one coherent work item.
4. Resolve stateless cookie storage and header forwarding against the supported route contract.
5. Add final pending-policy checks when their intended edit semantics are explicit.
6. Add effective-state diagnostics through current resolvers, without blocking the earlier corrections.
7. Defer endpoint authentication until standard-client and terminal environment compatibility requirements are defined.
8. Decide credential lifetime improvements only after authority transport and cancellation requirements are clear.

This order does not assert measured exploitation of the terminal channel.
It gives a source-visible host authority chain proper review priority.
It also avoids making local corrections wait for a broader backend integration change.

Endpoint authentication can move earlier if accidental local access is an explicit deployment requirement.
Its token transport still depends on correct native and terminal environment delivery.
Do not solve that dependency with authentication fallback or anonymous compatibility mode by default.

## Features from comparison projects to defer

| Feature family | Why cotf should defer it now | Evidence that could justify it later |
|---|---|---|
| General signed instructions, signature policy, and rollback | They do not repair command input grammar, approval identity, or host terminal authority | A defined trusted-instruction distribution and update requirement |
| Merkle audit chains and cryptographic event integrity | Existing concrete decisions need correct identities before cryptographic packaging | A stated tamper-evidence consumer and trusted storage model |
| General credential placeholders with provider revision protocols | Cotf already has fixed upstreams and selected route headers | Dynamic provider rebinding or route replacement with explicit credential attachment needs |
| Per-process grants for every credential route | Daemon-lived ordinary provider authority remains an intentional option | A cancellation or retained-capability requirement that existing lifetimes violate |
| A replacement containment engine | Changing Landlock, Seatbelt, or container machinery does not fix broker semantics | A measured enforcement requirement that current platform mechanisms cannot express |
| A separate jail for every trusted host tool | The user explicitly accepts the trusted selected-tool model | A concrete tool whose supported input cannot be safely mediated within that model |
| Full TLS interception | Destination gating needs no request-body decryption | A stated content-inspection or injection requirement with a justified trust-store design |
| A general terminal-control service | Existing host-owned calls may cover every required operation | A checked external wrapper that needs a narrow operation unavailable from the host integration |
| Hostile tenant isolation over shared assistant memory | It conflicts with the stated shared-assistant contract | A new product requirement with separate daemon or storage boundaries |
| Managed cookie federation across routes and aliases | No supported cookie-dependent workflow currently requires that complexity | A concrete provider contract that needs retained cookie state |
| Broad process namespace changes | Cleanup and hosted-terminal behavior have existing process relationships | A precise process-visibility requirement with a tested cleanup design |

These are deferred mechanisms, not claims that comparison projects use them incorrectly.
Borrow a mechanism only when it solves cotf's stated boundary or workflow problem.

The root reviewer selects final proposals after the review window.
