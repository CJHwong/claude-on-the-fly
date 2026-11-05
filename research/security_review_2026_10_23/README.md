# Security model research, 2026-10-23 (historical snapshot)

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

The study records the historical review.
The review lasted 60 minutes and 40 seconds and used four expert reviewers.
This folder preserves the findings, proposed changes, compatibility risks, and evidence.

Start with [the concrete suggestions](suggestions.md).
Read [the study](study.md) for the comparison with nono, NemoClaw/OpenShell, OpenClaw, Hermes Agent, and OpenAB.

Cotf's trusted command shim fits its stated trust model.
The recommendation preserves that model.
The main boundary concern involves host terminal management and its launch inputs.
The study does not demonstrate a live escape or real credential disclosure.

Recommended implementation sequence:

1. Correct approval identities, callback cleanup, AWS file grammar, and inaccurate documentation.
2. Check provider compatibility before isolating cookie state.
3. Change terminal management and environment delivery together, after backend compatibility checks.
4. Validate the deferred credential-channel questions with synthetic values in an isolated environment.

The study changed no production code.
This archive contains research only.
It does not establish the current implementation or deployment status.

| Material | Location |
|---|---|
| Final conclusions and primary sources | [Study](study.md) |
| Exact changes and feature risks | [Suggestions](suggestions.md) |
| Credential and cookie analysis | [Credential custody review](reviews/credential_custody.md) |
| Commands, approvals, and policy analysis | [Command broker review](reviews/command_broker.md) |
| Terminal and platform analysis | [Containment review](reviews/containment.md) |
| Workflow compatibility | [Compatibility review](reviews/compatibility.md) |
| Independent claim checks | [Assurance review](reviews/assurance.md) |
| Final containment wording checks | [Containment draft check](reviews/containment_draft_check.md) |
| Evidence and unsupported impact claims | [Findings register](evidence/findings_register.md) |
| Fourteen final scratch checks | [Validation report](evidence/validation.md) |
| Completion time and source stability | [Verification summary](evidence/verification_summary.json) |
| Initial source fingerprints | [Core manifest](evidence/source_manifest.json) |
| Additional source fingerprints | [Extended manifest](evidence/extended_source_manifest.json) |

Expert reports preserve earlier review stages.
The final study and suggestions take precedence where later assurance checks correct them.
Reported test counts overlap and must not be summed.
Source fingerprints describe the study snapshot; check them before later implementation.

Two live probes encountered automatic cybersecurity review blocks.
The reviewers stopped those probes and did not retry them.
Source checks and harmless synthetic or mocked checks continued.
This folder contains reports, not probe scripts or production credentials.
