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
| The Linux dotenv mask swept with `followlinks=False`, so a symlinked directory inside a granted config tree was never descended into and the token file beneath it stayed readable inside the jail. The mask list also held the walked path rather than the real one, and a mask is a bind mount over a path, so even a found file stayed readable under its real name. macOS was never affected: one unanchored regex matches either path | `sandbox._dotenvs_under`, `sandbox._linux_masked` | Measured on a real Linux host where `~/.claude/skills` is a symlink to a skills repository: `cat` of that skill's `.env` inside the jail returned the token before the change and is refused after it. The sweep now follows directory symlinks with a visited set of realpaths to break loops, and masks realpaths. Both halves fail without the change |
| A workspace name reached `_PROJECT_DIR` unsanitized, so a traversal made the data dir agent-writable, and `cron.yaml` from there is unjailed shell with the daemon's environment | `agent.workspace_path` | Traversal reproduced, then contained; regression tests for both the Slack and journal entry points |
| `_CODEX_HOME` collapsed onto `~/.codex` with `scope_sessions` off, so the write allow nullified the deny above it and `config.toml`, `AGENTS.md`, `hooks.json` became agent-writable | `sandbox._macos_wrap` | Live jailed write into a real `~/.codex`, refused after the fix and succeeding with the one line reverted, under both profiles |
| An unrecognised `sandbox.mode` resolved to `off`, and both startup gates return early unless the mode is `jail`, so a typo produced the posture the operator was avoiding | `sandbox.mode` | All six mode values probed |
| The cross-thread read denies and the `state/` write deny sat above allows that could re-open them | `seatbelt/*.sb` | One `extra_paths` entry of `$HOME` re-opened both stores; tests now assert rule position, not just behaviour |
| `sandbox.extra_paths` had no validation, so the remedy `_JAIL_GUIDANCE` tells the agent to relay was also the bypass: an entry of `$HOME` re-opened `~/.ssh` and `~/.aws` | `sandbox._extra_read_paths` | Entries resolving to the home, an ancestor of it, or a credential store in either direction are logged at ERROR and dropped, the rest still granted; a test asserts every `_DENY_PROBES` entry is out of reach |
| The command-broker path guard missed an attached short option, a second `=`, a traversal attached to a short option, and a relative path through a planted symlink, on a broker that runs outside the jail with the operator's credential | `commands._unsafe_path_argument` | All four verified passing the old guard and refused by the new one; the guard now reads every `=` segment and the short-option tail, and resolves each candidate against the settled cwd |
| The agent's uv cache was the operator's, and `upgrade.run` installs from it outside any jail with the TUI's full environment | `seatbelt/*.sb`, `sandbox.agent_env` | Write grant moved to `DATA_DIR/uv-cache` and published as `UV_CACHE_DIR`; a live jailed write to `~/.cache/uv` reports "Operation not permitted" under both bases, while `uv venv` and `uv pip install --offline` complete and populate the new cache |
| The startup self-test ran in one daemon of the two that spawn jailed agents, so a jail that could not hold `state/` stopped Slack loudly while the job worker kept draining the same queue across it | `sandbox.verify_boundary`, `jobs/cli._run` | Both halves now run from the worker's composition root before it claims work, fatal with exit 2; ordering and the before-claim position are asserted in `tests/jobs/test_cli.py` and `tests/test_sandbox.py`. Cron is deliberately not gated: it runs shell and never calls `agent.run` |
| The macOS `deny-most` profile granted the project subpath but not stat() on its parents under the opaque `$HOME`, so git's repository discovery failed on the home directory and no git command worked inside a workspace | `seatbelt/fs-deny-most.sb` `_ANCESTOR_*`, `sandbox_macos.home_ancestors` | `git init` and `git clone --shared` measured failing with "Operation not permitted" on macOS 26 under the stock profile and passing with a metadata literal per ancestor; the parity suite now runs `git init` in the workspace on both platforms and checks a sibling of the path stays hidden |
| `verify_denials` could pass having proven nothing: a probe that raised was dropped from the results, so an all-timeout run logged `0/0 probed paths confirmed denied` at INFO and returned success | `sandbox.verify_denials` | A probe that could not run is now `UNTESTED` and fatal, and every probe lands in the results dict. Reverting the `UNTESTED` return makes `test_a_probe_that_cannot_be_spawned_refuses_to_start` and `test_a_probe_that_hangs_is_abandoned_not_awaited_forever` fail. `ABSENT` stays non-fatal: the file is genuinely not on the host, and `preflight` has already proven the jail starts |
| The TUI live view rendered remote text as live Rich markup, unlike every log and tail path. The label is a workspace name, which on the trusted-bot path carries a Slack `username` the poster chooses | `dashboard._refresh_watch_session` | All five sinks in that method (one header, plus the two empty-state pairs) now go through the existing `session_format._safe`; `label` itself stays raw because the workspace path is built from it. A label holding `[/bold]` and one holding `[/Users/…/thing]` both raise MarkupError without the change |
| `never_ask` is an exact-match lookup while `_DNS_SAFE_HOST` permits a trailing dot, so `metadata.google.internal.` skipped the permanent-block tier and was downgraded into an operator prompt. Defanged downstream, so the harm was the offer existing on the one tier that exists never to be offered | `egress.canonical_host` | One canonical form for the CONNECT host and for every configured set, so the two sides of a membership test cannot disagree. The dotted form of an `egress.allow` host also stopped re-prompting, and a bare `.` is now refused as not a hostname rather than reaching the resolver. Four cases fail without the change |
| The pending-turn journal was 0o644 (measured), holding the verbatim text of every unanswered turn | `turns.TurnJournal._write` | Created through `os.open` at 0o600 with an explicit chmod, the same shape `codex_state.write_thread_id` uses for the same class of file. The chmod is what covers a permissive umask and a temp file an older build left behind; both cases fail without it. The fixed temp name is kept on purpose, reason in the comment |
| The replay cap and the TTL were evadable by a tampered journal: a negative `replays` needed ~10^18 restarts to reach the cap, a NaN `recorded_at` made every TTL comparison False, and `chat_id: true` passed the `int` check and became chat_id 1 | `turns.PendingTurn.from_dict`, `turns._as_float` | Clamped at the one boundary where a disk record becomes an object, so both gates in `take()` are sound without touching them. Bad entries are dropped, which is what `_read` already does with a `None`. A boolean chat id, a NaN and an Infinity `recorded_at`, and a `replays` of -10^18 all fail without the change |
| A thread could permanently defeat its own operator guardrails: it replaced a shared-entry link inside its writable `CODEX_HOME` with a directory, `unlink()` raised `IsADirectoryError`, and the suppressed `OSError` meant `hooks.json`, `AGENTS.md` and `config.toml` never came back for that thread | `codex_state._clear_link_site` | The link site is cleared through one helper that removes a planted directory, bounded to `HOMES_DIR` and never descending a symlink; every link failure now logs. A test plants the directory and asserts the operator's `hooks.json` is back on the next `ensure_home`, and fails without the change |
| Thread ids were spliced into glob patterns unescaped, so `*` matched every rollout in the tree: `adopt_rollout` copied another thread's into this workspace's `CODEX_HOME`, and `_find_codex_rollout` returned an unrelated conversation for the next prompt's handoff context | `codex_state.rollout_glob`, `codex_state._valid_thread_id` | Both sites now share one `glob.escape`d pattern, and the id charset is checked on the read side and the write side. 10 adversarial cases (`*`, `?`, `[a-c]*`, `../elsewhere`, whitespace) fail without the change. Charset derived from 14 real mappings on a deployed data dir, all 36 characters of lowercase hex and hyphen. Never reachable: the id comes only from codex's `--json` control event, so this is defence in depth |
| The NaN/Infinity clamp on `recorded_at` missed the huge-integer shape: a raw integer literal past ~1.8e308 is valid JSON, passes the `isinstance(int)` check, and `float()` raises `OverflowError` on it, which propagated through `take()` and crashed the daemon at startup | `turns._as_float` | Reproduced live with a 400-digit `recorded_at` (`CRASH: OverflowError int too large to convert to float`), then caught: `OverflowError` returns `None` and the entry is dropped the way a NaN is. `from_dict` is now total, so the journal read path cannot raise. `test_a_recorded_at_too_large_for_a_float_is_dropped` fails without the change |
| `sandbox.extra_paths` could re-open a *file*-level credential deny: the docstring claimed the home rule covered them, but an entry naming `~/.netrc` itself is neither the home nor an ancestor nor inside a listed store, so it was granted and re-opened exactly one denied file | `sandbox._CREDENTIAL_FILES`, `sandbox._extra_path_refusal` | 21 file-level paths mirrored 1:1 from the profile's literal denies, checked bidirectionally like the stores. `~/.netrc`, `~/.cargo`, `~/.config/hub` refused while `/opt/homebrew`, `/usr/local`, `/tmp` still granted. `test_every_file_level_credential_is_out_of_reach_of_extra_paths` walks the list and fails without the change |
| The Linux jail left `~/.codex/prompts` and `~/.codex/auth.json` writable: `_CODEX_PROTECTED` omitted them, so a turn could plant standing instructions codex reads on every run, or swap the operator's OAuth token | `sandbox._CODEX_PROTECTED` | `prompts` and `auth.json` added to the write-deny list, `prompts` to the dirs. `skills` deliberately not added: codex writes its own tree inside it (measured: 242 touches and a `skills/.system/` tree per turn), so a read-only mount there breaks codex. `test_linux_write_denies_cover_codex_prompts_and_auth` fails without the change |
| Under `deny-most` a relocated `CLAUDE_CONFIG_DIR` was invisible to a jailed agent: the claude read grant was written against `_HOME/.claude` while every write re-grant on the same tree uses `_CLAUDE_CONFIG`, so a config directory outside `$HOME` matched nothing and the CLI could not read its own settings | `seatbelt/fs-deny-most.sb` | Measured before: `Operation not permitted` on `<config>/settings.json`. Grant rewritten against `_CLAUDE_CONFIG`, placed with the other read allows so the `history.jsonl` and cross-thread `projects/` denies still come after it and still win -- both verified live under each base with the session boundary on |
| The symlink preflight warned on every start about links whose target another rule already denies, so the one case that matters was buried in noise | `sandbox._preflight_protected_symlinks` | It now probes each resolved target under the live jail and warns only about the reachable ones, naming what the link resolves to. The probe opens for append and writes nothing, so a reachable target is reported without being modified. Measured on a real home: both `~/.codex` links resolve into denied trees and the warning is gone. A probe that cannot run counts as reachable, because this decides whether to warn |
| A jailed claude turn could not authenticate at all: `jail.sb` denies the login keychain, which is where the CLI keeps its OAuth credential, so every jailed `claude-native` turn failed with `Not logged in` before making a network call | `sandbox._claude_oauth_token`, `sandbox.agent_env` | The daemon runs outside the jail, so it reads that one item and passes the one value as `ANTHROPIC_AUTH_TOKEN`; the keychain deny is untouched. Narrowing the deny instead was measured and rejected: seatbelt matches file paths and every secret lives in one database, so dropping it exposed an unrelated planted item too, and would expose the broker's own `cotf-anthropic` key. Live jailed turn: `PASS 5s body='PONG'`. Cost: the agent can read its own credential out of its environment, the posture `fs-allow-reads.sb` already takes for codex's `auth.json`. A broker route would close that too and is the stricter option if it is ever wanted |
| Every jailed `claude-pty` turn hung for its whole timeout: the startup lock is a `mkdir` under the deny-default config directory, and its stale-lock recovery reads a pid file *inside* the directory, so a denied `mkdir` is unrecoverable rather than slow | `seatbelt/*.sb` `_CLAUDE_CONFIG/.pty-lock`, `sandbox.agent_env` | Reproduced live under the jail (`mkdir: Operation not permitted`, then 150s of silent spin at 50ms a tick); `CLAUDE_PTY_NO_LOCK=1` got the same turn out to the network, which is what identified the lock as the blocker. `test_the_pty_startup_lock_can_be_taken_under_the_jail` fails without the grant under both bases. Capability, not a weakening: the directory holds one pid file read only by claude-pty, and the sibling test proves `settings.json` and `hooks/` stayed denied |
| A jailed turn could not reach cotf's tmux server, so `claude-pty` fell back to `script` and no jailed pty turn ever got a live pane. The profile's own comment said a unix-socket allow was all-or-nothing, because only `(remote unix)` works and it cannot be scoped | `seatbelt/jail.sb` `_PANE_SOCKET` | The comment was wrong. Measured on macOS 26: `sandbox-exec` accepts `(allow network-outbound (literal <socket>))`, and with it a jailed `tmux` lists cotf's real sessions; with that one line removed and nothing else changed it answers `error connecting ... (Operation not permitted)`. Being a literal, the Docker socket and ssh-agent stay denied, and a test asserts it is the only non-IP outbound allow. End to end: `claude-pty` under the jail went from a 150s timeout to `PASS 5s` |
| The profiles named `$HOME/.codex` literally in 25 rules, so a deployment that moved `CODEX_HOME` matched none of them: the denies protected a directory codex no longer used, and under `deny-most` codex could not read its own `config.toml` | `seatbelt/fs-deny-most.sb`, `seatbelt/fs-allow-reads.sb`, `sandbox._codex_operator_home` | All 25 rewritten against a new `_CODEX_OPERATOR_HOME` param, resolved through the operator's `CODEX_HOME`. Live under both bases with the home relocated outside `$HOME`: `config.toml` readable, `history.jsonl` still refused, which is what proves the grant is scoped rather than blanket |
| codex's `history.jsonl` -- every prompt the operator ever typed into codex on this host -- was readable by a jailed turn, while claude's identical file had been denied since the start | `seatbelt/*.sb` `_CODEX_OPERATOR_HOME/history.jsonl` | Denied in both profiles, written after the codex read grant so last-match-wins keeps it. Live probe against the real file refuses under both bases; a structural test pins the ordering. This is what made prompt-history protection symmetric across the two backends |
| Every jailed `claude-pty` turn still burned its whole timeout after the lock fix, parked on claude's first-run theme picker with nobody able to press a key | `sandbox.agent_env` | `agent_env` exported `CLAUDE_CONFIG_DIR` unconditionally, defaulting it to `~/.claude` under a comment claiming that is what the CLI would have done anyway. It is not: the default *directory* is `~/.claude`, but the default settings *file* is `~/.claude.json` at home root, and naming the directory moves it to `~/.claude/.claude.json` -- a different file, with no `hasCompletedOnboarding`. A `-p` turn does not care; a pty turn runs the real TUI and opens the wizard. The variable is now forwarded only when the daemon actually has one. Measured: `PASS 4s` with it absent, 150s timeout with it set to the default |
| `sandbox.fs: deny-most` refused to start *any* turn on macOS, every backend and mode, in 0s at the egress preflight. Two causes in one path: `sys.base_prefix` names a uv symlink (`cpython-3.12-...` -> `cpython-3.12.9-...`) and seatbelt matches the path the kernel resolves, so the grant covered nothing and dyld could not load `libpython`; and the five runtime slots truncated the list silently | `sandbox._runtime_read_paths`, `sandbox_macos._RUNTIME_SLOTS` | Every runtime path is now granted as written and as resolved, the ceiling is eight, and an overflow warns naming what it dropped. Measured before: `dyld: Library not loaded: @executable_path/../lib/libpython3.12.dylib ... (blocked by sandbox)`, SIGABRT before the interpreter ran a line. Reproduced on `origin/main` in a scratch worktree, so it predates this branch |
| A jailed probe inherited whatever directory the daemon was started from, which `deny-most` grants no more than any other home path | `sandbox._run_jailed` | Runs in the workspace it just granted. Measured before: the egress probe died on `getcwd()` before importing `socket`, because python resolves the empty `sys.path` entry against the cwd. The error named neither a path nor a rule -- `PermissionError: [Errno 1] Operation not permitted` out of `importlib` -- and every turn refused to start |
| The Linux jail aborted outright on any merged-usr distribution: `bwrap: Can't mount on symlink destination /bin`. Debian, Ubuntu and Fedora all ship `/bin` and `/lib` as symlinks, so this is the normal layout rather than an exotic one | `sandbox._linux_wrap` | bwrap mounts rather than matches, and refuses a symlink destination by aborting the whole jail rather than dropping one grant. The Linux plan now takes the resolved form only, which the entry above already puts in the list. Measured in a container on the real `_linux_grants` output. This had been hiding behind the parity suite: with the jail aborting, every probe failed, and a failed read is what the contract reads as `deny`. Five parity cases that expect `allow` failed on `origin/main` and pass here, and the dotenv case that expects `deny` passed there for no reason at all |
| Handing a jailed claude its keychain credential crashed every jailed turn on Linux. `read_keychain` shells out to `security`, which only macOS has, and `_claude_oauth_token` caught `KeyError` but not the `FileNotFoundError` a missing binary raises | `sandbox._claude_oauth_token` | Guarded on platform and on `OSError`. Introduced earlier on this same branch and caught before merge by running the Linux jail in a container -- reading the code had not found it in two passes |
| Jailed `claude-pty` could not run on Linux at all: the startup lock is a `mkdir` in the claude config directory, which the Linux jail mounts read-only | `sandbox.claude_pty_startup_is_delegated`, `backends/claude._hold_pty_startup_gate` | A writable mount over the lock is not a fix -- the `mkdir` then fails with `EEXIST` and the script's recovery only reclaims a lock whose pid file names a dead process, so it spins identically. An overlay is not a fix either: a private one per turn makes the lock succeed while serializing nothing, a shared one hands two threads a writable directory they both see. So the daemon takes the script's documented `CLAUDE_PTY_NO_LOCK=1` and holds the boot window itself. Measured in a container both ways: 0.1s with the fix, and `lock wait timeout after 8s (holder pid=unknown)` with the variable removed and nothing else changed |
| A runtime read grant reached the binary's own directory and nothing above it. Anything that canonicalizes its own path then died before running: node resolves its main module with `realpath`, which `lstat`s every ancestor, and the opaque `$HOME` refused `lstat '/Users/hoss/.local'` | `seatbelt/fs-deny-most.sb` | Metadata on the whole chain via seatbelt's own `(path-ancestors ...)` filter, for every `_RUNTIME_*` and `_EXTRA_*` slot. Metadata only -- the name and mode of each directory on the way down, never its listing and never its siblings. The `_ANCESTOR_*` literals predate the filter and still serve the project dir; one job, two mechanisms, worth collapsing later |
| The grant was the binary's parent directory, which misses a prefix-style install: the CLI sits in `<prefix>/bin` and its code in `<prefix>/lib/node_modules`, a sibling. codex started and then died on `Cannot read package config .../@openai/codex/package.json: operation not permitted` | `sandbox._install_library_dir` | Grant the `lib/` beside a `bin/` install, when there is one. `lib/` rather than the prefix on purpose: for `~/.local/bin/claude` the prefix is `~/.local`, one grant covering mise's installs, its state and every other tool kept there, to buy nothing -- claude's code is under `~/.local/share`. Asking for the directory that holds the code also makes it structurally impossible to hand back `$HOME` |
| pty mode granted `argv[0]` only. `claude-pty` is a shell script that execs `claude` for the turn and `tmux` to host it, neither of them granted, so under `deny-most` it died `rc 127` -- which reads as "command not found" rather than as a jail | `sandbox._EXECS_BEHIND` | A wrapper now contributes the binaries it execs. With this, the ancestors and the `lib/` grant, a jailed `claude-pty` turn under `deny-most` answered `PASS 4s`, having previously produced no envelope at all |
| Under `deny-most` codex could not start at all, on every mode: `Error: Operation not permitted (os error 1)`, naming no path. The read grant on the codex home covers the *links* inside it and not what they point at, because seatbelt matches the path the kernel resolves, so `~/.codex/agents -> ~/.agents/agents` was unreadable while the profile still read as granting it | `sandbox._codex_link_read_paths`, `seatbelt/fs-deny-most.sb` `_CODEX_LINK_*` | The kernel named it: `deny(1) file-read-data /Users/<user>/.agents/agents`, from `log stream` during a failing run. The Linux jail has mounted these targets read-only since the session boundary landed, so this is the macOS half of a grant that already existed rather than a new capability. Read only -- the write deny on the codex tree is still below it -- and a target that `sandbox.extra_paths` would refuse is refused here too, so a link at `$HOME` or into `~/.ssh` cannot reopen the home. Collapsed to shortest roots first, which took a real home from 54 targets to 3. Live: `codex-native PASS 7s`, `codex-pty PASS 8s` under `jail` + `deny-most` with no `extra_paths`, both `FAIL 1s` before |
| The codex link grant re-opened a dotenv inside the tree it named, the same way an operator grant would. Measured on a real home: granting `~/.agents/skills` for `~/.codex/skills` made `~/.agents/skills/<skill>/.env` -- a live API token -- readable to a jailed turn | `seatbelt/fs-deny-most.sb` `_CODEX_LINK_*`, `sandbox._linux_masked` | Introduced by the row above and caught before merge by probing what the grant exposed rather than what it was meant to expose. The same unanchored `\\.env` regex the `_EXTRA_*` slots get, one per slot, written after the grants so last-match-wins keeps them; Linux resolves the files and masks them, having no patterns. Probed live: the dotenv denied, `SKILL.md` beside it still readable, so the grant still works |
| A dotenv was readable anywhere the jail granted a tree but nobody had written a matching deny. Each grant carried its own scoped rule, so `~/.claude` and `~/.codex` -- which get a blanket subpath grant -- kept theirs readable, and under the read-permissive base so did every dotenv on the machine | `seatbelt/fs-deny-most.sb`, `seatbelt/fs-allow-reads.sb`, `sandbox._linux_masked` | One `(deny file-read* (regex "(.*/)?\\.env"))` at the end of each profile, replacing the fourteen scoped rules it subsumes; nothing re-allows a read after it, and a test pins that plus jail.sb adding no read allow after the import. The scoped form's stated reason for not doing this -- `.env.example` in the workspace -- was already false: a workspace is always `DATA_DIR/workspaces/<name>` and the data-dir rule already denied it, measured. Linux names each file instead, and the sweep cap now applies only to `sandbox.extra_paths`, where "narrow the entry" is advice an operator can act on; a config tree cotf must mount is masked whole (`~/.codex` holds 132, which is 17KB of argv). Probed live on both bases: every dotenv denied, `SKILL.md` beside one still readable. All eight jail cells still pass. Cost, measured and accepted: a file named `.env*` the agent writes in its own temp dir is no longer readable back |
| The broker path guard did not know a CLI's own path syntax, so an allowlisted tool could read any file the operator could. `_path_candidates` read a bare argument and the value after `=` on a flag, and looked inside neither `@/etc/passwd`, `file:///etc/passwd`, nor a `key=@path` value on a bare token. Measured: `gh api -F body=@/etc/passwd` arrived as the single token `body=@/etc/passwd` and read as a relative path inside the workspace, so the guard passed it and the broker ran it outside the sandbox with the real credential | `commands._path_candidates`, `commands._introduced_paths` | The two introducers every one of these CLIs shares, `@` and `file://`, stripped repeatedly so the nested forms unwrap, with each intermediate form kept as its own candidate, plus everything from the first slash of a `file://` URL that carries an authority -- RFC 8089 makes `file://localhost/etc/passwd` mean `/etc/passwd` and curl reads it, measured, so stripping only the scheme left a relative-looking `localhost/etc/passwd` that the guard passed. Emitting a candidate rather than refusing the argument is what makes this safe to over-apply: an extra form only matters when it is absolute or traversing, so a Slack handle or an email address is unaffected. A per-tool argument grammar is still deliberately absent, so a tool with a path syntax outside these two shapes remains the operator's check |
| A `file://` URL had two readings the guard did not take, both found by running a real exfiltration probe against the broker rather than by reading the code. RFC 3986 makes a scheme case-insensitive, so `FILE:///etc/passwd` walked past a lowercase-only match; and curl percent-decodes a URL path, so `file://<workspace>/%2e%2e/%2e%2e/etc/passwd` was lexically inside the workspace, passed the guard, and curl then read `/etc/passwd`. Both measured against real curl on a canary file. The second needs no `allow_paths` at all: the workspace is always an allowed root, so it escaped the default configuration | `commands._PATH_INTRODUCERS`, `commands._url_forms`, `commands._fully_decoded` | The introducer is matched case-insensitively, and a `file://` tail contributes its percent-decoded form as another candidate. Decoding runs to a fixed point, which is one step past curl: a decoded form no longer starts with `file://` so it would never decode again, and `%252e%252e` would stop short of `..`. That over-refuses a filename containing a literal `%25` and is taken anyway, matching the trade the rest of this guard makes. Verified by the probe described under "How these were found": 30 spellings, 0 leaks, and 13 ordinary arguments still run |
| A write ran through a read-only allowlist, with the operator's real GitHub credential. `requests_read_only` read `-X POST` and `--method=POST` but not the attached short form gh's parser also accepts, and did not know `--input` supplies a request body at all. Measured against gh 2.100.0 pointed at a local server that reports the method it received: `-XPOST`, `-fname=x`, `-Fname=x`, `--input <file>` and `--input=<file>` all send POST, and all five were admitted as reads. Live on the deployed host, which carries `gh api` under `allow_read_only` | `commands._flag_value`, `commands._attached_short_value`, `commands._PARAMETER_FLAGS` | A short flag's value is read whether it is separated, joined by `=`, or glued on, and `--input` joins the parameter flags. Only a real short flag (`-` plus one character) takes the glued reading, so `--methodological` is not read as `--method`. Verified: all ten measured write spellings refused, all seven measured read spellings still allowed |
| A readback flag was matched only as a bare token, so `--show-token=true` was not refused. Real gh accepts that spelling, checked against gh 2.100.0, which rejects an invented flag but takes this one. This is the one refusal the broker exists for -- it is what keeps the credential out of the sandbox | `commands.refuses_readback`, `commands._carries_flag` | Every flag is now matched in each spelling a parser accepts: bare, `=value`, and glued onto a short flag. `--show-token=false` is refused too, which is over-refusal in the safe direction |
| `egress.allow` silently disabled DNS-rebinding protection for every host on it. The constructor folded `allowed_hosts` into the private-address set, so an allowlisted name that resolved to a private or loopback address was tunnelled instead of refused. That is SSRF reachable through the ordinary, documented action of adding a host to the allowlist: the operator answers "yes, talk to this name" and gets "and the internal network behind it". Three sources say it was never intended -- `_permitted`'s own docstring ("`egress.allow` alone is never an SSRF exception"), the shipped `config.yaml` comment ("adding a name to the normal allowlist never disables DNS-rebinding/SSRF protection"), and the commit that introduced it, whose subject is "explicit private-host opt-in" | `egress.EgressProxy.__init__` | `allowed_hosts` is no longer folded into the private set, restoring the two-opt-in contract the documentation already promised. Measured against the real proxy with `localtest.me`, a public name that genuinely resolves to 127.0.0.1: before, on `allow` alone, the CONNECT was permitted and dialled loopback (502 from the upstream, not a 403 from the gate); after, it is refused "no usable public address", while `example.com` still tunnels 200 and an explicit `private_allow` still admits loopback. The three tests that broke were harness conveniences reaching a local echo server through `allow`; they now name the `private_allow` opt-in an operator would really need |
| An undeclared boolean flag hid a refused verb from the allowlist. The allowlist read a bare flag as taking the next token, so with `allow: [status]`, `systemctl --quiet stop status` matched `status`, and systemctl, which reads `--quiet` as boolean (measured on systemd 259), ran `stop`. `boolean_flags` closed it only for the flags an operator listed | `commands.allowed_command`, `commands.hidden_by_a_leading_flag` | A command now runs only when both flag readings admit it, the rule `refuses_readback` already applied. The cost is a value flag before the subcommand (`aws --profile prod logs tail`); the refusal names the fix, moving the flag after the subcommand, which aws, gh and systemctl accept. Moving it also exposes a hidden verb, so the hint is safe for an attack. A replay of 1896 real brokered calls from the deployed host's transcripts through the old and new guard with its live allowlist: none newly refused. Real run through the broker on that host: the leading-flag form refused with the hint, the reordered `stop` refused as unlisted, `is-active` ran |
| The path guard could not see a path inside a JSON argument: `--params '{"body":"/etc/passwd"}'` was one relative-looking token. No brokered tool is known to open a file named this way, and gws 0.22.5 was measured not to (`--params @/x` fails as invalid JSON; its file flags are plain values the guard already saw) | `commands._json_strings`, `commands._path_candidates` | Every string literal in a token starting with `{` or `[` is a candidate, escapes decoded, found without parsing so deep nesting cannot make it give up. The value after the first `=` is also a candidate, since a JSON value can hold `=`. None of 675 real JSON arguments on the deployed host held a value it refuses. Real run through the broker against real gws: a plain and an escaped nested `/etc/passwd` refused, an ordinary `--params` listed Drive |
| `~/.claude/.credentials.json`, refresh token included, was readable under the Linux jail, which re-exposes `~/.claude` read-only and masked only `history.jsonl`. macOS keeps the credential in the denied keychain, and had no deny for the file where it exists off the keychain: the live jail test read it before the rule was added | `sandbox._claude_credential_text`, `sandbox._CLAUDE_READ_DENIED`, `seatbelt/fs-*.sb` | Under `jail` on Linux the daemon reads the access token outside the jail and passes it as `ANTHROPIC_AUTH_TOKEN`, as macOS does from the keychain, and the file is masked; both macOS profiles deny it by name. Under `env` nothing changes, because the CLI can still refresh its own file. Real bubblewrap run on the deployed host with a placeholder credential: the file hidden, `history.jsonl` hidden, `settings.json` readable, the env holding the access token and not the refresh token, and `claude auth status` reporting `loggedIn: true, authMethod: oauth_token`. codex's `auth.json` stays readable with its refresh token, as recorded in the security model |

## Open

Ordered by severity against the threat model above.

### Credential reach

**`fs: allow-reads` leaves credential stores readable.** Measured on a real home: the
Firefox profile tree (holding `logins.json` and `key4.db`) and `~/Library/Messages/chat.db`.
`~/.ssh`, `~/.aws` and `~/Library/Keychains` are correctly denied. This is the known cost
of enumerate-the-bad, which the profile itself admits. `deny-most` does not have it, and
is the posture to deploy.

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

**A jailed turn's pane runs on a server the operator can attach to.** The
`_PANE_SOCKET` allow above is what makes a jailed pty turn visible, and visibility is
the point of the pane -- but it is also a channel. The socket is cotf's own tmux
server, so an operator who attaches sees the turn, and a turn that can talk to the
server can also address *other* sessions on it: `tmux send-keys -t <other>` reaches
another thread's pane. Accepted for now, because every session on that server belongs
to this daemon and a turn that wanted to influence another thread has cheaper routes
through the shared workspace. The narrower shape is one server per thread, which costs
a tmux process per concurrent turn and loses the single `tmux attach` an operator uses
today. Not measured.

**The daemon's claude-pty gate on Linux is narrower than the script's.** The lock it
replaces is a directory in the claude config dir, so it excludes every claude-pty on
the host. The daemon's gate is an in-process lock, so it excludes only the turns this
daemon starts. A claude the operator runs in their own terminal at the same moment can
still collide with a jailed turn during claude's one-second supervisor boot, and the
symptom is a hung TUI rather than an error. Accepted: it is strictly better than the
turn never running, which is what Linux did before.

**`sandbox.fs: deny-most` completes a real turn on both backends, and needs operator
tuning for pty hooks.** `codex-native`, `codex-pty` and `claude-native` all pass with no
`extra_paths` at all. One thing remains.

The pty turn needs `extra_paths` for wherever the operator's claude hooks live. On the
machine this was measured on they sit under `~/.rhapsody/cache/pty/hooks`, and without
the grant claude answers correctly and then the Stop hook that writes the envelope
fails: `bash: .../stop_envelope.sh: Operation not permitted`. cotf cannot know that
path, so this is the tuning surface working as designed rather than a defect -- but it
fails in a way that names a hook rather than a sandbox, which is worth knowing.

An earlier version of this entry said codex could not run when reached through a `mise`
shim, and blamed a tracking symlink mise writes under `~/.local/state/mise`. That
diagnosis was wrong and is removed. The `mise WARN tracking config: failed to ln -sf`
line is real and non-fatal; running the cask binary directly, with no shim anywhere,
failed identically. The actual cause was the symlinked `~/.codex` entry recorded in the
Fixed table above, and the kernel log named it once it was asked.

Two denials seen alongside it are benign and deliberately not granted:
`file-read-data $HOME/.CFUserTextEncoding`, which every CoreFoundation process attempts,
and `file-read-metadata $HOME/.git`, from codex walking up from the workspace looking
for a repository. Granting `~/.agents` alone cleared the failure with both still denied.

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

**The TUI live view builds its path from the raw workspace name.** The display is
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

### Linux jail reach

Both found on maoao while proving the jobs daemon's brokers under `jail`. Neither is
specific to jobs; a chat turn hits them the same way.

**The jail breaks uv's minor-version interpreter link.** A uv venv's `bin/python` points
at `~/.local/share/uv/python/cpython-3.X-<platform>`, which is itself a symlink to the
patch-level directory. The grant binds `sys.prefix` and the resolved `sys.base_prefix`,
not the link between them, and `$HOME` is a tmpfs, so `execvp` of the venv's python fails
inside the namespace. That fails the preflight (the daemon refuses to start) and every
shim, whose shebang is that interpreter. The installed tool on maoao has this chain, so
switching it to `jail` today stops every daemon at startup.

**`claude.mode: ollama` cannot reach the ollama server from a Linux jail.** The relay
bridges a published `*_BASE_URL`, the egress proxy, and the brokers. Ollama mode
publishes no base URL, so port 11434 is never bridged and the jailed `ollama launch`
answers "could not connect to ollama server".

## Considered while adding tmux panes

Not a review's findings. What the pane work (`tmux.py`, `backends/codex.py`) changed
about the boundary, so a later reviewer does not have to re-derive it.

**The secret would have gone in argv, and does not.** Hosting a run in a tmux pane means
the run is a child of a tmux server rather than of the daemon, so the obvious way to give
it the curated environment is `tmux new-session -e KEY=VALUE`. Measured on tmux 3.7c: a
pane on a server that is *already running* does not receive the client's environment at
all, so `-e` is the only way that works on a shared server. It would put `COTF_CMD_TOKEN`
— the bearer token for the broker that runs credentialed CLIs *outside* the jail — into a
command line readable by any local `ps`, and with sandboxing off `agent_env()` is the
whole daemon environment. Rejected. Each run gets a private server addressed by
`TMUX_TMPDIR` instead: a server the daemon starts inherits the daemon's spawn env, and
its panes inherit that, with nothing on any command line. Measured both directions.

**Dropping the environment instead would be worse, not neutral.** A pane on a
pre-existing server does not get an empty environment, it gets the operator's login
shell. That inverts the curation, and it silently disarms the gates: no
`COTF_APPROVE_URL` means the shim fails closed while codex skips an untrusted hook and
runs the command anyway, and no proxy variables means no egress gating.

**The jail moved inside the pane command.** `sandbox.wrap` is applied to the argv that
becomes the pane's command string (`_run_codex_in_pane`), not to a process the daemon
spawns. A quoting mistake there runs codex unjailed, which is why the pane command is
built with `shlex.join`.

**The prompt is in that command line, and `ps` can read it.** The interactive UI needs
its stdin to be the terminal, so the prompt cannot arrive on stdin from a file the way
the earlier `codex exec` arm passed it; it is an argv element instead. The unhosted arm
has always done the same. What that exposes to any local user is the system prompt, the
handoff, and the message text — conversation content, not credentials, which the broker
keeps out of the agent's environment entirely. Worth knowing before putting anything in
a prompt that a local user should not see.

**Process-group kill does not reach a hosted run.** `agent._kill_process_tree` kills the
daemon's own process group; a pane is a child of the tmux server. `tmux.kill` runs
`kill-server`, which ends the session, its panes and everything started inside them, and
both producers call it in their `finally`. `tmux.sweep` covers what a SIGKILLed daemon
left.

**A read command cannot start a server with the wrong environment.** Measured:
`capture-pane` and `resize-window` against a missing socket fail with "error connecting"
rather than starting a server. This matters because the TUI polls for panes that may not
exist yet.

## Accepted, not fixing

**One conversation can read another's transcripts** when `scope_sessions` is off. This is
the design position, recorded in the security model. Protection at that level is an
instruction in `_JAIL_GUIDANCE`, not a boundary.

**The threads of one conversation can read each other's transcripts and files** even with
`scope_sessions` on, since a workspace is the conversation's directory and the grant follows
it (`protocol.Frontend.workspace_name`). Accepted: a Slack channel's threads share their
audience, and a DM's threads share their one person. The conversation memory at
`<workspace>/memory/` is shared on purpose for the same reason.

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
- A full job agent run under the jail. The jobs brokers were proven on maoao by driving
  them directly, because the ollama gap above stops the model from starting.
- Two concurrent jailed turns, so the per-turn `_SESSION_ENV` ContextVar is untested under
  real concurrency.
- Codex under the macOS jail, and claude under the Linux jail. Both share the policy layer;
  only the platform mechanism differs.
- Six TUI modules read by diff only, not line by line: `state.py`, `screens/config_picker.py`,
  `screens/doctor.py`, `screens/history.py`, `env_editor.py`, `supervisor.py`.

## How these were found

Reading the code found none of them -- with one exception, the egress row above,
which a *contradiction* found: the constructor and the docstring three lines away
could not both be true, and a real CONNECT settled which. Each of the rest came
from a real run: a probe that
starts the actual `CommandBroker`, lets it write its real shims, and invokes
those shims the way a sandboxed agent does -- over loopback HTTP, with a real
per-workspace token, spawning a real subprocess. The brokered binary is a CLI
written to be exactly as permissive as curl and no more, so a spelling that
prints the canary is a spelling curl would have read too.

Two habits did the work and are worth repeating on the next change here:

1. **Ask what the real CLI does, never what it ought to do.** Every claim above
   was settled by running `curl` or `gh` and watching what came back -- the
   method on the wire, the file on stdout. `gh` was pointed at a local server,
   so nothing reached GitHub and nothing was mutated.
2. **Probe the negative case as well.** The probe carries a set of ordinary
   arguments that must keep working. A guard that refuses `@alice` or
   `report.md` is broken, not secure, and only that half catches it.

One property is worth stating because it surprises people rather than because it is
wrong: a `commands.allow_paths` root grants writing as well as reading. The guard asks
where a path lands, not what the tool does with it, and it has no per-tool flag table
that could separate an output flag from an input one. Measured: with `/tmp/shared`
granted, `-o /tmp/shared/new.txt` is admitted. Documented in
`docs/how-to/broker-a-command.md` rather than changed, because the alternative is the
per-tool argument grammar this broker deliberately does not have.

Three properties were tested and held, so they need no fix:

- **argv alone cannot point `gh` at a host the agent chooses.** `gh api
  http://127.0.0.1:<port>/steal` reaches the server with no `Authorization`
  header, and `--hostname` rejects an `address:port`. The credential is scoped to
  hosts gh already knows.
- **A per-turn token is bound to its workspace.** Driven directly against the
  endpoint: the issuing workspace runs, while another workspace, the parent
  directory and `/` are each refused with "only runs inside this session's
  workspace", and a forged token gets 403.
- **A broker route's upstream host cannot be moved by the path.** The tail after
  the prefix is `lstrip("/")`-ed and appended, and no spelling relocates the
  host: `//evil.com/x`, `@evil.com/x`, `../../x`, `..%2f..%2fx` and
  `\\evil.com/x` all still resolve to the route's own upstream. `_match` also
  requires an exact prefix or a `/` boundary, so `/anthropicEVIL` does not match
  `/anthropic`.
- **The agent cannot choose the subprocess environment.** `_subprocess_env`
  copies from the daemon's own environment, so an `env_passthrough` name such as
  `GH_HOST` carries the operator's value and nothing the agent set.
