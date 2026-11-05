# Concrete suggestions and compatibility risks

> Historical snapshot: October 23, 2026 (Asia/Taipei).
> Implementation status requires recheck against the current source.
> References to current behavior describe the reviewed snapshot.

These proposals describe the review snapshot.
Keep the trusted host command shim.

1. **Keep host terminal control outside the jailed child.**

   Remove child access to the host tmux socket.
   Protect host-consumed launch scripts and environment files from child reads and writes.
   Keep creation, capture, approvals, attachment, and cleanup on an exact host-owned pane.
   Relevant files: `seatbelt/jail.sb`, both filesystem profiles, `tmux.py`, and backend launch code.

   Compatibility risk: immediate socket removal can break Claude terminal mode and pane features.
   Check the external wrapper's child-launch interface before changing its policy.
   Check attachment, approvals, resize, resume, cleanup, and concurrent turns.
   Drain old frontend and job turns before claiming the replacement boundary.
   The source establishes the authority concern. A live escape remains unverified.

2. **Set the complete environment at final child launch.**

   Replace additive exports in `backends/codex.py::_pane_env_file` with the complete selected environment.
   Include removals and explicitly required terminal variables.
   If staging fails, stop that launch or select native fallback before interactive execution begins.

   Compatibility risk: inherited variables can disappear, and native fallback changes terminal features.
   Check successive turns against an existing server.
   Include omitted variables, empty values, values equal to daemon defaults, quoting, and non-ASCII text.
   Preserve account selection, session identity, active approvals, and resume.

3. **Separate cookie state by broker route authority.**

   Replace the shared cookie session in `broker.py` with route-owned state.
   Keep the default route separate from named accounts.
   Reset cookie state when a route changes accounts.
   Share alias state only when a supported consumer requires it and the authority contract permits it.

   Compatibility risk: workflows can depend on implicit sharing between routes or aliases.
   Check same-route continuity, A/B/default isolation, concurrency, route replacement, and cleanup.
   Cookie storage and forwarded Cookie or Set-Cookie headers need separate decisions.
   Use a disabled jar only after supported routes demonstrate stateless compatibility.
   The synthetic checks demonstrate state mixing, not provider takeover.

4. **Preserve complete approval identities.**

   In `permissions.py`, hash original Bash input and patch text without lossy normalization.
   Keep display formatting separate.
   Do not reuse standing grants from incomplete terminal captures.
   Preserve deliberate Write/Edit path grants, fetch host grants, and generic-tool grants.

   Compatibility risk: syntax-distinct commands can ask again, and incomplete terminal captures lose standing-grant reuse.
   Check quote context, significant whitespace, patch text, identical-input reuse, and truncated terminal bodies.
   The current design intends exact-command scope; the normalization loses distinctions.

5. **Fence delayed approval delivery.**

   Mark the service closed before asynchronous cleanup.
   Cancel and await detached relay tasks in `PermissionService.stop`.
   Bind final delivery to the originating turn and pane instance.
   Keep that identity check effective through the final asynchronous delivery operation.

   Compatibility risk: teardown drops pending answers.
   Preserve active approvals, session grants, and unrelated live sessions.
   Check delayed allow, delayed deny, close, pane replacement, and valid current delivery.
   A mocked test demonstrates a late callback. Live replacement-pane effects remain unverified.

6. **Recognize AWS binary-file input syntax.**

   In `commands.py`, recognize `fileb://` using AWS filename rules.
   Apply the existing approved-root decision to its path.
   Preserve workspace inputs, explicit roots, and existing command prefixes.

   Compatibility risk: previously accepted outside-root inputs become refused.
   Check relative and absolute workspace paths, approved roots, outside paths, and symlinks.
   Do not assume generic URL decoding matches AWS filename behavior.
   Admission checks demonstrate the parser gap. Actual file disclosure remains unverified.

7. **Correct the security guarantees.**

   Update the broker, security model, approval, and configuration documentation where claims conflict.
   Describe credential capabilities as broker-object-lived and command capabilities as turn- or job-bound.
   State that command revocation prevents new admission without cancelling accepted subprocesses.
   List intentional backend-held credentials, writable caches, and platform-specific coverage.

   Compatibility risk: none from accurate documentation.
   Check each statement against implementation and update duplicate claims together.
   Effective-state diagnostics remain an optional later extension to Doctor.

Deferred questions:

- Keychain service restrictions need checks with synthetic stores and required public certificate trust.
- Cross-account capability visibility needs isolated validation with synthetic bearer values.
- Pending egress policy edits need an explicit contract before changing request invalidation.
- Endpoint authentication and shorter provider capability lifetimes need their own requirements and compatibility checks.

The recommendation adds no mandatory second sandbox for trusted command tools.
The detailed evidence and limitations are in [the study](study.md) and [the assurance review](reviews/assurance.md).
