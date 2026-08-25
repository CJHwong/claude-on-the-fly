"""Structured preflight checks.

Each checker takes an env mapping and returns a list of CheckResult. Pure
functions — no I/O for env checks, optional `shutil.which` for binary checks.

Used by:
- preflight.py: raises SystemExit on any non-ok result (backward-compatible behavior)
- tui/screens/doctor.py: renders results with fix hints
- tui/env_editor.py: maps changed env vars back to affected daemons via FRONTEND_ENV_VARS
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from claude_on_the_fly import envfile
from claude_on_the_fly.interim import interim_progress_reads_as_on

Status = Literal["ok", "missing", "invalid", "warn"]


def fix_hint(name: str) -> str:
    """Where to set `name`, by its environment-variable spelling.

    Derived from `settings.FIELDS` rather than written out per call site, so a
    setting that moves to the config file cannot leave a hint pointing at `.env`
    behind. That was not hypothetical: three checkers sent an operator to `.env` for
    a value the file had taken over, which is the worst kind of stale doc -- a
    diagnostic that is confidently wrong at the moment someone needs it.

    Resolved against `agent.DATA_DIR` per call, so a daemon on a redirected data
    dir (COTF_DATA_DIR) is told about its own files, not the default location's.
    """
    from claude_on_the_fly import settings
    from claude_on_the_fly.agent import DATA_DIR

    for path, field in settings.FIELDS.items():
        if field.env == name:
            return f"set `{path}` in {DATA_DIR / settings.FILENAME}"
    return f"set in {DATA_DIR / '.env'}"


def display_name(name: str) -> str:
    """User-facing spelling for a check's internal setting name.

    Checkers deliberately still consume and identify settings through the legacy
    environment-variable key space: it is the compatibility seam shared by the
    runtime, preflight, and tests.  Doctor output is configuration documentation,
    though, so showing ``OLLAMA_MODEL`` there after it moved to YAML sends the
    operator to the wrong file.  Translate migrated settings at the presentation
    boundary and leave real environment-only settings (tokens, LOG_LEVEL, etc.)
    unchanged.
    """
    from claude_on_the_fly import settings

    for path, field in settings.FIELDS.items():
        if field.env == name:
            return path
    return name


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_hint: str | None = None


# ---------------------------------------------------------------------------
# Env var declarations — used by env_editor to map changes to daemons.
# ---------------------------------------------------------------------------

# Read by orchestrator.run, so they belong to whichever chat frontend is
# hosting it. Listed against both frontends below rather than in a group of
# their own, because env_editor maps a changed key to the daemons that must
# restart, and both of them must.
SANDBOX_ENV_VARS: tuple[str, ...] = (
    "COTF_SANDBOX",
    "COTF_SANDBOX_FS",
    "COTF_SANDBOX_EXTRA_PATHS",
    "COTF_SANDBOX_BROKER_ONLY_LOOPBACK",
)

TELEGRAM_ENV_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "TELEGRAM_STATS_MODE",
    *SANDBOX_ENV_VARS,
)
SLACK_ENV_VARS: tuple[str, ...] = (
    "SLACK_APP_TOKEN",
    "SLACK_TOKEN",
    "SLACK_ALLOWED_SENDER_IDS",
    "SLACK_BLOCKED_SENDER_IDS",
    "SLACK_SILENT_SENDER_IDS",
    "SLACK_SLASH_COMMAND",
    # Declared here and not in JOBS_ENV_VARS: the slack daemon is the process
    # that binds it (slack.JOB_COMMAND), so it is the one env_editor must
    # restart when it changes.
    "SLACK_JOB_COMMAND",
    "SLACK_STATS_MODE",
    *SANDBOX_ENV_VARS,
    # Deprecated aliases (still honored; see SLACK_LEGACY).
    "SLACK_USER_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_ALLOWED_USER_IDS",
    "SLACK_BLOCKED_USER_IDS",
    "SLACK_ALLOWED_BOT_IDS",
)
# The worker shares SLACK_TOKEN with the slack frontend (editing it affects both);
# JOBS_SLACK_TOKEN optionally overrides it. JOBS_TIMEOUT is the per-job wall-clock
# limit in seconds (0 or negative = no limit). JOBS_STALE_TTL_S is deferred with
# multi-worker support.
JOBS_ENV_VARS: tuple[str, ...] = (
    "JOBS_QUEUE_KIND",
    "JOBS_POLL_INTERVAL_S",
    "JOBS_TIMEOUT",
    "JOBS_SLACK_TOKEN",
    "SLACK_TOKEN",
)
BACKEND_ENV_VARS: tuple[str, ...] = (
    "AGENT_BACKEND",
    "CLAUDE_MODE",
    "CODEX_MODE",
    "OLLAMA_MODEL",
    "CLAUDE_MODEL",
    "CODEX_MODEL",
)

FRONTEND_ENV_VARS: dict[str, tuple[str, ...]] = {
    "telegram": TELEGRAM_ENV_VARS,
    "slack": SLACK_ENV_VARS,
    "cron": (),  # no per-frontend env vars; uses cron.yaml
    "jobs": JOBS_ENV_VARS,
}

# The chat frontends, in display order. This tuple is the single source of that
# order: the dashboard's chat rows, the history filter cycle, and the doctor all
# read it, so reordering here reorders every surface at once. The first entry is
# also the dashboard's default selection before a tab is activated.
CHAT_FRONTENDS: tuple[str, ...] = (
    "slack",
    "telegram",
)

# Every daemon the TUI can start/stop, chat frontends first.
SUPERVISABLE_FRONTENDS: tuple[str, ...] = (
    *CHAT_FRONTENDS,
    "cron",
    "jobs",
)


def _config_files() -> dict[str, Path]:
    """Per-frontend required config files. Resolved lazily so DATA_DIR can be
    monkeypatched in tests."""
    from claude_on_the_fly.cron import resolve_config_path

    # Resolved, not hardcoded: an install that has not been migrated off
    # `schedule.yaml` yet is configured, and doctor must not call it missing.
    return {"cron": resolve_config_path()}


def _config_validators() -> dict[str, Callable[[Path], object]]:
    """Per-frontend "does this config actually load?" callables.

    Imported lazily and per-frontend so a `doctor` run does not pull yaml,
    croniter and liquid into every caller just to check that a file exists.
    """
    from claude_on_the_fly.cron import load_config as load_cron

    return {"cron": load_cron}


def check_config_file(frontend: str) -> list[CheckResult]:
    """Verify any config file the frontend needs at startup is on disk and loads.

    Existence alone was the old check, and it passes on a config the daemon then
    refuses to start with — a bad cron expression, a prompt template that does not
    compile, a `prompt_file` that has been moved. Doctor is where somebody looks
    *before* starting a daemon, so it is where that has to surface.
    """
    path = _config_files().get(frontend)
    if path is None:
        return []
    if path.is_file():
        validate = _config_validators().get(frontend)
        if validate is not None:
            try:
                validate(path)
            except ValueError as exc:
                return [
                    CheckResult(
                        name=path.name,
                        status="invalid",
                        detail=str(exc),
                        fix_hint=f"fix {path} — the daemon will refuse to start",
                    )
                ]
        return [CheckResult(name=path.name, status="ok", detail=str(path))]
    return [
        CheckResult(
            name=path.name,
            status="missing",
            detail=f"required config not found at {path}",
            fix_hint="See README for examples",
        )
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(env: Mapping[str, str], name: str, hint: str | None = None) -> CheckResult:
    value = env.get(name)
    if value is None or value == "":
        return CheckResult(
            name=name,
            status="missing",
            detail="not set",
            fix_hint=hint or fix_hint(name),
        )
    return CheckResult(name=name, status="ok", detail="set")


# ---------------------------------------------------------------------------
# Per-frontend env checks
# ---------------------------------------------------------------------------


def check_telegram(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = [_require(env, "TELEGRAM_BOT_TOKEN")]

    uid = env.get("TELEGRAM_ALLOWED_USER_ID", "")
    if not uid:
        results.append(
            CheckResult(
                name="TELEGRAM_ALLOWED_USER_ID",
                status="missing",
                detail="not set",
                fix_hint=fix_hint("TELEGRAM_ALLOWED_USER_ID"),
            )
        )
    else:
        try:
            int(uid)
            results.append(
                CheckResult(
                    name="TELEGRAM_ALLOWED_USER_ID", status="ok", detail=f"= {uid}"
                )
            )
        except ValueError:
            results.append(
                CheckResult(
                    name="TELEGRAM_ALLOWED_USER_ID",
                    status="invalid",
                    detail=f"must be an integer, got {uid!r}",
                    fix_hint="Use the numeric Telegram user ID, not the @handle",
                )
            )
    return results


def check_slack(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []

    app_check = _require(env, "SLACK_APP_TOKEN")
    if app_check.status == "ok":
        token = env["SLACK_APP_TOKEN"]
        if not token.startswith("xapp-"):
            app_check = CheckResult(
                name="SLACK_APP_TOKEN",
                status="invalid",
                detail="must start with 'xapp-'",
                fix_hint="Use the App-Level Token from Slack admin, not the user token",
            )
    results.append(app_check)

    results.append(_check_slack_bearer(env))

    command = env.get("SLACK_SLASH_COMMAND", "")
    if command:
        results.append(_check_slack_command(command))

    job_command = effective_job_command(env)
    if job_command:
        results.append(_check_slack_job_command(job_command))
        # Only when the trigger is live: that is exactly when this frontend
        # builds a queue (and could die on a bad kind), and when it starts
        # making promises a missing worker cannot keep.
        results.append(_check_queue_kind(env))
        results.append(_check_jobs_worker_reachable(env))

    return results


def _check_slack_command(command: str) -> CheckResult:
    """Validate SLACK_SLASH_COMMAND if set. It is optional, but a malformed one
    fails silently at runtime: slack_bolt registers the handler and Slack simply
    never routes anything to it."""
    from claude_on_the_fly.slack_manifest import command_error

    problem = command_error(command)
    if problem is None:
        return CheckResult(name="SLACK_SLASH_COMMAND", status="ok", detail=command)
    return CheckResult(
        name="SLACK_SLASH_COMMAND",
        status="invalid",
        detail=f"{command} {problem}",
        fix_hint="Unset it to drop the slash command, or run "
        "`claude-slack --manifest` to pick one and get a matching manifest",
    )


def _job_command_error(value: str) -> tuple[Status, str] | None:
    """None when `value` is a usable job trigger, else (status, why).

    The status separates "this cannot work" from "this works, but probably not
    as you meant": only the first is a reason to refuse to start the daemon.

    Three kinds of mistake, worth keeping apart: Slack never delivers it
    ('/', '<>&'); one of our own prefixes eats it ($stop, $continue); or it
    fires, but not as its author meant (whitespace, a leading word character).
    Only that last group is a trap rather than a dead trigger. Deliberately not
    a full charset check, for the same reason as `slack_manifest.command_error`:
    Slack is the authority on what a message may contain.
    """
    if any(char.isspace() for char in value):
        # Fires, so don't read this as "can never match": *internal* whitespace
        # matches — "$my job" on "$my job <task>" — but only on exactly the
        # spacing its author typed, so a doubled one misses with no error. Only
        # the *leading* form can never match, and a *trailing* one kills the
        # bare-trigger usage form: `_ingest_event` strips before comparing.
        return "invalid", "cannot contain whitespace"
    if value.startswith("/"):
        return (
            "invalid",
            "cannot start with '/' — Slack routes that as a slash command",
        )
    if any(char in value for char in "<>&"):
        # `_ingest_event` reads `event["text"]` raw, with nothing unescaping it.
        return (
            "invalid",
            "cannot contain '<', '>' or '&' — Slack escapes them in message text",
        )
    # The turn-control prefixes as literals: importing them from
    # `claude_on_the_fly.slack` would pull slack_bolt into every checks import.
    # test_checks.py `test_turn_control_prefixes_match_the_slack_constants`
    # fails on drift, so the duplication is checked rather than merely noted.
    if value == "$stop":
        # Matched by exact equality and wins first, so the trigger is not merely
        # dead: a bare "$stop" still aborts the turn while "$stop do the thing"
        # falls past it and queues a job — one prefix, two behaviours.
        return "invalid", "collides with the $stop turn-control prefix"
    if value == "$compact":
        # Same shape as $stop: exact-match, intercepted before the job branch, so
        # a bare "$compact" compacts while "$compact <task>" queues a job.
        return "invalid", "collides with the $compact turn-control prefix"
    if value == "$continue":
        # Intercepted *after* the job branch, so a trigger equal to it shadows
        # the reply soft-limit reset entirely — a gated thread could never be
        # un-gated.
        return "invalid", "collides with the $continue turn-control prefix"
    if value[:1].isalnum():
        # A footgun, not a silent failure: it fires exactly as written, and the
        # trigger is matched against the head of every inbound message, so an
        # ordinary word swallows any message that opens with it.
        return (
            "warn",
            "should start with punctuation, e.g. `$job` — a plain word "
            "swallows every message beginning with it",
        )
    return None


def _check_slack_job_command(command: str) -> CheckResult:
    """Validate SLACK_JOB_COMMAND if set. It is optional, but a malformed one
    costs nothing at startup and everything at runtime — see
    `_job_command_error` for the three ways it goes wrong."""
    problem = _job_command_error(command)
    if problem is None:
        return CheckResult(name="SLACK_JOB_COMMAND", status="ok", detail=command)
    status, detail = problem
    return CheckResult(
        name="SLACK_JOB_COMMAND",
        status=status,
        detail=f"{command} {detail}",
        fix_hint="Unset it to drop the background-job trigger, or pick a "
        "punctuation-led value like `$job`",
    )


# Preferred env var -> deprecated fallbacks it replaces. Legacy names still work
# but are undocumented; run_slack warns when one is used. SLACK_ALLOWED_SENDER_IDS
# supersedes both the old user and bot allowlists (ids route by prefix).
SLACK_LEGACY: dict[str, tuple[str, ...]] = {
    "SLACK_TOKEN": ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"),
    "SLACK_ALLOWED_SENDER_IDS": ("SLACK_ALLOWED_USER_IDS", "SLACK_ALLOWED_BOT_IDS"),
    "SLACK_BLOCKED_SENDER_IDS": ("SLACK_BLOCKED_USER_IDS",),
}


def _parse_ids(raw: str) -> set[str]:
    return {piece.strip() for piece in raw.split(",") if piece.strip()}


def resolve_slack_token(env: Mapping[str, str]) -> tuple[str | None, str]:
    """Return (var_name, token) for the first set Slack bearer var, or (None, "").
    The token kind (user vs bot) is inferred from its prefix, so one SLACK_TOKEN
    field covers both; SLACK_USER_TOKEN / SLACK_BOT_TOKEN still work."""
    for name in ("SLACK_TOKEN", *SLACK_LEGACY["SLACK_TOKEN"]):
        value = env.get(name, "")
        if value:
            return name, value
    return None, ""


def resolve_jobs_token(env: Mapping[str, str]) -> tuple[str | None, str]:
    """Return (var_name, token) for the claude-jobs notifier's Slack bearer.

    `JOBS_SLACK_TOKEN` overrides the shared `SLACK_TOKEN` so the worker can post
    under a different identity than the acking frontend (deployer-controlled).
    Falls back to `resolve_slack_token` (honoring the legacy aliases)."""
    override = env.get("JOBS_SLACK_TOKEN", "")
    if override:
        return "JOBS_SLACK_TOKEN", override
    return resolve_slack_token(env)


def resolve_slack_ids(env: Mapping[str, str], preferred: str) -> set[str]:
    """Resolve a comma-separated id set. The preferred var wins; otherwise its
    legacy fallbacks are merged (so the old split user/bot allowlists combine)."""
    raw = env.get(preferred, "")
    if raw:
        return _parse_ids(raw)
    ids: set[str] = set()
    for legacy in SLACK_LEGACY.get(preferred, ()):
        ids |= _parse_ids(env.get(legacy, ""))
    return ids


def slack_deprecations(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(legacy_var, preferred_var) for every deprecated Slack var in use — i.e.
    set while its preferred replacement is not."""
    out: list[tuple[str, str]] = []
    for preferred, legacy_names in SLACK_LEGACY.items():
        if env.get(preferred, ""):
            continue
        out.extend(
            (legacy, preferred) for legacy in legacy_names if env.get(legacy, "")
        )
    return out


def _check_slack_bearer(env: Mapping[str, str]) -> CheckResult:
    """A single bearer token, either kind: `xoxp-` (replies as you) or `xoxb-`
    (replies as the app). Set it as SLACK_TOKEN."""
    name, token = resolve_slack_token(env)
    if not token:
        return CheckResult(
            name="SLACK_TOKEN",
            status="missing",
            detail="set SLACK_TOKEN to an xoxp- (user) or xoxb- (bot) token",
            fix_hint=fix_hint("SLACK_TOKEN"),
        )
    if not token.startswith(("xoxp-", "xoxb-")):
        return CheckResult(
            name=name or "SLACK_TOKEN",
            status="invalid",
            detail="must start with 'xoxp-' (user) or 'xoxb-' (bot)",
            fix_hint="Use the User or Bot OAuth Token from Slack admin",
        )
    kind = "bot" if token.startswith("xoxb-") else "user"
    return CheckResult(name=name or "SLACK_TOKEN", status="ok", detail=f"set ({kind})")


# Mirrors `slack.DEFAULT_JOB_COMMAND`. Duplicated as a literal for the same
# reason the turn-control prefixes are: importing `claude_on_the_fly.slack`
# would pull slack_bolt into every checks import. Drift fails in
# test_checks.py::test_job_command_default_matches_the_slack_constant.
DEFAULT_JOB_COMMAND = "$job"


def effective_job_command(env: Mapping[str, str]) -> str | None:
    """The trigger this env actually produces, or None when jobs are off.

    Absent means the default; present-but-blank is the opt-out. Same rule as
    `slack._resolve_job_command`, applied to an arbitrary mapping so preflight
    can reason about a daemon's environment before it starts.
    """
    return env.get("SLACK_JOB_COMMAND", DEFAULT_JOB_COMMAND) or None


def check_jobs(env: Mapping[str, str]) -> list[CheckResult]:
    """The worker's notifier needs a resolvable Slack bearer token, the queue
    kind has to be one that exists, and something has to be able to enqueue."""
    return [
        _check_jobs_token(env),
        _check_queue_kind(env),
        _slack_job_producer_note(env),
        _check_alert_targets(env),
    ]


def _check_alert_targets(env: Mapping[str, str]) -> CheckResult:
    """Warn when an alert target is set but its token is missing.

    The worker refuses to start without a Slack token, so its alerts are
    covered; the cron producer is the one that would silently skip alerts.
    Advisory: an install that never configured alerts is legitimate.
    """
    channel = env.get("SLACK_ALERT_TARGET", "").strip()
    chat = env.get("TELEGRAM_ALERT_TARGET", "").strip()
    if not channel and not chat:
        return CheckResult(name="alert targets", status="ok", detail="none configured")
    missing = []
    if channel and not resolve_jobs_token(env)[1]:
        missing.append("SLACK_ALERT_TARGET needs JOBS_SLACK_TOKEN or SLACK_TOKEN")
    if chat and not env.get("TELEGRAM_BOT_TOKEN", "").strip():
        missing.append("TELEGRAM_ALERT_TARGET needs TELEGRAM_BOT_TOKEN")
    if not missing:
        return CheckResult(name="alert targets", status="ok", detail="configured")
    return CheckResult(
        name="alert targets",
        status="warn",
        detail="; ".join(missing),
    )


def _check_queue_kind(env: Mapping[str, str]) -> CheckResult:
    """Validate JOBS_QUEUE_KIND against the registry.

    Blocking, and deliberately also part of `check_slack`: `make_queue()` raises
    on an unknown kind, and the Slack frontend calls it while constructing the
    producer. Without this the typo surfaces as a daemon that dies at startup
    with a traceback — and it takes down *Slack*, not just jobs. Caught here it
    is one legible line naming the kinds that exist.
    """
    from claude_on_the_fly.jobs.registry import SUPPORTED_QUEUES

    kind = env.get("JOBS_QUEUE_KIND", "").strip().lower()
    if not kind:
        return CheckResult(name="JOBS_QUEUE_KIND", status="ok", detail="file (default)")
    if kind in SUPPORTED_QUEUES:
        return CheckResult(name="JOBS_QUEUE_KIND", status="ok", detail=kind)
    return CheckResult(
        name="JOBS_QUEUE_KIND",
        status="invalid",
        detail=f"{kind!r} is not a registered queue kind",
        fix_hint=f"Use one of: {', '.join(sorted(SUPPORTED_QUEUES))}, or unset it",
    )


def _check_jobs_worker_reachable(env: Mapping[str, str]) -> CheckResult:
    """Warn when Slack's job trigger is on but no worker is draining the queue.

    `$job` acks "I'll reply here when it's done" the moment it enqueues. With no
    worker that promise is never kept and nothing in the thread says so, which
    is the worst shape a failure can take — it looks like it worked.

    Advisory, not blocking: the worker may legitimately be started after the
    frontend, and refusing to run Slack because a *different* daemon is down
    would be the collateral damage this whole feature is gated to avoid.
    """
    from claude_on_the_fly.heartbeat import live_pid

    pid = live_pid("jobs")
    if pid is not None:
        return CheckResult(
            name="jobs worker", status="ok", detail=f"running (pid {pid})"
        )
    return CheckResult(
        name="jobs worker",
        status="warn",
        detail=(
            f"not running — {env.get('SLACK_JOB_COMMAND', '')} will ack jobs "
            "that nothing drains"
        ),
        fix_hint="Start it: `claude-tui start jobs`",
    )


def _check_jobs_token(env: Mapping[str, str]) -> CheckResult:
    """Honors JOBS_SLACK_TOKEN (worker-specific override) first, else the shared
    SLACK_TOKEN via `_check_slack_bearer`. Same token-kind rules as slack — no
    hardcoded identity, no mandated kind."""
    override = env.get("JOBS_SLACK_TOKEN", "")
    if not override:
        return _check_slack_bearer(env)
    if not override.startswith(("xoxp-", "xoxb-")):
        return CheckResult(
            name="JOBS_SLACK_TOKEN",
            status="invalid",
            detail="must start with 'xoxp-' (user) or 'xoxb-' (bot)",
            fix_hint="Use the User or Bot OAuth Token from Slack admin",
        )
    kind = "bot" if override.startswith("xoxb-") else "user"
    return CheckResult(name="JOBS_SLACK_TOKEN", status="ok", detail=f"set ({kind})")


def _slack_job_producer_note(env: Mapping[str, str]) -> CheckResult:
    """Report whether Slack can enqueue into this worker at all.

    `claude-jobs doctor` runs `check_jobs`, never `check_slack`, so without this
    line a worker whose SLACK_JOB_COMMAND is unset reports "all checks passed"
    while nothing in Slack can ever reach it. A malformed value reports its
    problem here too, because the doctor view renders the `slack` and `jobs`
    groups on one screen (`tui/screens/doctor.py`) and a bare "producer on"
    would contradict `check_slack`'s `invalid` row of the same name.

    Explicitly disabled is `warn`, not a failure: an `enqueue`-only install is
    legitimate — cron, a git hook, another tool shelling out — so the worker
    must still start. But a worker no chat frontend can reach is a silent
    no-op, and the operator deserves to be told which of the two they have.
    """
    raw = env.get("SLACK_JOB_COMMAND")
    command = effective_job_command(env)
    if command is None:
        return CheckResult(
            name="SLACK_JOB_COMMAND",
            status="warn",
            detail="disabled — only `claude-jobs enqueue` can reach this worker",
            fix_hint="Unset SLACK_JOB_COMMAND to restore the default trigger "
            f"`{DEFAULT_JOB_COMMAND}`",
        )
    if raw is None:
        return CheckResult(
            name="SLACK_JOB_COMMAND",
            status="ok",
            detail=f"Slack producer on ({command}, default)",
        )
    problem = _job_command_error(command)
    if problem is None:
        return CheckResult(
            name="SLACK_JOB_COMMAND",
            status="ok",
            detail=f"Slack producer on ({command})",
        )
    # Report the reason and mirror the severity check_slack assigns it, so the
    # two rows of this name on the doctor screen cannot disagree.
    status, reason = problem
    return CheckResult(
        name="SLACK_JOB_COMMAND",
        status=status,
        detail=f"Slack producer misconfigured: {command} {reason}",
        fix_hint="Unset it to drop the background-job trigger, or pick a "
        "punctuation-led value like `$job`",
    )


# ---------------------------------------------------------------------------
# Backend (CLI selection) env checks
# ---------------------------------------------------------------------------

_VALID_BACKENDS = ("claude", "codex")
_VALID_MODES = ("native", "ollama")
# claude-pty is claude-only; codex has no equivalent wrapper.
_VALID_CLAUDE_MODES = ("native", "ollama", "pty")


def check_backend(env: Mapping[str, str]) -> list[CheckResult]:
    backend = env.get("AGENT_BACKEND", "claude").lower()
    results: list[CheckResult] = []

    if backend not in _VALID_BACKENDS:
        results.append(
            CheckResult(
                name="AGENT_BACKEND",
                status="invalid",
                detail=f"{backend!r} (supported: {', '.join(_VALID_BACKENDS)})",
                fix_hint=f"Set AGENT_BACKEND to one of: {', '.join(_VALID_BACKENDS)}",
            )
        )
        return results

    results.append(
        CheckResult(name="AGENT_BACKEND", status="ok", detail=f"= {backend}")
    )

    mode_var = f"{backend.upper()}_MODE"
    valid_modes = _VALID_CLAUDE_MODES if backend == "claude" else _VALID_MODES
    mode = env.get(mode_var, "native").lower()
    if mode not in valid_modes:
        results.append(
            CheckResult(
                name=mode_var,
                status="invalid",
                detail=f"{mode!r} (supported: {', '.join(valid_modes)})",
                fix_hint=f"Set {mode_var}={' or '.join(valid_modes)}",
            )
        )
        return results
    results.append(CheckResult(name=mode_var, status="ok", detail=f"= {mode}"))

    if mode == "ollama":
        model = env.get("OLLAMA_MODEL", "").strip()
        if not model:
            results.append(
                CheckResult(
                    name="OLLAMA_MODEL",
                    status="missing",
                    detail=f"required when {mode_var}=ollama",
                    fix_hint=fix_hint("OLLAMA_MODEL"),
                )
            )
        else:
            results.append(
                CheckResult(name="OLLAMA_MODEL", status="ok", detail=f"= {model}")
            )

    auto_compact = _check_auto_compact(env, backend, mode)
    if auto_compact is not None:
        results.append(auto_compact)

    interim = _check_interim_progress(env, backend, mode)
    if interim is not None:
        results.append(interim)

    return results


def check_backend_runtime_access(env: Mapping[str, str]) -> list[CheckResult]:
    """Probe host access the selected backend needs before it is spawned.

    Permission bits are not sufficient when the supervisor itself is running in
    a sandbox. A real create/write/unlink cycle verifies that the replacement
    daemon will be able to initialize Codex state before a restart stops the
    healthy process.
    """
    if env.get("AGENT_BACKEND", "claude").lower() != "codex":
        return []

    configured = env.get("CODEX_HOME", "").strip()
    home = (
        Path(configured).expanduser()
        if configured
        else Path(env.get("HOME") or Path.home()) / ".codex"
    )
    probe = home / f".cotf-write-probe-{os.getpid()}-{uuid4().hex}"
    fd: int | None = None
    try:
        home.mkdir(parents=True, exist_ok=True)
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"ok\n")
        os.close(fd)
        fd = None
        probe.unlink()
    except OSError as exc:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            probe.unlink()
        return [
            CheckResult(
                name="Codex state write access",
                status="invalid",
                detail=f"cannot write {home}: {exc}",
                fix_hint=(
                    "Run the start or restart command from a terminal outside "
                    "a sandboxed agent session. Changing mode bits may not help "
                    "when the parent process is sandboxed."
                ),
            )
        ]

    return [
        CheckResult(
            name="Codex state write access",
            status="ok",
            detail=f"write probe passed at {home}",
        )
    ]


# Backend/mode pairs that can both compact and supply the prompt-size reading
# the auto-compact gate thresholds on. The gap is claude's ollama mode, which
# withholds the window on purpose (see `ClaudeBackend.run`) because the claude
# CLI reports one for whichever model it thinks is answering rather than the one
# ollama routed to — so the gate there has nothing trustworthy to compare
# against however the threshold is set. codex reports both in its rollout.
_AUTO_COMPACT_CAPABLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("claude", "native"),
        ("claude", "pty"),
        ("codex", "native"),
        ("codex", "ollama"),
    }
)


def _check_auto_compact(
    env: Mapping[str, str], backend: str, mode: str
) -> CheckResult | None:
    """Report a threshold that is set but can never fire. None when unset.

    Advisory: a setting that does nothing is not a reason to refuse to start,
    but it is a reason to say so — silence here reads as a working setting, and
    the whole point of the knob is to spend money in the background.
    """
    raw = env.get("COTF_AUTO_COMPACT_PCT", "").strip()
    if not raw:
        return None
    if (backend, mode) in _AUTO_COMPACT_CAPABLE:
        return CheckResult(
            name="COTF_AUTO_COMPACT_PCT", status="ok", detail=f"= {raw}%"
        )
    return CheckResult(
        name="COTF_AUTO_COMPACT_PCT",
        status="warn",
        detail=(
            f"set to {raw}% but inert under {backend}/{mode} — the claude CLI "
            "reports a context window for the wrong model under ollama, so the "
            "reading is withheld"
        ),
        fix_hint=(
            "Use CLAUDE_MODE=native or pty for automatic compaction; manual "
            "compaction still works in every mode"
        ),
    )


# Backend/mode pairs whose output we read line by line as it is produced. Not the
# same set as _AUTO_COMPACT_CAPABLE and not derivable from it: claude's ollama
# mode streams exactly as native does (the launcher only prepends an argv
# prefix), while pty returns a single envelope from claude-pty and codex buffers
# all of stdout before parsing it. Auto-compact's gap is ollama, for an unrelated
# reason — the window figure describes the wrong model there.
_INTERIM_CAPABLE: frozenset[tuple[str, str]] = frozenset(
    {("claude", "native"), ("claude", "ollama")}
)


def _check_interim_progress(
    env: Mapping[str, str], backend: str, mode: str
) -> CheckResult | None:
    """Report interim progress switched on where it can never fire. None when off.

    Advisory, like the auto-compact threshold: a setting that does nothing is not
    a reason to refuse to start, but silence reads as a working setting.

    On/off is decided by `interim.interim_progress_reads_as_on` rather than by a
    second copy of the truthy set here: the runtime reads the same setting, and a
    doctor that disagreed with it would report "off" for a value the daemon acts
    on. The predicate takes the raw string, so this stays a pure function over
    the mapping it was handed.
    """
    if not interim_progress_reads_as_on(env.get("COTF_INTERIM_PROGRESS", "")):
        return None
    if (backend, mode) in _INTERIM_CAPABLE:
        return CheckResult(name="COTF_INTERIM_PROGRESS", status="ok", detail="= on")
    return CheckResult(
        name="COTF_INTERIM_PROGRESS",
        status="warn",
        detail=(
            f"on but inert under {backend}/{mode} — that mode hands back one "
            "envelope at the end of the turn, so there is no stream to follow"
        ),
        fix_hint="Use agent.claude.mode native or ollama to see progress while a turn runs",
    )


# ---------------------------------------------------------------------------
# Binary checks (optional shutil.which probes)
# ---------------------------------------------------------------------------


def _which(binary: str, install_hint: str) -> CheckResult:
    if shutil.which(binary):
        return CheckResult(name=binary, status="ok", detail="installed")
    return CheckResult(
        name=binary,
        status="missing",
        detail="not on PATH",
        fix_hint=install_hint,
    )


def check_binaries(env: Mapping[str, str]) -> list[CheckResult]:
    backend = env.get("AGENT_BACKEND", "claude").lower()
    results: list[CheckResult] = []

    if backend == "claude":
        results.append(
            _which("claude", "Install: https://docs.anthropic.com/en/docs/claude-code")
        )
    elif backend == "codex":
        results.append(_which("codex", "Install: https://github.com/openai/codex"))

    mode = env.get(f"{backend.upper()}_MODE", "native").lower()
    if mode == "ollama":
        results.append(_which("ollama", "Install: https://ollama.com"))
    if mode == "pty" and backend == "claude":
        results.extend(check_pty_setup(env))

    return results


def check_pty_setup(env: Mapping[str, str] | None = None) -> list[CheckResult]:
    """Validate claude-pty install + hook wiring for CLAUDE_MODE=pty.

    Three sub-checks: the pty binary resolves, jq is on PATH (pty shells out
    to it), and ~/.claude/settings.json has pty's Stop hook + statusline shim
    wired in (without them, pty hangs forever waiting on an envelope file).
    """
    from claude_on_the_fly.backends.claude import (
        PTY_INSTALL_HINT,
        PTY_PROJECT_SLUG,
        resolve_pty_binary,
    )

    results: list[CheckResult] = []

    pty_path = resolve_pty_binary()
    if pty_path:
        results.append(CheckResult(name="claude-pty", status="ok", detail=pty_path))
    else:
        results.append(
            CheckResult(
                name="claude-pty",
                status="missing",
                detail=f"not on PATH or ~/.local/share/{PTY_PROJECT_SLUG}/bin",
                fix_hint=PTY_INSTALL_HINT,
            )
        )

    results.append(_which("jq", "Install: brew install jq (pty shells out to jq)"))

    results.append(check_pty_hooks(env))
    results.append(check_pty_hook_paths(env))
    results.append(check_pty_tmux_for_approvals())
    return results


def check_pty_tmux_for_approvals() -> CheckResult:
    """Approvals under pty require claude-pty's tmux backend, not its script one.

    pty is gated by reading claude's own permission dialog off the terminal and
    typing the answer back, which needs a tmux pane the daemon can address.
    claude-pty picks tmux only when tmux is on PATH and CLAUDE_PTY_NO_TMUX is not
    "1"; otherwise it falls back to `script`, where there is no pane to read.

    A missing pane does not fail loudly on its own. The dialog simply goes
    unanswered and the turn stalls until its timeout, which looks like the agent
    hanging rather than like a misconfiguration -- so it is worth catching at
    startup, where the fix is one line.

    Reports ok when approvals are off, since the script backend is perfectly fine
    then.
    """
    from claude_on_the_fly import permissions

    if not permissions.configured().enabled:
        return CheckResult(
            name="pty tmux backend", status="ok", detail="not needed (approvals off)"
        )
    if os.environ.get(PTY_NO_TMUX_ENV, "0") == "1":
        return CheckResult(
            name="pty tmux backend",
            status="missing",
            detail=f"{PTY_NO_TMUX_ENV}=1 forces the script backend, which has no "
            "pane to read a permission dialog from",
            fix_hint=f'unset {PTY_NO_TMUX_ENV}, or set permissions.mode to "off"',
        )
    if shutil.which("tmux") is None:
        return CheckResult(
            name="pty tmux backend",
            status="missing",
            detail="tmux is not on PATH, so claude-pty falls back to its script "
            "backend and no permission dialog can be answered",
            fix_hint="brew install tmux",
        )
    return CheckResult(name="pty tmux backend", status="ok", detail="tmux available")


# Environment variables the two shims are built around: the statusline shim
# writes the sidecar, the Stop hook writes the envelope whose appearance is
# pty's "turn done" signal. A script that names one is that shim; a script that
# does not cannot satisfy the contract whatever it is called or wherever it
# lives.
# Set to "1" this forces claude-pty onto its script backend, which approvals
# cannot use: there is no addressable pane to read a dialog from.
PTY_NO_TMUX_ENV = "CLAUDE_PTY_NO_TMUX"

PTY_SIDECAR_MARKER = "CLAUDE_PTY_SIDECAR"
PTY_ENVELOPE_MARKER = "CLAUDE_PTY_ENVELOPE"
# The PostCompact writer must gate on the compaction's trigger; see
# `_has_pty_postcompact_hook` for why that, and not the envelope marker, is what
# separates it from the Stop shim.
PTY_TRIGGER_MARKER = ".trigger"
# Bytes read from a wired script when identifying it. The markers sit in the
# first few lines of both shims; this only has to be generous, not exact.
PTY_SHIM_PROBE_BYTES = 64 * 1024


def _is_pty_shim(command: str, marker: str) -> bool:
    """Whether `command` refers to a script implementing pty's side of `marker`.

    Read the script and look for the environment variable it must act on,
    rather than matching the install path. The path told us which *install*
    wired the hook, not whether a working one is wired — so a shim installed
    anywhere else (a sibling tool vendoring the same project, a user's own
    prefix) read as "missing" and took down every daemon that runs under
    CLAUDE_MODE=pty, over a functioning setup.

    Falls back to the install-path match when the script cannot be read, so an
    unreadable-but-correctly-named shim is no worse off than before.
    """
    from claude_on_the_fly.backends.claude import PTY_PROJECT_SLUG

    command = command.strip()
    if not command:
        return False
    # The wired value can carry arguments; the script is the first field.
    script = command.split()[0]
    try:
        with open(script, encoding="utf-8", errors="replace") as handle:
            return marker in handle.read(PTY_SHIM_PROBE_BYTES)
    except OSError:
        return PTY_PROJECT_SLUG in command


def check_pty_hooks(env: Mapping[str, str] | None = None) -> CheckResult:
    """Verify ~/.claude/settings.json wires pty's Stop hook + statusline shim.

    The two hooks are what make pty work — without them claude-pty hangs
    forever. install.sh writes them; this check catches a stale config.

    Identified by what the wired scripts do, not where they were installed —
    see `_is_pty_shim`. Several tools vendor the same shims into their own
    prefixes and rewrite `statusLine.command` to their copy, so a path match
    fails whenever the last writer was not the install this repo expects.
    """
    from claude_on_the_fly.backends.claude import (
        PTY_INSTALL_HINT,
    )

    settings_path = envfile.claude_config_dir(env) / "settings.json"
    if not settings_path.is_file():
        return CheckResult(
            name="claude-pty hooks",
            status="missing",
            detail=f"no settings.json at {settings_path}",
            fix_hint=PTY_INSTALL_HINT,
        )

    import json as _json

    try:
        config = _json.loads(settings_path.read_text())
    except (_json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="claude-pty hooks",
            status="invalid",
            detail=f"cannot parse {settings_path}: {exc}",
            fix_hint="Fix the JSON or restore from the backup install.sh left",
        )

    statusline_cmd = ""
    statusline_node = config.get("statusLine")
    if isinstance(statusline_node, dict):
        statusline_cmd = str(statusline_node.get("command", ""))
    has_statusline = _is_pty_shim(statusline_cmd, PTY_SIDECAR_MARKER)

    has_stop_hook = False
    for entry in config.get("hooks", {}).get("Stop", []) or []:
        for hook in entry.get("hooks", []) or []:
            if _is_pty_shim(str(hook.get("command", "")), PTY_ENVELOPE_MARKER):
                has_stop_hook = True
                break
        if has_stop_hook:
            break

    missing = []
    if not has_statusline:
        missing.append("statusLine shim")
    if not has_stop_hook:
        missing.append("Stop hook")
    if missing:
        return CheckResult(
            name="claude-pty hooks",
            status="missing",
            detail=f"settings.json missing: {', '.join(missing)}",
            fix_hint=PTY_INSTALL_HINT,
        )

    # The PostCompact hook is what lets a compaction finish under pty, and only
    # that. Ordinary turns are unaffected, so this warns rather than blocks: an
    # install predating the hook should keep running and just not compact.
    # Checked by what the script does, like the others — but the envelope marker
    # alone would also match the Stop shim, and the Stop shim in this slot is
    # worse than nothing (it writes an envelope for the mid-turn `trigger:
    # "auto"` compactions too, ending the turn early and returning the summary
    # in place of the answer). Requiring the trigger gate as well tells the two
    # apart, since stop_envelope.sh never reads `.trigger`.
    if not _has_pty_postcompact_hook(config):
        return CheckResult(
            name="claude-pty hooks",
            status="warn",
            detail=(
                "Stop hook + statusline shim wired, but no PostCompact hook — "
                "$compact and auto-compaction would hang under CLAUDE_MODE=pty"
            ),
            fix_hint=f"Update claude-pty: {PTY_INSTALL_HINT}",
        )

    return CheckResult(
        name="claude-pty hooks",
        status="ok",
        detail="Stop + PostCompact hooks + statusline shim wired",
    )


def check_pty_hook_paths(env: Mapping[str, str] | None = None) -> CheckResult:
    """Report wired pty hooks whose script is gone.

    install.sh only ever dedups its *own* path — deliberately, since several
    tools vendor these shims and an entry this install did not write is not its
    to delete. The cost is that a tool removed from disk leaves its hook wired
    forever, and claude then tries to run a missing script on every turn.
    Reported rather than repaired, for the same reason install.sh leaves it: the
    entry belongs to whoever wrote it.
    """
    settings_path = envfile.claude_config_dir(env) / "settings.json"
    import json as _json

    try:
        config = _json.loads(settings_path.read_text())
    except (_json.JSONDecodeError, OSError):
        # check_pty_hooks already reports an unreadable/absent settings.json;
        # a second row saying so would be noise.
        return CheckResult(name="claude-pty hook paths", status="ok", detail="—")

    wired: list[str] = []
    for slot in ("Stop", "PostCompact"):
        for entry in config.get("hooks", {}).get(slot, []) or []:
            for hook in entry.get("hooks", []) or []:
                command = str(hook.get("command", "")).strip()
                if command:
                    wired.append(command)

    orphans = [c for c in wired if not Path(c.split()[0]).is_file()]
    duplicates = {c for c in wired if wired.count(c) > 1}
    problems = []
    if orphans:
        problems.append(f"{len(orphans)} orphaned ({', '.join(orphans)})")
    if duplicates:
        problems.append(
            f"{len(duplicates)} duplicated ({', '.join(sorted(duplicates))})"
        )
    if not problems:
        return CheckResult(
            name="claude-pty hook paths",
            status="ok",
            detail=f"{len(wired)} wired, all present",
        )
    return CheckResult(
        name="claude-pty hook paths",
        status="warn",
        detail="; ".join(problems),
        fix_hint=(
            "Remove the dead entries from settings.json, or run that tool's "
            "uninstall.sh — re-running install.sh only tidies its own path"
        ),
    )


def pty_postcompact_hook_wired(env: Mapping[str, str] | None = None) -> bool:
    """Whether this machine's settings.json can finish a pty compaction.

    Called on the compaction path itself, not only by the doctor: without the
    hook a pty compaction never returns an envelope, and the frontends pass no
    timeout, so the turn would wait forever and block every message queued
    behind it. Cheaper to read one JSON file than to hang a thread.

    Fails closed on an unreadable config — refusing with a message beats an
    unbounded wait.
    """
    settings_path = envfile.claude_config_dir(env) / "settings.json"
    import json as _json

    try:
        config = _json.loads(settings_path.read_text())
    except (_json.JSONDecodeError, OSError):
        return False
    return _has_pty_postcompact_hook(config)


def _has_pty_postcompact_hook(config: dict) -> bool:
    """Whether settings.json wires a working pty PostCompact envelope writer."""
    for entry in config.get("hooks", {}).get("PostCompact", []) or []:
        for hook in entry.get("hooks", []) or []:
            command = str(hook.get("command", ""))
            if _is_pty_shim(command, PTY_ENVELOPE_MARKER) and _is_pty_shim(
                command, PTY_TRIGGER_MARKER
            ):
                return True
    return False


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------


_ENV_CHECKERS: dict[str, Callable[[Mapping[str, str]], list[CheckResult]]] = {
    "telegram": check_telegram,
    "slack": check_slack,
    "jobs": check_jobs,
}


def check_frontend(frontend: str, env: Mapping[str, str]) -> list[CheckResult]:
    """Per-frontend checks: env vars + required config files.

    Deliberately no check for the tools a cron entry's `command` shells out to:
    which ones those are is the entry author's business, and the package cannot
    know whether a given install needs acli, gh, curl, or nothing at all.
    """
    if frontend not in SUPERVISABLE_FRONTENDS:
        raise ValueError(f"unknown frontend: {frontend!r}")
    env_checker = _ENV_CHECKERS.get(frontend)
    env_checks = env_checker(env) if env_checker else []
    return env_checks + check_config_file(frontend)


def check_all(env: Mapping[str, str] | None = None) -> dict[str, list[CheckResult]]:
    """Run every check group. Used by the doctor view."""
    e = os.environ if env is None else env
    return {
        **{name: check_frontend(name, e) for name in SUPERVISABLE_FRONTENDS},
        "backend": check_backend(e),
        "binaries": check_binaries(e),
    }


# Statuses that do not stop a daemon from starting. `warn` reports a setting
# that works exactly as written but is likely not what its author meant — advice
# worth surfacing in `doctor`, never a reason to refuse to run. `missing` and
# `invalid` stay blocking: they describe a daemon that cannot do its job.
NON_BLOCKING_STATUSES: frozenset[Status] = frozenset({"ok", "warn"})


def is_blocking(result: CheckResult) -> bool:
    """Whether this result should stop a daemon from starting.

    The question every caller counting "problems" actually means. Advisory
    `warn` results are worth printing and worth a yellow cell, but exiting 1 or
    refusing a spawn over one turns advice into an outage.
    """
    return result.status not in NON_BLOCKING_STATUSES


def first_failure(results: list[CheckResult]) -> CheckResult | None:
    """Return the first blocking result, or None if none blocks startup."""
    for r in results:
        if is_blocking(r):
            return r
    return None


def all_ok(results: list[CheckResult]) -> bool:
    """True when nothing blocks startup. Advisory `warn` results do not."""
    return all(r.status in NON_BLOCKING_STATUSES for r in results)
