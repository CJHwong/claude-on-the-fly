# Evidence register

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This register separates demonstrations from source claims.
The expert review concluded at 2026-10-22 18:41:43 UTC.
The final study incorporates the assurance corrections.

| Identifier | Observed behavior | Evidence | Claim that remains unsupported |
|---|---|---|---|
| E1 | The shared broker sends a synthetic account A cookie on an account B request | Real local HTTP requests through current Broker code | Real provider takeover, credential theft, or acceptance of that cookie as authentication |
| E2 | Cookie headers pass through the broker in both directions | Current header filters and harmless protocol checks | Every provider uses those headers as a secret |
| E3 | Different meaningful Bash inputs share a grant identity | Pure subject-function calculations and current source | The jail or host credential boundary was bypassed |
| E4 | Patch and terminal identity can lose text through flattening or display caps | Current source and existing test coverage inspection | Every terminal capture reproduces full underlying tool input |
| E5 | The AWS file grammar includes a scheme the broker does not recognize | Pure admission checks, installed help, and official AWS documentation | The installed CLI reads and discloses a protected file through an allowed operation |
| E6 | macOS profiles expose a pane command socket and writable host-consumed pane files | Current source and launcher ordering | A completed live sandbox escape |
| E7 | Exporting only changed environment keys leaves omitted harmless markers in inherited state | Benign scratch environment checks | A real credential crossed accounts in a live tmux pane |
| E8 | A delayed mocked approval callback survives PermissionService.stop | Mocked asynchronous callback and existing service stop method | Keystrokes reached a live replacement pane |
| E9 | Account capability URLs appear in Codex arguments while process visibility is broadly allowed | Current source and recorded process-visibility contract | A jailed turn read another real account token |
| E10 | Filesystem-only Keychain denial differs from service-mediated access control | Current cotf profile, pinned nono denies, Apple architecture documentation | Current cotf returns an actual Keychain item |
| E11 | Linux dotenv masks are bounded spawn-time snapshots | Current mount and scan source | Every conventional protected credential store is exposed |
| E12 | Credential documentation describes a narrower lifetime than source implements | Current broker, command broker, and orchestration source | Daemon-lived ordinary provider grants are inherently a defect |

## Existing tests observed during expert review

| Suite | Result | Limitation |
|---|---|---|
| Broker, Codex auth, approvals | 157 passed | Includes overlap with the broker review suites |
| Commands, egress, permissions, approval shim, approvals | 701 passed | Existing cases do not cover every new finding |
| Pure Linux wrapper | 19 passed | Generated mount arguments and scratch state, not a live Linux kernel boundary |
| Benign cookie and environment choices | 4 passed | Protocol and harmless marker behavior |
| Delayed service-stop callback | 1 passed, 4 deselected | Mocked terminal callbacks only |

Do not add these counts into a unique-test total. The suites overlap.
An earlier conversation run passed 51 real macOS parity tests.
That earlier run was not repeated as a proof of this review's new candidates.

## Interrupted validation

Two reviewer runs stopped at automated security checks.
The lead stopped their live boundary probes and did not retry them.
The reviewers continued source-only review and benign protocol validation.
The initial negative socket attempt used an unresolved path and proves neither production safety nor failure.
The review preserves that evidence limit rather than reporting a positive escape.

## Source stability

The lead captured fingerprints for 14 core files at review start.
At 2026-10-22 18:13:18 UTC, all 14 files retained those fingerprints.
The final check found 14 core files and 12 additional files unchanged against their recorded fingerprints.
The final validation round adds 14 passing scratch checks: six cookie cases and eight lifecycle cases.
The study changed no production code.
