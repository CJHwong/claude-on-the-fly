"""Preflight checks - validate tokens and tools before starting."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import httpx

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Read a required env var or exit with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


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


def check_backend() -> None:
    """Validate the configured agent backend before startup.

    Dispatches by `AGENT_BACKEND` (default `claude`) and `CLAUDE_MODE`
    (default `native`). Native mode runs the live `claude -p ping`;
    ollama mode skips the live ping and validates the ollama wrap instead.
    """
    backend_name = os.environ.get("AGENT_BACKEND", "claude").lower()
    if backend_name != "claude":
        raise SystemExit(f"Unknown AGENT_BACKEND: {backend_name!r} (supported: claude)")
    mode = os.environ.get("CLAUDE_MODE", "native").lower()
    if mode == "native":
        check_claude_cli()
        return
    if mode == "ollama":
        check_ollama_mode()
        return
    raise SystemExit(f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama)")


def check_ollama_mode() -> None:
    """Validate `ollama launch claude` mode: both CLIs present, model available."""
    if not shutil.which("claude"):
        raise SystemExit(
            "claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code"
        )
    if not shutil.which("ollama"):
        raise SystemExit("ollama CLI not found. Install it: https://ollama.com")
    model = os.environ.get("OLLAMA_MODEL", "").strip()
    if not model:
        raise SystemExit("CLAUDE_MODE=ollama requires OLLAMA_MODEL to be set")
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
    logger.info("ollama launch mode: ok (model=%s)", model)


def check_claude_cli() -> None:
    """Verify claude CLI is installed and authenticated.

    Usage-limit / account-outage failures are downgraded to a warning so the
    server still starts — per-message handlers will surface the same error
    when someone actually tries to use it.
    """
    if not shutil.which("claude"):
        raise SystemExit(
            "claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code"
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
    token = require_env("TELEGRAM_BOT_TOKEN")
    raw_user_id = require_env("TELEGRAM_ALLOWED_USER_ID")
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError:
        raise SystemExit(
            f"TELEGRAM_ALLOWED_USER_ID must be an integer, got: {raw_user_id!r}"
        )
    check_backend()
    asyncio.run(check_telegram(token))
    return token, allowed_user_id


def run_slack() -> tuple[str, str, str, set[str]]:
    """Validate env vars and tokens. Returns (app_token, user_token, user_id, allowed_user_ids).

    user_id is resolved from Slack auth.test — no need to pass it via env.
    """
    _setup_logging()
    app_token = require_env("SLACK_APP_TOKEN")
    user_token = require_env("SLACK_USER_TOKEN")
    allowed_raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "")
    allowed_user_ids = {uid.strip() for uid in allowed_raw.split(",") if uid.strip()}
    check_backend()
    user_id = asyncio.run(check_slack(app_token, user_token))
    logger.debug("preflight: user_id=%s allowed_user_ids=%s", user_id, allowed_user_ids)
    return app_token, user_token, user_id, allowed_user_ids


def run_gmail() -> tuple[str, set[str]]:
    """Validate env vars and gws CLI. Returns (gcp_project, allowed_senders)."""
    _setup_logging()
    gcp_project = require_env("GMAIL_GCP_PROJECT")
    allowed_raw = require_env("GMAIL_ALLOWED_SENDERS")
    allowed_senders = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    if not allowed_senders:
        raise SystemExit(
            "GMAIL_ALLOWED_SENDERS must contain at least one email address"
        )
    check_backend()
    check_gws_cli()
    return gcp_project, allowed_senders
