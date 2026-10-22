"""Provider-confirmed account exhaustion and safe resume evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast


class AccountExhaustedError(RuntimeError):
    """An account needs credits or a quota reset before it can continue."""

    def __init__(
        self,
        backend: str,
        *,
        tools_started: bool = False,
        resume_ready: bool = False,
        thread_id: str | None = None,
    ) -> None:
        super().__init__(f"{backend}: Credit balance or usage quota exhausted")
        self.backend = backend
        self.tools_started = tools_started
        self.resume_ready = resume_ready
        self.thread_id = thread_id
        self.public_message = (
            "The selected account has exhausted its credits or usage quota."
        )


_EXHAUSTED_CODES = frozenset(
    {
        "insufficient_quota",
        "usage_limit_reached",
        "billing_hard_limit_reached",
        "credit_balance_too_low",
        "insufficient_credits",
        "UsageLimitExceeded",
        "usage_limit_exceeded",
    }
)
_EXHAUSTED_MESSAGES = re.compile(
    r"^(?:you(?:'|’)?ve hit your (?:usage )?limit(?:[.\s]|$)"
    r"|your credit balance is too low(?:[.\s]|$)"
    r"|credit balance is too low(?:[.\s]|$)"
    r"|you exceeded your current quota, please check your plan and billing details)",
    re.IGNORECASE,
)


def provider_exhausted(failure: object) -> bool:
    """Check an error value from a provider error channel, never assistant prose."""
    if isinstance(failure, str):
        text = failure.strip()
        if text.startswith(("{", "[")):
            try:
                return provider_exhausted(json.loads(text))
            except ValueError:
                return False
        return bool(_EXHAUSTED_MESSAGES.match(text))
    if isinstance(failure, list):
        return any(provider_exhausted(item) for item in failure)
    if not isinstance(failure, dict):
        return False
    details = cast(dict[str, Any], failure)
    code = details.get("code") or details.get("codex_error_info") or details.get("type")
    if details.get("status") in (401, 403) or details.get("status_code") in (401, 403):
        return False
    if code in (
        "authentication_error",
        "permission_error",
        "invalid_api_key",
        "unauthorized",
        "forbidden",
    ):
        return False
    if isinstance(code, str) and code in _EXHAUSTED_CODES:
        return True
    return provider_exhausted(details.get("error")) or provider_exhausted(
        details.get("message")
    )


def claude_error(
    result: dict, stderr: str, returncode: int, tool_counts: dict
) -> AccountExhaustedError | None:
    """Read terminal Claude failures without interpreting ordinary result text."""
    terminal_error = result.get("is_error") or str(
        result.get("subtype", "")
    ).startswith("error")
    if terminal_error and any(
        provider_exhausted(result.get(key)) for key in ("error", "result", "errors")
    ):
        return AccountExhaustedError("claude", tools_started=bool(tool_counts))
    if returncode and provider_exhausted(stderr):
        return AccountExhaustedError("claude", tools_started=bool(tool_counts))
    return None


def claude_resume_ready(path: Path, offset: int) -> bool:
    """Require durable results for every tool call appended during this turn."""
    calls: set[str] = set()
    results: set[str] = set()
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            records = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError):
        return False
    for record in records:
        parts = _claude_tool_parts(record)
        if parts is None:
            return False
        for part in parts:
            kind = part.get("type")
            if not isinstance(kind, str):
                return False
            if kind not in {"tool_use", "tool_result"}:
                continue
            identifier = part.get("id" if kind == "tool_use" else "tool_use_id")
            if not isinstance(identifier, str) or not identifier:
                return False
            target = calls if kind == "tool_use" else results
            target.add(identifier)
    return bool(calls) and calls <= results


def _claude_tool_parts(record: object) -> list[dict[str, Any]] | None:
    """Reject malformed transcript records before trusting their tool evidence."""
    if not isinstance(record, dict):
        return None
    entry = cast(dict[str, Any], record)
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return None
    content = cast(dict[str, Any], message).get("content", [])
    if entry.get("type") == "user" and isinstance(content, str):
        return []
    if not isinstance(content, list) or any(
        not isinstance(part, dict) for part in content
    ):
        return None
    return cast(list[dict[str, Any]], content)
