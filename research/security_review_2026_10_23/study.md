# Cotf security study

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

The review began at 2026-10-22 17:41:03 UTC.
It concluded at 2026-10-22 18:41:43 UTC.
The review window lasted 60 minutes and 40 seconds.
Four specialists reviewed credential custody, containment, broker decisions, and evidence quality.
Independent review, cross-review, compatibility review, and synthetic checks informed these proposals.

**Scope and central finding**

Cotf's trusted host command broker remains consistent with its stated trust model.
The jailed agent calls selected host tools through a command shim.
In jail mode, the policy hides covered credential stores while the broker runs outside the jail.
This design does not inherently need another sandbox around every trusted tool.

That conclusion depends on the broker contract.
Tool selection, argument interpretation, returned output, and host management interfaces must preserve the intended authority.
The useful lessons concern those contracts rather than a wholesale runtime replacement.

The credential guarantee also needs explicit exceptions.
Cotf deliberately supplies a selected Claude subscription access token to its child.
Codex credential visibility depends on whether ChatGPT brokerage is enabled.
Protecting refresh credentials and unrelated stores differs from giving the backend no usable credentials.
See [selected Claude handoff](../../src/claude_on_the_fly/sandbox.py#L96).

**What the other projects contribute**

The comparisons use pinned source, not feature counts.
They do not rank whole products as safer or less safe.

| Project | Relevant security model | Useful lesson for cotf |
|---|---|---|
| [nono](https://github.com/nolabs-ai/nono/blob/07ffbf88c56182070a11a4f92ab076524297e914/crates/nono/src/sandbox/macos.rs#L530) | Landlock on Linux and Seatbelt on macOS; optional credential proxy and command confinement | Check service access as well as credential paths. Its explicit Keychain service denies deserve targeted compatibility validation. |
| [NemoClaw](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/nemoclaw-blueprint/blueprint.yaml#L6) | OpenShell provides process, filesystem, and network controls; managed inference credentials stay outside the sandbox | Keep permission to contact a destination separate from permission to attach a credential. Preserve fixed endpoint binding. |
| [OpenClaw](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/gateway/security/index.md#L15) | Trusted operator model; optional container confinement and secret egress facilities; plugins remain trusted host code | Name plaintext environment exceptions and define when scoped secret grants expire or revoke. |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent/blob/41dfcf2c1bd7146d4ad46aea08b7dd007900602e/SECURITY.md#L70) | Local terminal by default; optional Docker terminal and credential proxy; other host integrations retain their own scope | State exactly which execution paths a sandbox covers. Proxy environment variables alone do not force all network traffic through a proxy. |
| [OpenAB](https://github.com/openabdev/openab/blob/8b6bf293d8de44b4df64882f4958639f09afcaaf/crates/openab-core/src/acp/connection.rs#L393) | Agent orchestration with environment filtering; stronger boundaries depend on deployment | Clearing environment variables does not conceal credential files under a preserved home directory. Cotf's filesystem boundary remains valuable. |

NemoClaw pins OpenShell 0.0.116.
Its pinned [credential relay](https://github.com/NVIDIA/OpenShell/blob/d1155aa70042d3e2ee49dbfa15346b108b7c1d92/crates/openshell-supervisor-network/src/l7/relay.rs#L88) separates destination authorization from credential attachment.
Newer OpenShell behavior cannot establish NemoClaw's current guarantees.
Its [OpenClaw policy](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/nemoclaw-blueprint/policies/openclaw-sandbox.yaml#L58) and [Hermes additions](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/agents/hermes/policy-additions.yaml#L50) permit best-effort Landlock behavior.
Managed credential custody does not include every session token or independently installed plugin secret.

OpenClaw distinguishes [protected secret entries from readable environment entries](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/gateway/secrets/secret-store-and-egress.md#L16).
Its secret proxy defaults off.
Its [setup-egress helper](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/plugins/sdk-runtime.md#L819) revokes authorization on callback settlement, cancellation, explicit revocation, or worker failure.
The callback still owns process cleanup.
Do not infer universal process teardown from the existence of a revoke handle.


Nono's proxy authentication varies by mode.
Strict standalone operation and credential interception enforce authentication.
Sandboxed transparent CONNECT has lenient compatibility behavior.
It is not a universal authentication example.
See the pinned [CONNECT modes](https://github.com/nolabs-ai/nono/blob/07ffbf88c56182070a11a4f92ab076524297e914/crates/nono-proxy/src/connect.rs#L27).

**Observed behavior and source-backed corrections**

| Finding | Evidence | Proposed correction | Limit |
|---|---|---|---|
| One provider session shares cookie state across routes | A local web fixture sends account A's harmless marker on B and default routes | Give each route authority separate cookie state; use a disabled jar only for verified stateless routes | No real provider takeover or credential disclosure was demonstrated |
| Approval identities discard meaningful input text | Pure subject calculations produce equal keys for distinct Bash inputs; patch flattening and terminal display caps also lose text | Hash full original input separately from display text; do not reuse grants from incomplete terminal captures | This does not make approvals a filesystem boundary or make identical commands have identical effects |
| Approval work can outlive service stop | A test of the actual service invokes a mocked answer after stop | Cancel pending relay work and fence final delivery with the originating turn or pane identity | No real terminal input or replacement-pane effect was demonstrated |
| Hosted launch state does not represent the complete selected environment | Source uses environment differences; a harmless model preserves a variable omitted from a later turn | Set the complete environment at final child launch, including removals; use an existing safe fallback if staging fails | No real account crossover was demonstrated |

Current source references:
[cookie session](../../src/claude_on_the_fly/broker.py#L378),
[Bash subject](../../src/claude_on_the_fly/permissions.py#L307),
[service stop](../../src/claude_on_the_fly/permissions.py#L615),
[environment staging](../../src/claude_on_the_fly/backends/codex.py#L770).

Cookie storage and forwarded headers are separate controls.
The current filters also pass caller Cookie and upstream Set-Cookie headers.
A disabled jar alone does not remove those headers.
Header filtering therefore needs an explicit route contract and independent compatibility checks.

Separate sessions preserve same-route continuity in the harmless fixture.
The final validation covers A/B/default isolation, concurrency, and both alias grouping choices.
It does not prove production route selection or provider compatibility.
Aliases should share state only when their authority contract and an existing consumer require it.
Login replacement and account deletion also need cleanup semantics.

The current Bash grants intend exact-command scope but use lossy normalization.
The defect concerns lossy equivalence, not a current program-wide grant.
Preserve deliberate path grants for Write and Edit, host grants for fetch, and generic-tool grants.
Equivalent shell spellings may ask again after exact input hashing.
That is a concrete compatibility cost.

**Source-backed boundary concerns that need runtime validation**

The host pane channel deserves the highest boundary review priority.
The macOS jail explicitly grants a tmux management socket.
The Codex hosted path starts host terminal machinery before it enters the child jail.
This path concerns terminal mode.
Native execution follows a different launch path.
Both macOS bases grant writes to shared pane files that the host launcher consumes.
Linux has no equivalent visible panes regrant in the reviewed source.

That forms an authority chain beyond ordinary Bash tool access.
A pathname restriction does not restrict which tmux operations the socket accepts.
A host server per turn alone does not remove that authority.
No completed live escape was demonstrated.

The smallest preferred direction keeps pane management on the host.
The child receives terminal streams but no raw host management socket.
The host protects scripts and environment files that it consumes before jail entry.
The final child receives exactly the selected environment.
Host capture, resize, approvals, attachment, and teardown continue to use an exact owned pane.

Codex already has a final child launch point.
Claude's external terminal wrapper needs a checked child-launch interface.
A native fallback has feature differences and must be visible to the operator.
An alternative runs a dedicated terminal server inside the existing jail.
That alternative changes discovery, attachment, and cleanup.

References:
[tmux server contract](../../src/claude_on_the_fly/tmux.py#L8),
[hosted Codex launch](../../src/claude_on_the_fly/backends/codex.py#L943),
[macOS socket grant](../../src/claude_on_the_fly/seatbelt/jail.sb#L50).

Three other concerns remain bounded:

1. The command parser recognizes ordinary file inputs but misses AWS's documented fileb:// syntax. Synthetic admission checks accept an outside path. Add that grammar to the existing root check. No actual AWS file read or disclosure was tested.
2. Credential capability URLs appear in Codex arguments while process visibility remains broad. The source creates an account-isolation question. Cross-account capability readback under the real jail remains unverified.
Any later validation should use synthetic capabilities.
3. Cotf's macOS policy permits broad Mach operations while denying Keychain files. Nono separately denies named security services. Apple's architecture confirms service-mediated Keychain access, but current cotf access remains unverified.

References:
[file argument parser](../../src/claude_on_the_fly/commands.py#L716),
[AWS file parameter grammar](https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-parameters-file.html),
[Codex provider arguments](../../src/claude_on_the_fly/codex_auth.py#L137),
[Apple security architecture](https://developer.apple.com/library/archive/documentation/Security/Conceptual/Security_Overview/Architecture/Architecture.html).

Per-turn bearer lifetimes reduce retained authority.
They do not prevent concurrent use of a copied bearer.
Moving a bearer from arguments to environment variables does not establish isolation by itself.
Do not add lifecycle machinery before defining the relevant account and cancellation contract.

**Release requirements for the pane boundary**

1. End old authorized turns before claiming the new boundary. A new launch policy does not revoke an existing process policy.
2. Deny host launch-file reads and writes explicitly under the global-read macOS base. Check resolved paths and later grants.
3. Check the exact final environment against a running server. Cover removals, empty values, and values equal to daemon defaults.
4. Preserve the selected account, session, approvals, resume, and cleanup across any native fallback. Show terminal feature loss explicitly.
5. Keep host creation, capture, approval delivery, attachment, and teardown on one exact owned target.
6. Validate proposed Keychain service denies on an isolated account with synthetic stores. Check both credential denial and required public certificate trust.

These are future implementation gates, not completed runtime validation.
Detailed compatibility analysis appears in [the containment design](reviews/containment.md) and [the draft check](reviews/containment_draft_check.md).

**Contracts and visibility**

The documentation incorrectly assigns the command broker's lifetime contract to credential URLs.
Credential route capabilities currently last for the broker lifetime.
Production command capabilities are per turn or job, workspace-bound, and revoked during cleanup.
Revocation prevents new command requests; it does not cancel an accepted host subprocess.

Egress and tool approval services have different lifetimes again.

| Service | Current scope | Teardown limit |
|---|---|---|
| Credential broker | One bearer per broker object, with separate bearers for isolated account routes | Closing the listener does not erase the object's bearer values |
| Command broker | A fresh workspace-bound capability per production turn or job | Revocation stops new requests, not an accepted host subprocess |
| Egress proxy | Per-session service and host grants | Grant expiry does not close an established tunnel |
| Tool approvals | Per-session service and scoped grants | Current stop does not fence all detached terminal callbacks |

Do not describe one token lifetime as the whole security model.
See [current broker documentation](../../docs/agent/broker.md#L25).

Linux's dotenv masks cover selected paths found at launch.
They do not establish macOS-equivalent coverage for every future file.
Writable runtime and plugin cache grants also need accurate persistence wording.
Keep needed backend state until consumers prove that narrower grants work.

Cotf already has Doctor checks and command-policy validation.
Extend those checks with effective sandbox mode, active enforcement, custody exceptions, and restart-pending settings.
Do not label configured policy as tested kernel enforcement.
Reuse the current resolvers rather than creating another policy engine.

Pending egress decisions need an explicit edit contract.
A final policy check after name resolution and operator waits can enforce current prohibitions.
A global revision system is unnecessary for that limited requirement.
Closing established tunnels would be a separate behavior change.

Authentication for approval and general egress endpoints is a separate optional work item.
It requires compatibility checks across hooks, terminal notifications, proxy clients, jobs, and local helper traffic.
It is not mandatory hostile-tenant isolation under the shared assistant model.

**Delivery options for the final decision**

| Option | Contents | Tradeoff |
|---|---|---|
| A: Local corrections | Cookie ownership, exact identities, callback fencing, known file grammar, and accurate contracts | Small independent changes; does not resolve the host pane authority question |
| B: Host-owned terminal control | A plus protected launch inputs, complete child environments, and removal of direct child access to the host socket | Preserves the current shared host terminal view; Claude wrapper compatibility needs proof |
| C: Terminal server inside the jail | A plus one server inside each live jail policy and complete environment selection | Preserves the existing jail model; requires broader discovery, attachment, and cleanup changes |

I recommend B as the target direction.
It preserves trusted host tools while separating terminal management from the jailed child.
Small independent corrections need not wait for its terminal integration work.
C remains a valid alternative if the external wrapper prevents host-owned child launch.

No current result justifies replacing cotf with nono or OpenShell.
Signed instructions, Merkle audit chains, rollback, and full encrypted-traffic interception do not address these concrete defects.
Another sandbox around every trusted command tool is also not the default recommendation.

**Validation and limits**

Existing reviewer runs report 157 custody tests, 701 broker and approval tests, and 19 Linux wrapper tests passing.
These suites overlap; their counts must not be summed.
The Linux wrapper checks do not establish live bubblewrap enforcement.
Earlier macOS parity checks are separate evidence, not proof of these findings.

Fourteen final scratch checks pass.
They cover six cookie cases and eight mocked lifecycle cases.
Those checks validate proposed mechanisms, not implemented cotf fixes.
A separate mocked test exercises the current approval service's late callback.
The cookie checks use harmless markers and local web servers.

Two live boundary probes encountered automatic cybersecurity review blocks.
The reviewers stopped those probes and did not retry them.
Source review and harmless protocol or mocked checks continued.
Live exploit, real credential disclosure, and real provider account impact remain unverified.

The independent assurance reviewer checked the draft and its primary citations.
The final fingerprint check found all 26 reviewed files unchanged against their recorded snapshots.
Fourteen fingerprints date from the initial review. Twelve additional fingerprints date from the later synthesis pass.
This study changes no production code and performs no git operations.


Evidence: [independent assurance](reviews/assurance.md), [compatibility objections](reviews/compatibility.md), and [final mock validation](evidence/validation.md).
