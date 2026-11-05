Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Independent assurance review

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This report reviews the synthesis, not new vulnerabilities.
“Must fix” means correct the final claims before delivery.
It does not authorize repository changes.
Final convergence remains gated until 2026-10-22 18:41:03 UTC.

I read all seven requested reports and checked the relevant source.
I fetched pinned primary sources as text.
I ran no tests, live probes, provider calls, process inspection, tmux commands, Keychain calls, installs, or git operations.
I did not retry either interrupted reviewer run.
Primary source copies are under `assurance-primary/` beside this report.

## Must fix

### M1. Keep demonstrated behavior separate from inferred impact

The evidence register provides the right limits.
Retain those limits in titles, priorities, and the final summary.

| Finding | Defensible wording | Unsupported stronger wording |
|---|---|---|
| Cookies | Synthetic requests demonstrate cross-account cookie state sharing in the broker. | Provider takeover or demonstrated authentication theft. |
| Approval subjects | Pure checks demonstrate distinct shell inputs sharing one identity. Source shows patch and terminal identity loss. | A demonstrated jail escape or completed unauthorized command. |
| AWS binary file input | The command guard admits a documented file-input spelling outside its recognized grammar. | The installed command reads and returns a protected credential. |
| Host pane control | Source grants access to a host management socket and host-consumed launch inputs. | A completed live sandbox escape on either platform. |
| Launch environment | Harmless markers show that additive exports cannot express removed variables. | A demonstrated real credential crossing between accounts. |
| Delayed approval | A mocked callback reaches the answer function after service stop. | Keystrokes reached a live replacement pane. |
| Keychain | File denial does not establish service denial. Current source permits broad service operations. | A current jailed child retrieved an actual Keychain item. |

“High priority” can identify an important boundary risk without claiming a demonstrated exploit.
State the evidence level beside that priority.
The earlier 51 macOS parity checks do not establish these later claims.

### M2. State the intentional authentication exceptions

Withdraw “no real credentials ever reach the agent,” if that wording appears in the synthesis.
Use this replacement:

> Cotf keeps configured broker credentials in trusted host components.
> Some backend login paths intentionally give the agent its selected credential.
> Claude can receive its selected subscription access token.
> Codex can read its selected login unless the brokered login path hides that file.

Source: `sandbox.py:113-149`, `sandbox.py:688-690`, and `sandbox.py:1600-1615`.
Both filesystem bases document the deliberate native Codex login behavior.
See `seatbelt/fs-allow-reads.sb:341-347` and `seatbelt/fs-deny-most.sb:344-350`.
All these paths are under `src/claude_on_the_fly/`.

This describes the intended split.
It does not claim that every alternate credential channel has received a runtime check.
The custody inventory must list agent-held backend credentials as explicit exceptions.
It must also list usable broker capabilities, which grant authority without revealing upstream secrets.

### M3. Correct the OpenClaw lifecycle attribution and citation

`proxy-worker.ts:118` begins `registerProcess`.
It does not prove that every managed process revokes its grant on completion.
Its `revoke()` implementation appears at lines 132-136.
The source establishes a revocation mechanism.

`docs/plugins/sdk-runtime.md:722` documents `api.logger` in the pinned file.
The relevant setup-egress lifecycle text appears at lines 819-825.
Use this replacement:

> OpenClaw documents revocable grants for its setup-egress helper.
> Cancellation, explicit revocation, callback settlement, and worker failure end authorization.
> The callback owns process and bridge cleanup.

Do not convert callback settlement into an automatic guarantee for every process launcher.
The helper explicitly does not manage remote processes itself.
No runtime check of this external mechanism occurs here.

Sources: [pinned worker](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/src/secrets/egress-proxy/proxy-worker.ts#L118), [pinned lifecycle contract](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/plugins/sdk-runtime.md#L819).

### M4. Qualify nono proxy authentication by mode

“Nono authenticates every proxy request” is false for the pinned source.
`ConnectAuthMode::Lenient` permits transparent CONNECT after an authentication failure.
Sandboxed `run`, `shell`, and `wrap` use that compatibility contract.
Host filtering still applies.

Use this replacement:

> Nono enforces proxy authentication for credential interception and authenticated standalone proxy operation.
> Its sandboxed transparent CONNECT path permits missing or invalid authentication for client compatibility.

An explicit `--no-auth` option also exists.
Do not import an unconditional authentication guarantee into the cotf comparison.
This does not make unauthenticated cotf endpoints safe.
Their consequence still depends on reachability and the authority each service grants.

Sources: [pinned CONNECT modes and check](https://github.com/nolabs-ai/nono/blob/07ffbf88c56182070a11a4f92ab076524297e914/crates/nono-proxy/src/connect.rs#L27), [pinned credential-interception check](https://github.com/nolabs-ai/nono/blob/07ffbf88c56182070a11a4f92ab076524297e914/crates/nono-proxy/src/server.rs#L1630).

### M5. Correct the actual cotf guarantees

These corrections follow from current source without a new execution claim.

| Existing claim | Exact correction |
|---|---|
| Credential URLs are per-turn and workspace-bound. | Ordinary broker routes share one object-lived bearer. Isolated account routes receive separate object-lived bearers. Neither binds a workspace or turn. |
| Token revocation stops the turn's work. | Command token removal rejects later admission. It does not find or cancel an already launched subprocess. |
| Broker stop permanently destroys the capability. | Stop closes the listener and session. The same object retains its token values for a later start. |
| Global plugins cannot persist. | Configuration writes are restricted. Selected writable caches and shell snapshots can contain later-consumed executable content. |
| Every failure denies. | Gate errors and timeouts deny. Invalid permission configuration can select the disabled fallback. Failed pane environment staging can retain server state. |

Sources: `broker.py:332-337`, `broker.py:359-404`, `commands.py:1063-1090`, and `commands.py:1338-1356`.
The two failure cases appear at `permissions.py:166-187` and `backends/codex.py:749-788`.
The conflicting documentation appears at `docs/agent/broker.md:25-30` and `docs/reference/config-yaml.md:17-21`.
The cache exception already appears at `docs/agent/broker.md:226-244`.
Preserve that useful existing disclosure and correct its broader sibling statement.

## Should fix

### S1. Use one evidence standard across projects

Compare selected configurations, protected resources, and caller authority.
Do not compare cotf defaults with another project's optional hardened deployment.
Do not treat cotf's trusted tools as a defect while accepting equivalent trusted host integrations elsewhere.

| Project | Verified primary mechanism | Limit that must accompany the comparison |
|---|---|---|
| Nono | The profile emits targeted Keychain service denies. Later platform rules can authorize exceptions. | Generated rules and their comments do not prove current cotf credential access. |
| NemoClaw/OpenShell | NemoClaw pins 0.0.116. The reviewed resolver scopes credentials by endpoint and captures a revision. | This mechanism does not require cotf to adopt a revision system for fixed upstream routes. |
| OpenClaw | Documentation defines one trusted boundary per Gateway and provides an operator audit. | Shared operation does not imply hostile tenant isolation. |
| Hermes | Documentation distinguishes terminal isolation from whole-process wrapping. | Its Docker proxy protects routed traffic and named credentials. Raw sockets and uncovered providers remain exceptions. |
| OpenAB | The agent launch clears its environment and explicitly preserves real HOME for saved logins. | Environment filtering alone does not establish filesystem credential isolation. Deployment isolation can still add protection. |

Sources: [nono profile](https://github.com/nolabs-ai/nono/blob/07ffbf88c56182070a11a4f92ab076524297e914/crates/nono/src/sandbox/macos.rs#L555), [NemoClaw pin](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/nemoclaw-blueprint/blueprint.yaml#L6), [OpenShell endpoint resolver](https://github.com/NVIDIA/OpenShell/blob/d1155aa70042d3e2ee49dbfa15346b108b7c1d92/crates/openshell-core/src/provider_credentials.rs#L259), [OpenShell relay](https://github.com/NVIDIA/OpenShell/blob/d1155aa70042d3e2ee49dbfa15346b108b7c1d92/crates/openshell-supervisor-network/src/l7/relay.rs#L88).

Additional sources: [OpenClaw trust model and audit](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/gateway/security/index.md#L15), [Hermes isolation scope](https://github.com/NousResearch/hermes-agent/blob/41dfcf2c1bd7146d4ad46aea08b7dd007900602e/SECURITY.md#L70), [Hermes proxy exceptions](https://github.com/NousResearch/hermes-agent/blob/41dfcf2c1bd7146d4ad46aea08b7dd007900602e/website/docs/user-guide/egress/iron-proxy.md#L422), [OpenAB launch](https://github.com/openabdev/openab/blob/8b6bf293d8de44b4df64882f4958639f09afcaaf/crates/openab-core/src/acp/connection.rs#L393).

Apple's archived architecture supports the file-versus-service distinction.
It dates to 2012 and does not identify every current service path or prove current profile behavior.
Source: [Apple architecture](https://developer.apple.com/library/archive/documentation/Security/Conceptual/Security_Overview/Architecture/Architecture.html).

### S2. Choose the egress edit contract before ranking its correction

The two design reports use different possible contracts.
The final synthesis must name the chosen contract.

1. If policy snapshots at admission may finish, document that boundary.
2. If pending requests must satisfy current rules, recheck after waits and remove invalid grants.
3. If every observed narrowing must cancel pending decisions, a local monotonic revision can express that stronger rule.

The current source promises refresh on the next CONNECT.
It does not promise termination of existing tunnels after every edit.
`egress.py:627-665` proves the ordering around waits.
`approvals.py:318-340` and `approvals.py:378` explain joined requests and grant storage.
A final connection refusal alone must not become a claim that stale grants never commit.
A global revision across unrelated services adds no required assurance here.

### S3. Keep cookie repair narrower than provider security claims

Separate jars repair the demonstrated automatic cross-account storage sharing.
They do not strip caller cookies or returned cookies.
A disabled jar also leaves those two header channels intact.
Account replacement can require a jar reset even when a route name stays constant.

Stateless routes with stripped cookies offer a simpler contract if supported consumers accept it.
Source header injection does not prove that compatibility.
Do not claim a private provider supports stateless operation from existing measurements with the current jar.
No generic cookie framework is necessary before a concrete route needs one.

### S4. Distinguish required checks from completed checks

The design reports contain useful acceptance criteria.
They are proposals, not executed validation.
Keep kernel enforcement, generated policy, mocked lifecycle, synthetic protocol, and provider compatibility in separate rows.
Do not add overlapping suite counts into a unique total.
Complete the source fingerprint comparison before delivery.
If a fingerprint changes, qualify affected source conclusions before using them.

## Consider

Per-turn provider capabilities reduce retained authority after completion.
They do not authenticate caller identity or prevent concurrent use of a disclosed bearer.
They are optional unless the operator selects that narrower lifetime.

Targeted Keychain service denies remain a relevant candidate.
Required certificate and backend consumers need compatibility evidence before release.
Do not describe nono's rule list as proof of complete service coverage.

Authentication for approval and egress endpoints can strengthen caller attribution.
Its priority depends on the intended session contract and actual endpoint reachability.
Shared memory alone establishes neither a hostile tenant requirement nor an authentication failure's practical impact.

Do not add a secret manager, second tool sandbox, global policy revision, or general terminal broker without a missing required operation.
Per-turn host tmux servers still expose host launch authority when the child controls their unrestricted socket.
Native fallback changes visible terminal behavior and needs an explicit release choice.

## Proposal families that survive

1. **Protect host terminal management and launch inputs.**
   Keep Codex management on the host, protect staged files, and establish the complete selected environment.
   Bind delayed terminal answers to the active pane instance and stop detached relays at teardown.
   Keep Claude wrapper integration conditional on source review and compatibility checks.

2. **Repair bounded broker decisions.**
   Separate cookie state for isolated accounts and preserve complete approval identities.
   Add supported file-input grammar to the existing command guard.
   Apply any selected egress correction locally to its actual edit contract.
   Preserve trusted host tools and deliberate broader grant scopes.

3. **Correct custody claims and extend existing diagnostics.**
   Publish actual lifetimes, intentional credential exceptions, writable persistence paths, platform limits, and pending restart state.
   Extend Doctor through current policy resolvers.
   Report configured policy separately from completed enforcement checks.

These families address current boundaries without replacing the architecture.
The evidence does not establish completed escapes or provider compromise.

## Review of the provisional synthesis

I read `study-report-draft.md` at 2026-10-22 18:29:51 UTC.
That draft already incorporates the main corrections above.
M1 through M4 therefore serve as retained assurance limits, not unresolved objections to that draft.
Its lifetime and persistence wording also addresses the principal M5 claims.

The remaining release requirement concerns existing processes.
New policy generation does not remove socket grants from an already running jailed child.
Drain old frontend and job turns before claiming the replacement boundary.
Retain exact ownership during cleanup of the shared host server.
This is a later rollout requirement, not authorization for live containment work now.

Use “consistent with the stated trust model” for the architectural conclusion.
“Sound” can imply a broader implementation assurance than this source review establishes.
Use “Bash grants intend exact-command scope but use lossy normalization.”
That states both the existing contract and its defect.

The NemoClaw preset sentence has exact pinned support.
Its OpenClaw policy selects `best_effort` at lines 58-59.
Its Hermes policy additions select the same value at lines 50-51.
Attribute these files to NemoClaw, not the OpenShell source tree.
These settings do not prove that every other containment layer disappears when Landlock support is partial.
Sources: [OpenClaw policy](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/nemoclaw-blueprint/policies/openclaw-sandbox.yaml#L58), [Hermes policy additions](https://github.com/NVIDIA/NemoClaw/blob/7312e7f49e3e96125b8237968e2552a48e2cb1b6/agents/hermes/policy-additions.yaml#L50).

The OpenClaw environment exception also has direct pinned documentation.
Protected secret entries and agent-readable environment entries have different contracts.
Its secret proxy defaults off.
Source: [secret store and egress](https://github.com/openclaw/openclaw/blob/7246bf3f9b7569c6126e18b26922e9d7b4644efa/docs/gateway/secrets/secret-store-and-egress.md#L16).

The final scratch file defines six parameterized cookie cases and eight mocked lifecycle cases.
Its case structure matches the draft's count of fourteen.
I did not execute that file or independently verify the reported pass result.
The draft correctly labels these as checks of proposed mechanisms.
They do not prove that cotf already implements those mechanisms.

The lead reports unchanged fingerprints for fourteen core files and twelve extended files at 18:29:50 UTC.
The final delivery still needs its own completion time and source check.
No final assurance verdict precedes the agreed convergence time.
