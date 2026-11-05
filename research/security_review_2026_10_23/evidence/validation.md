# Final custody validation round

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

This report records the last assigned validation round.
It does not claim overall convergence before 2026-10-22 18:41:03 UTC.
No new finding discovery occurred.

I used harmless cookie markers and mocked callbacks only.
The cookie checks use temporary HTTP servers on loopback.
The lifecycle checks make no socket or terminal calls.
I used no real provider, token, account file, tmux server, or process readback.
I made no repository changes and ran no git operations.

## Executed checks

The scratch file is `test_final_custody_validation.py` beside this report.
It uses aiohttp 3.13.3 and the repository's isolated test fixtures.
Its cookie model and lifecycle fence are test code, not cotf implementation changes.

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests .venv/bin/python -m pytest -q -c pyproject.toml -p no:cacheprovider -p conftest <review_scratch>/test_final_custody_validation.py
..............                                                           [100%]
14 passed in 8.68s
```

Six checks concern cookies. Eight checks concern the scratch lifecycle fence.
The cookie origin uses `localhost`, with the same host and cookie path for every session.
It does not rely on aiohttp's rejection of cookies from literal address origins.
Every marker is ordinary test text, such as `marker-A`.

## Cookie results and their limits

| Check | Result | What it proves | What it does not prove |
|---|---|---|---|
| Same session continuity | A session sends its own earlier marker on a later request | Separate sessions can preserve the current same-route cookie behavior | Real provider compatibility or authentication semantics |
| A, B, and default isolation | Each session retains only its own marker | Independent aiohttp jars separate state even when host and cookie path match | Cotf currently selects the correct separate session |
| Concurrent A, B, and default requests | Concurrent seeding and nine overlapping reads return the correct markers | Independent jars maintain those identities through this concurrent library scenario | All request schedules, retries, refresh events, or provider responses |
| Independent route aliases | A and its declared alias have separate markers | Per-route sessions can keep aliases separate without losing each route's own continuity | That alias sharing is required by an existing provider |
| Explicit shared aliases | A and its declared alias observe each other's marker updates | An explicit session grouping shares state as intended by that grouping | That cotf can infer a correct account identity or authority group |
| Concurrent alias reads | Reads follow the selected independent or shared grouping | Library behavior follows session ownership rather than alias spelling | Safe grouping across aliases with different route permissions |

The alias map is explicit synthetic input.
It does not read login files or infer real identity from headers.
The default route always has separate ownership in this model.

These results support separate sessions as a compatible correction for proxy state sharing.
They do not establish that the correction exists in cotf.
The current broker still uses its original shared client session.
This review changed no production code.

The conservative implementation groups by route authority first.
Two aliases of one account do not have to share a jar.
Shared cookies can become another coupling between aliases with different permitted operations.
Only share alias state when an existing consumer needs it and the authority contract permits it.

Managed cookie state still needs a login replacement rule.
This model does not simulate login replacement, token refresh, source recovery, or account deletion.
It cannot settle those compatibility requirements.
Preserving a jar forever under a route name is not a complete account lifecycle design.

## Cookie policy conclusion

Separate sessions preserve more existing behavior than a globally disabled jar.
They retain same-route continuity while removing automatic A/B/default sharing.
That makes them the compatibility-first direction when provider cookie dependence is unknown.

`DummyCookieJar` remains the simpler design for a verified stateless route.
It removes the need to define cookie ownership across login changes.
Its compatibility cost is the loss of same-route cookie continuity.

Storage and wire headers remain separate controls.
Earlier harmless checks establish that a disabled jar still passes explicit request cookies.
They also establish that response `Set-Cookie` remains visible through the current header filter.
Those earlier checks are in `test_custody_protocol_choices.py`.
No new header behavior is inferred from this round's jar checks.

No real ChatGPT cookie or provider authentication cookie was inspected.
The demonstrated problem remains proxy state sharing.
Provider takeover and provider credential disclosure remain unproved.
Header stripping needs its own route contract and compatibility acceptance.

## Mocked lifecycle fence results

The scratch fence captures a generation before it reads a dialog.
It checks that generation and a closed flag after each await.
Its final delivery callback is synchronous and has no external effect.

| Check | Cases | Result |
|---|---|---|
| Current dialog and current generation | Allow and deny | The callback runs exactly once with the original result |
| Service closes while the answer waits | Allow and deny | The delayed callback does not run |
| A new turn replaces the generation while the answer waits | Allow and deny | The delayed callback does not run |
| Service is already closed | One case | Neither dialog read, question, nor delivery occurs |
| Generation changes while dialog read waits | One case | The model suppresses the question and delivery |

The controls prove that the fence preserves valid delivery in this model.
The delayed cases prove that the modeled state changes suppress stale callbacks.
The checks cover stale allow and stale deny results.
A denial can also be an unwanted input to a replacement terminal.

This fence is not present in `PermissionService`.
An earlier test exercised that class with mocked read, answer, and runner cleanup functions.
That test confirms a detached relay can call its mocked answer after `stop()` returns.
See `test_custody_protocol_choices.py::test_stop_does_not_fence_a_delayed_mock_dialog_callback`.
The earlier result is a mocked lifecycle result, not live terminal behavior.

The new scratch model proves a bounded proposed suppression mechanism.
It does not prove a production fix, terminal instance identity, or atomic input delivery.
The final callback is synchronous by construction.
Real asynchronous delivery can add another interval between a state check and its external effect.
An implementation must preserve the current generation through that final controlled effect.
It must also set closed state or advance the generation before asynchronous cleanup begins.

No callback sent terminal input or invoked a command.
No temporary or existing tmux server participated.

## Acceptance requirements that remain

1. Route selection must choose the intended cookie session in actual cotf code.
2. All created client sessions need cleanup, including partial startup failures.
3. Default and named account state must remain separate.
4. Alias sharing must follow an explicit authority contract rather than hostname equality.
5. Managed jars need checked login replacement and refresh behavior.
6. Stateless routes need provider compatibility evidence before rollout.
7. Cookie request filtering and response filtering need independent tests.
8. Production terminal callbacks need a turn or pane generation and a closed-state fence.
9. Delivery must retain that fence through its final external effect.
10. Existing valid approvals, cancellation, fallback, and cleanup behavior must continue to work.

The source review and these checks reduce uncertainty about the proposed mechanisms.
They do not establish live platform enforcement or real provider security impact.
