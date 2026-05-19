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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["ok", "missing", "invalid", "warn"]

DOTENV_HINT = "set in ~/.claude-on-the-fly/.env"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_hint: str | None = None


# ---------------------------------------------------------------------------
# Env var declarations — used by env_editor to map changes to daemons.
# ---------------------------------------------------------------------------

TELEGRAM_ENV_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "TELEGRAM_STATS_MODE",
)
SLACK_ENV_VARS: tuple[str, ...] = (
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "SLACK_ALLOWED_USER_IDS",
    "SLACK_STATS_MODE",
)
GMAIL_ENV_VARS: tuple[str, ...] = (
    "GMAIL_GCP_PROJECT",
    "GMAIL_ALLOWED_SENDERS",
    "GMAIL_POLL_INTERVAL",
    "GMAIL_STATS_MODE",
)
BACKEND_ENV_VARS: tuple[str, ...] = (
    "AGENT_BACKEND",
    "CLAUDE_MODE",
    "CODEX_MODE",
    "OLLAMA_MODEL",
    "CLAUDE_MODEL",
)

FRONTEND_ENV_VARS: dict[str, tuple[str, ...]] = {
    "telegram": TELEGRAM_ENV_VARS,
    "slack": SLACK_ENV_VARS,
    "gmail": GMAIL_ENV_VARS,
    "schedule": (),  # no per-frontend env vars; uses schedule.yaml
    "symphony": (),  # uses symphony.yaml
}

SUPERVISABLE_FRONTENDS: tuple[str, ...] = (
    "telegram",
    "slack",
    "gmail",
    "schedule",
    "symphony",
)


def _config_files() -> dict[str, Path]:
    """Per-frontend required config files. Resolved lazily so DATA_DIR can be
    monkeypatched in tests."""
    from claude_on_the_fly.agent import DATA_DIR

    return {
        "schedule": DATA_DIR / "schedule.yaml",
        "symphony": DATA_DIR / "symphony.yaml",
    }


def check_config_file(frontend: str) -> list[CheckResult]:
    """Verify any config file the frontend needs at startup is on disk."""
    path = _config_files().get(frontend)
    if path is None:
        return []
    if path.is_file():
        return [CheckResult(name=path.name, status="ok", detail=str(path))]
    hint = (
        "See README for examples"
        if frontend == "schedule"
        else "See symphony.yaml.example"
    )
    return [
        CheckResult(
            name=path.name,
            status="missing",
            detail=f"required config not found at {path}",
            fix_hint=hint,
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
            fix_hint=hint or DOTENV_HINT,
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
                fix_hint=DOTENV_HINT,
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

    user_check = _require(env, "SLACK_USER_TOKEN")
    if user_check.status == "ok":
        token = env["SLACK_USER_TOKEN"]
        if not token.startswith("xoxp-"):
            user_check = CheckResult(
                name="SLACK_USER_TOKEN",
                status="invalid",
                detail="must start with 'xoxp-'",
                fix_hint="Use the User OAuth Token from Slack admin",
            )
    results.append(user_check)

    return results


def check_gmail(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = [_require(env, "GMAIL_GCP_PROJECT")]

    senders_raw = env.get("GMAIL_ALLOWED_SENDERS", "")
    senders = {s.strip() for s in senders_raw.split(",") if s.strip()}
    if not senders:
        results.append(
            CheckResult(
                name="GMAIL_ALLOWED_SENDERS",
                status="missing",
                detail="must contain at least one email address, pattern, or '*'",
                fix_hint=DOTENV_HINT,
            )
        )
    else:
        results.append(
            CheckResult(
                name="GMAIL_ALLOWED_SENDERS",
                status="ok",
                detail=f"{len(senders)} sender(s)",
            )
        )
    return results


# ---------------------------------------------------------------------------
# Backend (CLI selection) env checks
# ---------------------------------------------------------------------------

_VALID_BACKENDS = ("claude", "codex")
_VALID_MODES = ("native", "ollama")


def check_backend(env: Mapping[str, str]) -> list[CheckResult]:
    backend = env.get("AGENT_BACKEND", "claude").lower()
    results: list[CheckResult] = []

    if backend not in _VALID_BACKENDS:
        results.append(
            CheckResult(
                name="AGENT_BACKEND",
                status="invalid",
                detail=f"{backend!r} (supported: {', '.join(_VALID_BACKENDS)})",
                fix_hint="Set AGENT_BACKEND=claude or codex",
            )
        )
        return results

    results.append(
        CheckResult(name="AGENT_BACKEND", status="ok", detail=f"= {backend}")
    )

    mode_var = f"{backend.upper()}_MODE"
    mode = env.get(mode_var, "native").lower()
    if mode not in _VALID_MODES:
        results.append(
            CheckResult(
                name=mode_var,
                status="invalid",
                detail=f"{mode!r} (supported: {', '.join(_VALID_MODES)})",
                fix_hint=f"Set {mode_var}=native or ollama",
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
                    fix_hint=DOTENV_HINT,
                )
            )
        else:
            results.append(
                CheckResult(name="OLLAMA_MODEL", status="ok", detail=f"= {model}")
            )

    return results


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

    return results


def check_gws_binary() -> CheckResult:
    return _which(
        "gws",
        "Install: npm install -g @googleworkspace/cli",
    )


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------


_ENV_CHECKERS: dict[str, Callable[[Mapping[str, str]], list[CheckResult]]] = {
    "telegram": check_telegram,
    "slack": check_slack,
    "gmail": check_gmail,
}


def check_frontend(frontend: str, env: Mapping[str, str]) -> list[CheckResult]:
    """Per-frontend checks: env vars + required config files (if any)."""
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


def first_failure(results: list[CheckResult]) -> CheckResult | None:
    """Return the first non-ok result, or None if all passed."""
    for r in results:
        if r.status != "ok":
            return r
    return None


def all_ok(results: list[CheckResult]) -> bool:
    return all(r.status == "ok" for r in results)
