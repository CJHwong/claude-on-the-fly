Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Broker design and severity review

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This is a third review round. It does not claim convergence.
The earliest convergence remains 2026-10-22 18:41:03 UTC.

I read `containment-source-review.md` and `custody-cross-review.md`.
I checked their relevant claims against current source and focused tests.
I performed no new runtime checks in this round.
I did not run AWS, tmux, credential reads, exploit commands, or live boundary probes.

## Decision order

1. Repair concrete authority and identity mismatches before adding shorter capability lifetimes.
2. Keep the command parser fix limited to supported input grammar.
3. Preserve exact input for Bash and patch approval identities.
4. Keep deliberate path, host, and generic-tool approval scopes separate from exact-call scopes.
5. Recheck current egress restrictions after waits. Do not introduce global policy revisions without another requirement.
6. Keep trusted terminal management on the host. Protect its scripts and final environment together.

## Parser and approval findings

### P1. Binary file input falls outside the command guard's grammar

Exact contract:
`commands.py:839` states that host-absolute and escaping paths receive a refusal before process creation.
`docs/how-to/broker-a-command.md:68` makes the same claim.
The guide also limits the parser to named shapes at `docs/how-to/broker-a-command.md:162`.
That caveat prevents a claim that cotf implements every possible command-line grammar.
It does not explain why a bundled tool's documented file scheme remains outside coverage.

Actually proven:
The bundled AWS policy permits ordinary read prefixes at `config.yaml:239`.
The guard's introducer list contains `@` and `file://` at `commands.py:716`.
The prior pure-function check admitted a synthetic outside path using `fileb://`.
It also confirmed that no readback refusal matched that argument.
No file opened during that check.
Installed help and primary documentation describe AWS binary file inputs.

Merely possible:
A real installed AWS argument might load that outside file.
Its output or error might expose those contents.
This review did not establish either behavior for that argument and version.
Do not label this as demonstrated credential disclosure or arbitrary host execution.

Smallest compatible fix:
Recognize the AWS binary file prefix before the existing path checks.
Reuse the current workspace and explicit-root decisions.
Do not add a universal command parser, another sandbox, or a global unknown-scheme ban.
Do not assume binary file prefixes share every authority or decoding rule of an RFC file URL.
Check the supported tool's grammar before adding those transformations.

Severity and class:
Verified parser coverage defect, with unverified disclosure impact.
Classify it as a boundary fix for the existing path restriction.
Reserve a higher disclosure severity for evidence that a supported invocation exposes protected contents.

Acceptance criteria:

1. The guard rejects a synthetic outside path through each supported binary file spelling.
2. The guard accepts an ordinary fixture file inside the workspace.
3. Explicit roots retain their current grant behavior.
4. Ordinary remote identifiers and unrelated colon-containing strings retain their current behavior.
5. The bundled policy and guide name the supported file shapes consistently.

### P2. Bash approval identity loses shell syntax

Exact contract:
`permissions.py:310` assigns every command its complete canonical arguments or complete compound shell text.
It explicitly rejects program-only grants at `permissions.py:311`.
`permissions.py:338` describes the subject as the precise scope of approval.

Actually proven:
`permissions.py:315` converts simple shell text through `shlex.split()` and `shlex.join()`.
That transformation removes quote context before hashing.
The other branch flattens compound shell text before hashing at `permissions.py:316`.
The prior pure-function check confirmed quote-context and newline-versus-space collisions.
The grant store reuses a matching key before asking at `approvals.py:285`.

Merely possible:
A later semantically different call can reuse an earlier operator decision.
The review did not execute such a call or demonstrate escape from the jail.
The Codex classification filter remains a convenience feature at `permissions.py:444`.

Smallest compatible fix:
Hash the exact original command string with the existing full SHA-256 digest.
Keep `_segments()` for the existing inspection classifier.
Keep flattening and truncation in display functions only.
Do not parse shell syntax to guess which quote or whitespace changes are harmless.

Standing-grant effect:
Identical command strings still reuse one grant for the same configured lifetime.
Different spellings that previously produced one parsed argument list now need separate grants.
Different indentation, shell quotes, or line endings can therefore cost another prompt.
The change narrows equivalence reuse. It does not remove standing grants.
No persistent grant migration is necessary because stores live in memory.
See `approvals.py:148` and `docs/explanation/tool-approvals.md:3`.

Severity and class:
Verified approval-cache identity defect.
Classify it as an approval boundary fix, not established containment failure.

Acceptance criteria:

1. Identical raw command strings keep identical subjects.
2. Quote-context changes and meaningful whitespace changes have distinct subjects.
3. Simple and compound commands cannot collide through display normalization.
4. A fake frontend asks again for a distinct subject after approving the first.
5. An identical repeat still uses the cached decision.
6. Codex inspection exemptions retain their existing behavior.

### P3. Patch approval identity loses file content

Exact contract:
The `patch-exact` subject covers the supplied patch at `permissions.py:350`.
`tests/test_permissions.py:256` describes that scope as the patch's own body.

Actually proven:
The implementation flattens the complete patch before hashing at `permissions.py:351`.
Whitespace inside an added line can therefore disappear from identity.
This is a direct source consequence. I did not run a patch collision probe.

Merely possible:
A different file modification can receive an earlier patch approval.
No modified file or execution result was demonstrated.

Smallest compatible fix:
Hash the original complete patch string.
Keep the existing display cap and readable scope generation separate.
Do not expand this change to the deliberately path-scoped Write and Edit tools.

Standing-grant effect:
Byte-identical patch strings keep their current reuse.
Whitespace-distinct patch strings stop sharing a grant.
Patch application against a changed file can still have a different result.
Exact input identity does not promise identical external state.

Severity and class:
Source-proven approval identity loss, with untested downstream effects.
Classify it as an approval boundary fix.

Acceptance criteria:

1. Identical patch strings share a subject.
2. Added lines with different spaces have distinct subjects.
3. Line-ending changes remain distinct unless the backend already normalizes them before cotf receives the string.
4. The display cap does not change the digest input.
5. Existing Write and Edit path-grant tests retain their behavior.

### P4. Terminal approval identity hashes truncated display text

Exact contract:
`Dialog.subject` promises the exact dialog and no other at `permissions.py:995`.
Its explanation claims the whole dialog controls identity at `permissions.py:1002`.

Actually proven:
`parse_dialog()` flattens and caps the explanation before it constructs `Dialog` at `permissions.py:1089`.
`Dialog.subject` then hashes only that capped body at `permissions.py:1009`.
Two equal-length explanations can differ beyond the first 400 characters and retain identical digest inputs.
The existing cap test checks only display behavior at `tests/test_permissions.py:1068`.
The 12-character digest is not the cause of this deterministic identity loss.

Merely possible:
A later different terminal prompt can reuse an earlier answer.
The full current terminal may already omit some underlying command text through clipping or wrapping.
This pass did not establish a real terminal collision or approval misdelivery.

Smallest compatible correction:
Keep an uncapped identity value separate from `body` and `scope` display values.
Hash the complete captured explanation without display cleanup.
Use the complete SHA-256 value unless a real key-size consumer requires truncation.
Preserve the current yes/no option parser and its refusal of scope-widening options.

Limit of that correction:
Full captured text identifies a capture, not necessarily the complete underlying tool call.
The code itself records that a pending tool call is absent from the transcript at `permissions.py:951`.
Do not reconstruct exact shell input from terminal indentation.

If complete identity cannot be established, the safe policy is one operator decision per active dialog.
That policy must still merge duplicate notifications for the same pending dialog.
A zero grant lifetime alone is insufficient for duplicate relays and concurrent pending callers.
See `permissions.py:727` and `approvals.py:318`.
Do not create a new terminal-control service merely to change digest input.

Standing-grant effect:
An uncapped capture identity retains reuse for identical complete captures.
Different terminal widths or wrapping can reduce reuse.
A one-decision policy removes future standing reuse for terminal dialogs only.
That larger behavior change needs an explicit decision and operator documentation.

Severity and class:
Source-proven approval identity loss.
Classify display-versus-identity separation as a boundary fix.
Classify one-decision terminal policy as additional compatibility work if incomplete captures require it.

Acceptance criteria:

1. Equal-length dialog bodies with the same displayed prefix and different benign tails have different subjects.
2. Display text remains bounded and announces truncation.
3. Identical complete captures keep their selected reuse behavior.
4. Duplicate notifications produce one operator question and at most one delivered answer.
5. Unreadable options still refuse rather than guess.
6. No identity correction changes the option that grants a wider standing rule into an acceptable answer.

## Deliberate grant scopes and stale descriptions

Simple program-level Bash grants are not the current deliberate behavior.
The implementation rejects them at `permissions.py:310`.
The test named `test_the_grant_key_stays_program_scoped` now asserts `bash-exact` at `tests/test_permissions.py:328`.
The test name conflicts with its assertion and docstring.
The older grant-scope wording also remains at `docs/explanation/tool-approvals.md:27`.
Comments near `permissions.py:228` and `permissions.py:234` still discuss program grants.
Correct those copies with the identity change.

These broader scopes remain deliberate current behavior:

| Tool shape | Current grant identity | Proposed behavior |
|---|---|---|
| Bash | Canonical arguments or flattened shell text | Exact supplied command text |
| apply_patch | Flattened patch text | Exact supplied patch text |
| Write, Edit, NotebookEdit | Destination path | Keep destination path |
| WebFetch, WebSearch | Host | Keep the existing host scope |
| Unknown tool | Tool name | Keep the existing explicit generic-tool scope |
| Claude terminal | Capped rendered dialog | Separate complete capture identity from display; decide incomplete-capture reuse separately |

Source: `permissions.py:337` through `permissions.py:358`.
The generic-tool behavior also has an explicit test at `tests/test_permissions.py:278`.
Changing it to exact input would be a separate scope change.

Program-based Codex inspection exemptions also remain deliberate.
They suppress a question before the grant store runs at `permissions.py:555`.
They are not operator-issued standing grants.
Exact-byte hashing must not quietly change that classifier.

## Egress: current-policy recheck is the sufficient first version

Current contract:
Policy edits apply on the next CONNECT at `egress.py:624`.
The source does not promise closure of all existing tunnels after every file edit.
The proxy already pins a numeric address at `egress.py:639` and connects to it at `egress.py:590`.

Proven ordering:
The first policy refresh precedes DNS resolution and an optional operator wait.
See `egress.py:627`, `egress.py:639`, and `egress.py:650`.
There is no final refresh before returning the approved decision at `egress.py:665`.
Runtime effect remains untested.

The smallest sufficient version for current-policy semantics is:

1. After DNS returns, refresh the effective host sets before taking a pre-approved or ungated return path.
2. After operator approval returns, refresh those sets again before returning success.
3. Reject a current never-ask host even when the earlier question received approval.
4. Revalidate the pinned numeric address against the current private-host exception.
5. If the request fails the final check, remove its stored host grant as well.
6. If an allow entry disappears during DNS, follow the current normal approval path.
7. Keep the last policy evaluation synchronous until the connection attempt begins.

This retains one DNS answer for the connection and avoids another rebinding window.
It does not need a second lookup after a long human wait.
The current numeric address remains the checked destination.

A removed allow entry can legitimately produce a new approval question.
That is the existing meaning of an unknown public host.
Do not make any unrelated policy edit invalidate all pending questions.
An operator decision after removal can authorize that unknown host under the current rules.

A policy revision system adds a different contract.
It can invalidate every decision that started before a change, even if the current host policy permits it.
That needs revision ownership, grant tagging, concurrent waiter behavior, and edit semantics.
Unrelated host edits can also cause unnecessary retries under a global revision.
Cotf does not need that machinery merely to enforce current never-ask and private-host restrictions.

A per-host revision becomes relevant only if the operator requires transient or historical revocation to remain binding.
For example, every pending question could require cancellation whenever its host policy changes.
That requirement is stronger than the current documented next-CONNECT contract.

Acceptance criteria:

1. A fake resolver pause followed by a never-ask edit prevents the pending decision.
2. A fake gate pause followed by the same edit also prevents the pending decision.
3. A removed private exception invalidates an earlier pinned private fixture address.
4. A failed final check leaves no usable host grant in the store.
5. A removed allow entry follows the normal approval path for a public fixture address.
6. An unrelated host edit does not repeat a valid operator question.
7. The connection code receives the original validated numeric fixture address.
8. Existing tunnels retain the documented lifetime unless a separate revocation feature changes it.

## Challenge the terminal proposal's complexity

The source identifies two direct host-control surfaces: the raw management socket and writable launch inputs.
See `tmux.py:245`, `backends/codex.py:946`, and `backends/codex.py:986`.
The final environment has a third, independent correctness problem.
The additive staging function cannot remove earlier server variables at `backends/codex.py:770`.
Its error path continues with the server environment at `backends/codex.py:782`.

The narrow Codex direction keeps the existing shared host server.
The host already launches the wrapped agent, captures output, and owns session teardown.
Remove agent access to management authority and protect the host-consumed launch inputs.
Establish the exact curated environment at the final child launch.
If trusted environment staging fails, use the existing unhosted path.

Do not replace that flow with a new general terminal broker without a concrete missing operation.
Do not add a server per turn merely to obtain shorter token lifetime.
A separate unrestricted host server still exposes host launch authority through its socket.
Socket file modes and read-only attachment flags do not restrict a same-owner raw client.

Claude terminal compatibility needs a separate checked integration.
Cotf currently wraps the external launcher at `backends/claude.py:1000`.
Its external wrapper controls tmux, according to `tmux.py:35`.
Moving that wrapper outside the jail requires evidence that the actual Claude child still receives the intended jail.
The review lacks that evidence.
Use the established native path as a bounded fallback if necessary, with an explicit terminal-behavior change.

The approval helper must share the host's exact socket selection.
Its bare tmux call remains at `permissions.py:1190`.
The main integration already centralizes explicit selection at `tmux.py:232`.
Use that existing operation path before inventing another service.

## Which work precedes lifetime improvements

Host management authority and trusted launch inputs come first.
Short-lived route tokens cannot restore custody if the child can direct an unrestricted host service.
They also cannot correct a stale inherited environment or mutable host launch script.

Cookie state isolation also addresses account authority independently of token lifetime.
Disabled storage alone leaves forwarded request and response cookies intact.
See `broker.py:265`, `broker.py:293`, and `broker.py:378`.
Choose the supported route contract and address all applicable cookie paths.

After those decisions, assess whether per-turn credential grants solve a stated cancellation requirement.
They reduce retained authority after a turn.
They do not prevent concurrent use of a copied capability during its lifetime.
Do not confuse revocation improvement with undisclosed capability transport.

## Benign fixture test matrix

These are proposed tests. None executed in this round.
All generated files contain ordinary text markers and remain inside the test's temporary tree.
No test uses an operator credential, external provider, existing tmux server, or sensitive path.

| Area | Benign fixture | Required result | Test level |
|---|---|---|---|
| Path grammar | Workspace file and sibling fixture file named `report.txt` | Inside admitted; unapproved sibling refused through supported schemes | Pure parser |
| Path roots | Two sibling fixture directories with one explicit root | Root admitted; sibling refused | Pure parser |
| Path spelling | Separated flags, attached values, JSON string values, supported file prefixes | Equivalent path locations receive equivalent decisions | Pure parser |
| Shell identity | Harmless print strings with different quotes, spaces, and line endings | Distinct source text has distinct subject; identical source repeats | Pure subject function |
| Shell grant reuse | Fake frontend and two distinct print requests | One question per distinct subject; identical repeat reuses grant | In-memory service |
| Patch identity | Two valid fixture patches that add different spacing to a text file | Distinct subjects without applying either patch | Pure subject function |
| Deliberate scopes | Two contents for one Write path, two fetch paths on one host, one generic tool name | Existing explicit broad scopes remain | Pure subject function |
| Terminal identity | Static fake panes with equal-length tails outside the display cap | Different complete captures have different identities | Pure parser |
| Terminal delivery | Fake pane reader and fake keystroke collector | Duplicate notices merge; one answer; no wider-rule selection | In-memory service |
| Egress during DNS | Fake resolver held by an event; temporary host-policy mapping | Final current policy controls decision | In-memory proxy |
| Egress during approval | Recording fake gate held by an event | Current prohibition wins and no stale grant remains | In-memory proxy |
| Egress pinning | Fake resolver with a fixed numeric address; connection callable records arguments | The same validated numeric address reaches connect | In-memory proxy |
| Launch environment | Inert child command and disjoint harmless marker mappings | Final environment matches selected mapping, including removals | Generated launch input |
| Launch failure | Fake file-write failure | Safe unhosted fallback; no inherited-environment continuation | Backend unit |
| Terminal authority | Generated macOS policy and Linux mount arguments | Host socket and launch files do not enter child management grants | Policy generation |
| Terminal targeting | Fake subprocess collector and unrelated inherited `TMUX` marker | Launch, capture, approval, and cleanup use one explicit selected address | Backend unit |
| Lifecycle | Fake clock, retained synthetic URL, and recording request handler | New requests deny after chosen expiry; in-flight behavior matches its documented contract | In-memory broker |

Kernel-level behavior and real CLI compatibility remain separate validation stages.
Generated arguments do not prove kernel confinement.
Pure parser tests do not prove disclosure.
Provider behavior cannot be inferred from harmless local cookie state.

The lead reviewer controls the remaining review window and final decisions.
