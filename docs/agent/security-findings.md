# Security findings

Contributor notes. What a security review found, what was done about it, and what was
deliberately not done. Add to this file rather than leaving a finding in a scan report
nobody reads again.

Severity here is **not** the raw severity a scanner reports. It is severity against the
threat model in [the security model](../explanation/security-model.md): the sandbox
exists to keep real credentials away from the agent and to gate outbound traffic by
destination. A finding that only lets one conversation read another is low by that
measure, because that is a documented property rather than a defect.

Every entry names a file and a line so it can be re-checked. A line number drifts; the
rule or function name is the durable part.

## Reviews so far

| When | Scope | Revision | Findings |
|---|---|---|---|
| 2026-08-01 | Whole repo, external scanner | `2c03555` | 32, all remediated in `28b7ece`..`744d3b6` |
| 2026-08-14 | Sandbox, brokers, frontends, state | `9f96b3f` | 39 after dedupe |
| 2026-08-14 | `upgrade.py`, `turns.py`, new surface | `8390b33` | 6 |
| 2026-08-15 | PR #29 tree, `/code-review` run plus verification | `0c7cd4c` + working tree | 3 fixed, 2 open |

The August 1 scan predated the Linux jail, `netns_relay`, the session boundary, and the
upgrade path, so none of that was covered by it.

## Fixed

| Finding | Where | Evidence |
|---|---|---|
| A workspace name reached `_PROJECT_DIR` unsanitized, so a traversal made the data dir agent-writable, and `cron.yaml` from there is unjailed shell with the daemon's environment | `agent.workspace_path` | Traversal reproduced, then contained; regression tests for both the Slack and journal entry points |
| `_CODEX_HOME` collapsed onto `~/.codex` with `scope_sessions` off, so the write allow nullified the deny above it and `config.toml`, `AGENTS.md`, `hooks.json` became agent-writable | `sandbox._macos_wrap` | Live jailed write into a real `~/.codex`, refused after the fix and succeeding with the one line reverted, under both profiles |
| An unrecognised `sandbox.mode` resolved to `off`, and both startup gates return early unless the mode is `jail`, so a typo produced the posture the operator was avoiding | `sandbox.mode` | All six mode values probed |
| The cross-thread read denies and the `state/` write deny sat above allows that could re-open them | `seatbelt/*.sb` | One `extra_paths` entry of `$HOME` re-opened both stores; tests now assert rule position, not just behaviour |
| `sandbox.extra_paths` had no validation, so the remedy `_JAIL_GUIDANCE` tells the agent to relay was also the bypass: an entry of `$HOME` re-opened `~/.ssh` and `~/.aws` | `sandbox._extra_read_paths` | Entries resolving to the home, an ancestor of it, or a credential store in either direction are logged at ERROR and dropped, the rest still granted; a test asserts every `_DENY_PROBES` entry is out of reach |
| The command-broker path guard missed an attached short option, a second `=`, a traversal attached to a short option, and a relative path through a planted symlink, on a broker that runs outside the jail with the operator's credential | `commands._unsafe_path_argument` | All four verified passing the old guard and refused by the new one; the guard now reads every `=` segment and the short-option tail, and resolves each candidate against the settled cwd |
| The agent's uv cache was the operator's, and `upgrade.run` installs from it outside any jail with the TUI's full environment | `seatbelt/*.sb`, `sandbox.agent_env` | Write grant moved to `DATA_DIR/uv-cache` and published as `UV_CACHE_DIR`; a live jailed write to `~/.cache/uv` reports "Operation not permitted" under both bases, while `uv venv` and `uv pip install --offline` complete and populate the new cache |
| The startup self-test ran in one daemon of the two that spawn jailed agents, so a jail that could not hold `state/` stopped Slack loudly while the job worker kept draining the same queue across it | `sandbox.verify_boundary`, `jobs/cli._run` | Both halves now run from the worker's composition root before it claims work, fatal with exit 2; ordering and the before-claim position are asserted in `tests/jobs/test_cli.py` and `tests/test_sandbox.py`. Cron is deliberately not gated: it runs shell and never calls `agent.run` |
| `verify_denials` could pass having proven nothing: a probe that raised was dropped from the results, so an all-timeout run logged `0/0 probed paths confirmed denied` at INFO and returned success | `sandbox.verify_denials` | A probe that could not run is now `UNTESTED` and fatal, and every probe lands in the results dict. Reverting the `UNTESTED` return makes `test_a_probe_that_cannot_be_spawned_refuses_to_start` and `test_a_probe_that_hangs_is_abandoned_not_awaited_forever` fail. `ABSENT` stays non-fatal: the file is genuinely not on the host, and `preflight` has already proven the jail starts |
| The TUI watch pane rendered remote text as live Rich markup, unlike every log and tail path. The label is a workspace name, which on the trusted-bot path carries a Slack `username` the poster chooses | `dashboard._refresh_watch_session` | All five sinks in that method (one header, plus the two empty-state pairs) now go through the existing `session_format._safe`; `label` itself stays raw because the workspace path is built from it. A label holding `[/bold]` and one holding `[/Users/…/thing]` both raise MarkupError without the change |
| `never_ask` is an exact-match lookup while `_DNS_SAFE_HOST` permits a trailing dot, so `metadata.google.internal.` skipped the permanent-block tier and was downgraded into an operator prompt. Defanged downstream, so the harm was the offer existing on the one tier that exists never to be offered | `egress.canonical_host` | One canonical form for the CONNECT host and for every configured set, so the two sides of a membership test cannot disagree. The dotted form of an `egress.allow` host also stopped re-prompting, and a bare `.` is now refused as not a hostname rather than reaching the resolver. Four cases fail without the change |
| The pending-turn journal was 0o644 (measured), holding the verbatim text of every unanswered turn | `turns.TurnJournal._write` | Created through `os.open` at 0o600 with an explicit chmod, the same shape `codex_state.write_thread_id` uses for the same class of file. The chmod is what covers a permissive umask and a temp file an older build left behind; both cases fail without it. The fixed temp name is kept on purpose, reason in the comment |
| The replay cap and the TTL were evadable by a tampered journal: a negative `replays` needed ~10^18 restarts to reach the cap, a NaN `recorded_at` made every TTL comparison False, and `chat_id: true` passed the `int` check and became chat_id 1 | `turns.PendingTurn.from_dict`, `turns._as_float` | Clamped at the one boundary where a disk record becomes an object, so both gates in `take()` are sound without touching them. Bad entries are dropped, which is what `_read` already does with a `None`. A boolean chat id, a NaN and an Infinity `recorded_at`, and a `replays` of -10^18 all fail without the change |
| A thread could permanently defeat its own operator guardrails: it replaced a shared-entry link inside its writable `CODEX_HOME` with a directory, `unlink()` raised `IsADirectoryError`, and the suppressed `OSError` meant `hooks.json`, `AGENTS.md` and `config.toml` never came back for that thread | `codex_state._clear_link_site` | The link site is cleared through one helper that removes a planted directory, bounded to `HOMES_DIR` and never descending a symlink; every link failure now logs. A test plants the directory and asserts the operator's `hooks.json` is back on the next `ensure_home`, and fails without the change |
| Thread ids were spliced into glob patterns unescaped, so `*` matched every rollout in the tree: `adopt_rollout` copied another thread's into this workspace's `CODEX_HOME`, and `_find_codex_rollout` returned an unrelated conversation for the next prompt's handoff context | `codex_state.rollout_glob`, `codex_state._valid_thread_id` | Both sites now share one `glob.escape`d pattern, and the id charset is checked on the read side and the write side. 10 adversarial cases (`*`, `?`, `[a-c]*`, `../elsewhere`, whitespace) fail without the change. Charset derived from 14 real mappings on a deployed data dir, all 36 characters of lowercase hex and hyphen. Never reachable: the id comes only from codex's `--json` control event, so this is defence in depth |
| The NaN/Infinity clamp on `recorded_at` missed the huge-integer shape: a raw integer literal past ~1.8e308 is valid JSON, passes the `isinstance(int)` check, and `float()` raises `OverflowError` on it, which propagated through `take()` and crashed the daemon at startup | `turns._as_float` | Reproduced live with a 400-digit `recorded_at` (`CRASH: OverflowError int too large to convert to float`), then caught: `OverflowError` returns `None` and the entry is dropped the way a NaN is. `from_dict` is now total, so the journal read path cannot raise. `test_a_recorded_at_too_large_for_a_float_is_dropped` fails without the change |
| `sandbox.extra_paths` could re-open a *file*-level credential deny: the docstring claimed the home rule covered them, but an entry naming `~/.netrc` itself is neither the home nor an ancestor nor inside a listed store, so it was granted and re-opened exactly one denied file | `sandbox._CREDENTIAL_FILES`, `sandbox._extra_path_refusal` | 21 file-level paths mirrored 1:1 from the profile's literal denies, checked bidirectionally like the stores. `~/.netrc`, `~/.cargo`, `~/.config/hub` refused while `/opt/homebrew`, `/usr/local`, `/tmp` still granted. `test_every_file_level_credential_is_out_of_reach_of_extra_paths` walks the list and fails without the change |
| The Linux jail left `~/.codex/prompts` and `~/.codex/auth.json` writable: `_CODEX_PROTECTED` omitted them, so a turn could plant standing instructions codex reads on every run, or swap the operator's OAuth token | `sandbox._CODEX_PROTECTED` | `prompts` and `auth.json` added to the write-deny list, `prompts` to the dirs. `skills` deliberately not added: codex writes its own tree inside it (measured: 242 touches and a `skills/.system/` tree per turn), so a read-only mount there breaks codex. `test_linux_write_denies_cover_codex_prompts_and_auth` fails without the change |

## Open

Ordered by severity against the threat model above.

### Credential reach

**`fs: allow-reads` leaves credential stores readable.** Measured on a real home: the
Firefox profile tree (holding `logins.json` and `key4.db`) and `~/Library/Messages/chat.db`.
`~/.ssh`, `~/.aws` and `~/Library/Keychains` are correctly denied. This is the known cost
of enumerate-the-bad, which the profile itself admits. `deny-most` does not have it, and
is the posture to deploy.

**`~/.claude/.credentials.json` is readable under the Linux jail.** The whole `~/.claude`
tree is re-exposed read-only and only `history.jsonl` is masked. On macOS the equivalent
lives in the Keychain, which the profile denies, so an operator reading the profile as
"keychain denied" gets no such protection on Linux. Partly already recorded under "Known
limits" in the security model, but the platform asymmetry is not. Unconfirmed: the test
VM shares the macOS home, so no such file existed to probe.

**A planted symlink turns the session grant into a filesystem-wide grant.** With
`scope_sessions` off, `projects/` is writable, so a turn can create a symlink named with
its own workspace hash. `_claude_session_paths` realpaths that path when building the
grant, so turning `scope_sessions` on — the recommended hardening — hands the profile the
symlink's target. On Linux `_ensure_session_mount_sources` also `mkdir`s through it as the
unjailed daemon. `_preflight_protected_symlinks` checks only the six `_codex_protected`
entries.

### Egress

**`network-bind` and `network-inbound` are not denied.** `jail.sb` denies outbound only.
Measured: a jailed process bound `0.0.0.0`, a client outside the sandbox connected over
the host's LAN address, and the payload left without touching the CONNECT proxy or its
logs.

Closing it is not free, and the reason is measured rather than assumed. SBPL cannot
express "bind loopback, refuse the network":

- `(local ip "127.0.0.1:*")` is rejected outright — *host must be `*` or `localhost`*.
- `(local ip "localhost:*")` permits a `0.0.0.0` bind too, so it does not discriminate.
- `(deny network-inbound)` blocks the bind itself, loopback included, and an
  `(allow network-inbound (remote ip "localhost:*"))` does not rescue it: the check
  happens at bind, before any peer exists.

So it is all-or-nothing, and denying it costs every listener the agent runs, which
`jail.sb` advertises as supported. Left open deliberately: the LAN path needs an attacker
already on the network, while the outbound gate holds.

**Loopback is fully open by default.** `sandbox.broker_only_loopback` is off unless set,
so `_loopback_specs` returns `localhost:*` for every slot. `SessionEgress` and
`SessionPermissions` start one unauthenticated service per chat, so one session's agent
can CONNECT through another session's proxy and inherit every host that operator
approved. Closes by defaulting the setting on, or by authenticating both services the way
the credential and command brokers already are.

**`--web` launches a browser outside the sandbox.** Every allowlisted `gh` and `acli`
subcommand accepts `-w/--web`, the allowlist is a leading-prefix gate, and the broker is
unjailed. The URL is attacker-chosen, so data can ride in the query string. Bounded to
github.com and the configured Atlassian site.

### Execution that outlives the turn

**`~/.claude/shell-snapshots` is writable and the CLI sources it** on a later Bash tool
call. The grant is already unsupported by measurement: a jailed turn that provably ran a
Bash tool wrote no snapshot. Kept only because dropping it touches the pty path, which
has never run under the jail.

**`~/.claude.json` is writable**, and it holds global `mcpServers` definitions and
trust-dialog state. Inconsistent with `_PROJECT_WRITE_DENIES`, which denies the
workspace-local `.mcp.json` for exactly this reason.

**`plugins/cache` is writable for both backends**, and holds the code a manifest points
at. The `plugins/` root is denied so a manifest cannot be added; the code below it is not.

**Project agent-instruction files are writable.** The terminal project deny block covers
`.mcp.json`, `.git/hooks` and the shell rc files, and omits `.claude/settings.json`,
`.claude/settings.local.json`, `.claude/hooks/`, `CLAUDE.md`, `AGENTS.md`, `.envrc`.
Settings hooks run commands and skip the approval path. Low against this threat model,
since those hooks still run inside the jail, but it is the same capability `.mcp.json`
was denied for.

**`codex_state.shared_link_targets` has no containment check.** It realpaths every entry
under `~/.codex` and adds the result to the Linux read-only set. An operator who symlinks
`~/.codex/prompts` to `$HOME` returns the whole home directory read-only, silently.

**The upgrade payload has no integrity gate.** No tag pin, no `--verify-signatures`, no
`--locked` or `--frozen`, so a changed `pyproject.toml` is re-resolved and its build
backend runs unjailed. Matters most in the documented default posture, `sandbox.mode: off`,
where an already-compromised turn's file writes become code the operator later runs on
purpose.

### The self-test

**`_runtime_read_paths` resolves `argv[0]` only.** In pty mode `argv[0]` is `claude-pty`,
which execs a different binary plus tmux, and the five slots are already full. Under
`deny-most` `$HOME` is opaque, so `claude` is invisible and `execvp` fails. The natural
operator remedy is a wide `extra_paths` entry, which is the finding above.

### Cross-conversation writes

Reads across conversations are a documented property. Writes are not: writing into
another conversation's transcript or into shared memory makes a different user's turn
act.

**`DATA_DIR/memory` is one shared writable tree** across every thread and sender, and the
system prompt has the agent read it every turn. Not fixed by `scope_sessions`.

**Other threads' claude transcripts are writable** when `scope_sessions` is off, not only
readable.

### Frontends and journal

**Forwarded and quoted Slack messages carry no untrusted-content boundary.** The allowlist
gates the forwarder, not the original author, and `system_prompt.md` says nothing about
the body of a forward being untrusted.

**Suggestion labels are replayed with the tapper's full sender authority.** Model-generated
text, tapped by a human, is fed back wrapped in the real sender's `[from-id:]` marker.

**A `recorded_at` of 0 is still exempt from the TTL.** `take()` gates on
`if entry.recorded_at and ...`, so a tampered entry can skip its TTL by recording no
timestamp at all. Left as it is: 0 is the dataclass default and the documented
"not recorded" case, `test_a_turn_with_no_timestamp_is_not_treated_as_expired` asserts it,
and the replay cap still bounds such an entry. Closing it means deciding that a record
without an age is a record to drop, which is a behaviour change rather than a clamp.

**A planted symlink at a conversation's own workspace path makes the next turn
crash.** `workspace_path` raises `ValueError` when `resolve()` follows the link out of
the tree (its own regression test asserts this), and `orchestrator._process` has no
catch: the turn was already `mark_dispatched`, so the message is lost and the chat's
drain task dies. Self-DoS only -- the agent can only break its own workspace -- and
catching it would change the intended fail-loud behaviour, so it is left open pending
a decision on what a compromised workspace should do to a turn.

**The TUI watch pane builds its path from the raw workspace name.** The display is
sanitized (`session_format._safe`), but `DATA_DIR / "workspaces" / label` uses the raw
label, so a name containing `..` makes the pane resolve a session log from outside the
tree and render it on the operator's screen. Display-only, on the operator's own
machine, and the label is daemon-internal state; left open.

### Approvals

These only apply with `permissions.mode: ask`.

**Approval digests collide across whitespace.** `_flatten` is `" ".join(text.split())`, so
a compound command's canonical form equals a simple command's `shlex.join`. Demonstrated:
an approved `echo curl <url>` covers `echo\ncurl <url>`, which is two commands, on the
same standing grant. `apply_patch` flattens the patch body before hashing, so an approved
line inside an `if` block replays dedented outside it.

**`/decide` and `/notify` carry no bearer token**, unlike the command and credential
brokers. A `/notify` loop drives Escape keystrokes into the operator's pty pane.

**pty dialog grants use a 48-bit truncated digest** of wrap-damaged pane text, while the
other two subjects were widened to the full digest.

## Accepted, not fixing

**One conversation can read another's transcripts** when `scope_sessions` is off. This is
the design position, recorded in the security model. Protection at that level is an
instruction in `_JAIL_GUIDANCE`, not a boundary.

**`--cap-drop ALL` and `no_new_privs` are absent from the bwrap argv.** Measured inside
the jail: `CapEff=0`, `CapBnd=0`, `NoNewPrivs=1`. bubblewrap's defaults already cover it.

**Host `/tmp` is bound read-write**, `--ro-bind / /` is recursive, and ipc/uts/cgroup are
not unshared. Fingerprinting surface on a jail whose filesystem is readable by design.

**`process-info*` and `signal` are granted, and the PID namespace is not unshared.**
Already recorded under "Known limits". Measured: the process table and other processes'
`cmdline` are readable and signalling is permitted, but `/proc/<pid>/environ` is *not*
readable — bwrap's user namespace blocks it, so this is not a credential leak.

## Checked and clear

Recorded so a later review does not redo the work. Each came back negative with a reason:
SBPL parameter injection; unresolved profile parameters; deny truncation (no deny is
written against a fixed slot, so truncation fails closed); subpath boundary matching;
`process-exec` escape (seatbelt policy is inherited across `exec`); bwrap argv injection;
netns relay reachability; the read-only remount pass; mask-coverage TOCTOU; bind-kind
misuse; command allowlist smuggling (no shell, and cobra rejects unknown flags before
dispatch); a shell or file-write primitive inside the allowed CLI surface; DNS rebinding
and egress suffix matching; a credential reaching the agent; the approval TTL clock;
destructive path bugs at all three `rmtree` sites; workspace hash collisions; mapping-file
TOCTOU; log content leakage; sender-authorization ordering in both frontends; `cron.yaml`
writers; `interim.py` bounds; command injection into the upgrade string; wrong-conversation
replay from a partial journal record; submodules and the checkout's own `.git/hooks` and
`.git/config`; and upgrade failure handling.

## Not covered by any review yet

- claude-pty under the jail, on either platform.
- MCP servers under the jail.
- `permissions.mode: ask` end to end: the fourth loopback slot has never been filled by a
  real turn.
- A real `api.anthropic.com` leg for the claude backend under the jail. Every macOS
  validation used a loopback stub.
- Jobs and cron under the jail. All validation went through a chat turn.
- Two concurrent jailed turns, so the per-turn `_SESSION_ENV` ContextVar is untested under
  real concurrency.
- Codex under the macOS jail, and claude under the Linux jail. Both share the policy layer;
  only the platform mechanism differs.
- Six TUI modules read by diff only, not line by line: `state.py`, `screens/config_picker.py`,
  `screens/doctor.py`, `screens/history.py`, `env_editor.py`, `supervisor.py`.
