Historical expert review. The final [study](../study.md) and [suggestions](../suggestions.md) incorporate later corrections.

# Containment check of the report draft

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

The pane, environment, Linux dotenv, and Keychain sections generally preserve the evidence limits.
The draft does not claim a demonstrated live escape or require a second sandbox for trusted tools.
I performed no new investigation or runtime check.

## Corrections

1. Qualify the central custody description.
   “Protected credential stores remain outside the agent's filesystem view” sounds unconditional.
   Use: “In jail mode, the policy hides covered credential stores while the broker runs outside the jail.”
   This avoids implying that default-off or environment-only mode establishes filesystem custody.
   It also avoids certifying every alias and service channel.

2. Make the pane filesystem statement platform-specific.
   Replace “The jail also grants writes to shared pane files” with:
   “Both macOS bases grant writes to shared pane files that the host launcher consumes.”
   Linux currently has no visible panes regrant beneath its opaque data directory.
   Its live hosted behavior remains unverified in this study.
   Source: `src/claude_on_the_fly/sandbox.py:2195` and `:2251`.

3. Add one sentence separating backend modes.
   Suggested text: “Codex native and both Ollama paths use plain execution; the hosted Codex path concerns terminal mode.”
   Claude terminal mode wraps the external launcher itself.
   Pane metadata alone does not establish an actual terminal launch.
   Sources: `src/claude_on_the_fly/backends/codex.py:1149`, `:1495`, and `src/claude_on_the_fly/agent.py:2009`.

4. Keep Keychain service compatibility unresolved explicitly.
   Apple establishes the file/service distinction, but its archived document does not establish current service names or cotf access.
   Targeted service denies remain a relevant custody candidate, rather than an implemented or validated repair.

5. Avoid wording that suggests real capability readback is the required next test.
   Replace “another real capability” with “another account's capability under the real jail, using synthetic values.”
   No operator credential or provider account is necessary to validate that delivery contract.

## Release requirements to add or link explicitly

1. End old turns before claiming the new pane boundary.
   Existing agents retain their earlier socket and filesystem policy.
   A next-turn reload does not revoke that process policy.
   Coordinate every frontend and jobs producer before reusing the host server.

2. Check complete final environment selection against an already-running server.
   Cover removals, empty values, and requested values equal to daemon defaults.
   Keep values out of arguments and diagnostic logs.
   Staging failure must select native fallback before interactive execution begins.
   Reject the existing inherited-environment continuation at `src/claude_on_the_fly/backends/codex.py:782`.

3. Deny host launch-file reads and writes explicitly under the global-read macOS base.
   Removing its read grant alone cannot establish privacy.
   Check resolved paths and later overlay grants.
   Preserve the final child jail wrapper while host launch, approvals, capture, and cleanup use one exact target.

4. Gate Claude fallback on unchanged provider, account, session identity, resume behavior, and active native approvals.
   Describe lost terminal attachment, statusline fields, and compaction display.
   Do not run an external wrapper outside the jail until its actual child-launch contract has a source review.

5. Gate service denies on both custody and compatibility.
   Required consumers include public certificate trust, native startup, and selected daemon-side login handoff.
   Later authorized checks need synthetic stores on an isolated account or test machine.
   Public trust success alone does not prove credential denial; credential denial alone does not prove usable startup.

These requirements are already detailed in `containment-design-review.md` and `containment-falsification.md`.
A short release-gates paragraph can link those artifacts instead of repeating the complete matrices.

No further containment correction is required before the root review's time gate.
That statement concerns draft wording, not runtime assurance or final convergence.
