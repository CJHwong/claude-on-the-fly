"""Preflight checks - validate tokens and tools before starting.

Env validation is delegated to claude_on_the_fly.checks (structured CheckResult
model, also consumed by the TUI doctor view). Live checks (httpx token probes,
subprocess CLI probes) live here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import httpx

from claude_on_the_fly import checks
from claude_on_the_fly.checks import CheckResult

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Read a required env var or exit with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _raise_on_failures(results: list[CheckResult]) -> None:
    """Raise SystemExit on the first non-ok result, preserving its detail."""
    fail = checks.first_failure(results)
    if fail is None:
        return
    msg = f"{fail.name}: {fail.detail}"
    if fail.fix_hint:
        msg += f" — {fail.fix_hint}"
    raise SystemExit(msg)


def _extract_cli_result(stdout: str) -> str:
    """Pull the `result` text from `claude -p --output-format json` stdout.

    Falls back to scanning NDJSON lines if the output isn't a single JSON blob.
    """
    text = stdout.strip()
    if not text:
        return ""
    try:
        msg = json.loads(text)
        if isinstance(msg, dict) and msg.get("result"):
            return str(msg["result"])
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("result"):
            return str(msg["result"])
    return ""


_AGENT_INSTALL_HINTS = {
    "claude": "https://docs.anthropic.com/en/docs/claude-code",
    "codex": "https://github.com/openai/codex",
}


def check_backend() -> None:
    """Validate the configured agent backend before startup.

    Dispatches by `AGENT_BACKEND` (default `claude`) and `<AGENT>_MODE`
    (default `native`). Native mode runs the agent-specific live check;
    ollama mode skips it and validates the ollama wrap instead.
    """
    backend_name = os.environ.get("AGENT_BACKEND", "claude").lower()
    if backend_name == "claude":
        mode = os.environ.get("CLAUDE_MODE", "native").lower()
        if mode == "native":
            check_claude_cli()
            return
        if mode == "ollama":
            check_ollama_mode("claude")
            return
        if mode == "snap":
            check_snap_mode()
            return
        raise SystemExit(
            f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, snap)"
        )
    if backend_name == "codex":
        mode = os.environ.get("CODEX_MODE", "native").lower()
        if mode == "native":
            check_codex_cli()
            return
        if mode == "ollama":
            check_ollama_mode("codex")
            return
        raise SystemExit(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")
    raise SystemExit(
        f"Unknown AGENT_BACKEND: {backend_name!r} (supported: claude, codex)"
    )


def check_codex_cli() -> None:
    """Verify codex CLI is installed. Auth is checked at first use."""
    if not shutil.which("codex"):
        raise SystemExit(
            f"codex CLI not found. Install it: {_AGENT_INSTALL_HINTS['codex']}"
        )
    logger.info("codex CLI: ok")


def check_ollama_mode(agent_name: str = "claude") -> None:
    """Validate `ollama launch <agent>` mode: both CLIs present, model available."""
    if not shutil.which(agent_name):
        raise SystemExit(
            f"{agent_name} CLI not found. Install it: {_AGENT_INSTALL_HINTS[agent_name]}"
        )
    if not shutil.which("ollama"):
        raise SystemExit("ollama CLI not found. Install it: https://ollama.com")
    model = os.environ.get("OLLAMA_MODEL", "").strip()
    if not model:
        env_var = f"{agent_name.upper()}_MODE"
        raise SystemExit(f"{env_var}=ollama requires OLLAMA_MODEL to be set")
    result = subprocess.run(
        ["ollama", "list"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise SystemExit(f"ollama list failed: {detail}")
    # First column of each non-header line is the model name.
    available = {
        line.split()[0] for line in result.stdout.splitlines()[1:] if line.strip()
    }
    if model not in available:
        raise SystemExit(
            f"OLLAMA_MODEL={model!r} not found in `ollama list`. "
            f"Pull it first: ollama pull {model}"
        )
    logger.info("ollama launch mode: ok (agent=%s model=%s)", agent_name, model)


def check_snap_mode() -> None:
    """Validate `CLAUDE_MODE=snap`: binary resolves, jq present, hooks wired.

    Reuses the structured `checks.check_snap_setup` so doctor view and
    preflight see the same failures. If the binary is missing on an
    interactive TTY, offer to install it via the canonical curl script.
    """
    from claude_on_the_fly import checks as _checks
    from claude_on_the_fly import snap_install

    if not snap_install.is_snap_installed():
        outcome = snap_install.ensure_snap_installed()
        if not outcome.installed:
            raise SystemExit(outcome.message)
        logger.info("claude-snap: %s", outcome.message)

    results = _checks.check_snap_setup()
    _raise_on_failures(results)
    logger.info("claude-snap mode: ok")


def check_claude_cli() -> None:
    """Verify claude CLI is installed and authenticated.

    Usage-limit / account-outage failures are downgraded to a warning so the
    server still starts — per-message handlers will surface the same error
    when someone actually tries to use it.
    """
    if not shutil.which("claude"):
        raise SystemExit(
            f"claude CLI not found. Install it: {_AGENT_INSTALL_HINTS['claude']}"
        )
    result = subprocess.run(
        ["claude", "-p", "--output-format", "json", "ping"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode == 0:
        logger.info("claude CLI: ok")
        return

    if "auth" in result.stderr.lower():
        raise SystemExit("claude CLI not authenticated. Run: claude auth login")

    message = _extract_cli_result(result.stdout) or result.stderr.strip()

    from claude_on_the_fly.agent import ClaudeUnavailableError, _classify

    if isinstance(_classify(message), ClaudeUnavailableError):
        logger.warning("claude CLI unavailable (server starting anyway): %s", message)
        return

    raise SystemExit(f"claude CLI check failed (exit {result.returncode}): {message}")


async def check_telegram(token: str) -> None:
    """Verify Telegram bot token is valid."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = resp.json()
        if not data.get("ok"):
            raise SystemExit(
                f"Invalid Telegram bot token: {data.get('description', 'unknown error')}"
            )
        bot_name = data["result"]["username"]
        logger.info("Telegram bot: @%s", bot_name)


async def check_slack(app_token: str, user_token: str) -> str:
    """Verify Slack tokens are valid. Returns the user_id owning the user token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise SystemExit(
                f"Invalid Slack user token: {data.get('error', 'unknown')}"
            )
        user_id = data.get("user_id")
        if not user_id:
            raise SystemExit("Slack auth.test returned no user_id")
        logger.info(
            "Slack user token: %s (%s, %s)",
            data.get("user"),
            data.get("team"),
            user_id,
        )

        if not app_token.startswith("xapp-"):
            raise SystemExit("SLACK_APP_TOKEN must start with 'xapp-'")
        logger.info("Slack app token: format ok")
        return user_id


def check_gws_cli() -> None:
    """Verify gws CLI is installed and authenticated with Gmail scope."""
    if not shutil.which("gws"):
        raise SystemExit(
            "gws CLI not found. Install it: npm install -g @googleworkspace/cli"
        )
    result = subprocess.run(
        ["gws", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise SystemExit(f"gws auth check failed: {result.stderr.strip()}")
    status = json.loads(result.stdout)
    if not status.get("token_valid"):
        raise SystemExit("gws token invalid. Run: gws auth login")
    if "gmail.googleapis.com" not in status.get("enabled_apis", []):
        raise SystemExit("Gmail API not enabled in GCP project. Enable it first.")
    logger.info("gws CLI: ok (user: %s)", status.get("user", "unknown"))


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def run_telegram() -> tuple[str, int]:
    """Validate env vars and tokens. Returns (token, allowed_user_id)."""
    _setup_logging()
    _raise_on_failures(checks.check_telegram(os.environ))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    check_backend()
    asyncio.run(check_telegram(token))
    return token, allowed_user_id


def run_slack() -> tuple[str, str, str, set[str]]:
    """Validate env vars and tokens. Returns (app_token, user_token, user_id, allowed_user_ids).

    user_id is resolved from Slack auth.test — no need to pass it via env.
    """
    _setup_logging()
    _raise_on_failures(checks.check_slack(os.environ))
    app_token = os.environ["SLACK_APP_TOKEN"]
    user_token = os.environ["SLACK_USER_TOKEN"]
    allowed_raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "")
    allowed_user_ids = {uid.strip() for uid in allowed_raw.split(",") if uid.strip()}
    check_backend()
    user_id = asyncio.run(check_slack(app_token, user_token))
    logger.debug("preflight: user_id=%s allowed_user_ids=%s", user_id, allowed_user_ids)
    return app_token, user_token, user_id, allowed_user_ids


def run_gmail() -> tuple[str, set[str]]:
    """Validate env vars and gws CLI. Returns (gcp_project, allowed_senders)."""
    _setup_logging()
    _raise_on_failures(checks.check_gmail(os.environ))
    gcp_project = os.environ["GMAIL_GCP_PROJECT"]
    allowed_raw = os.environ["GMAIL_ALLOWED_SENDERS"]
    allowed_senders = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    check_backend()
    check_gws_cli()
    return gcp_project, allowed_senders
