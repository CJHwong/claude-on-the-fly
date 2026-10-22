"""Retry confirmed account exhaustion within one configured account pool."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from claude_on_the_fly import accounts
from claude_on_the_fly.quota import AccountExhaustedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from claude_on_the_fly.agent import AgentProfile, Response

log = logging.getLogger(__name__)

_CONTINUE_PROMPT = (
    "Continue the interrupted turn from the saved conversation and durable tool "
    "results. The previous account exhausted its credits or usage quota. "
    "Do not repeat completed tool actions or other side effects. "
    "Finish the remaining work for the latest user request. "
    "If the saved progress is unavailable, stop and report that problem."
)


class AccountFallbackError(RuntimeError):
    """Fallback stopped with a safe explanation for the requester."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


def _candidates(profile: AgentProfile, platform: str, session: str) -> tuple[str, ...]:
    pool = profile.account_pool
    selected = profile.account
    saved = accounts.saved_selection(platform, session, profile.backend)
    if (
        saved is not None
        and saved.pool == pool
        and saved.account is not None
        and saved.account.name in pool
    ):
        selected = saved.account.name
    if selected not in pool:
        raise accounts.AccountError("Selected account is outside its fallback pool")
    start = pool.index(selected)
    return pool[start:] + pool[:start]


async def run(
    profile: AgentProfile,
    workspace: Path,
    session_uuid: str,
    prompt: str,
    platform: str,
    invoke: Callable[[AgentProfile, str, float | None], Awaitable[Response]],
    timeout: float | None,
) -> Response:
    """Keep the session and deadline while switching only exhausted accounts."""
    if not profile.account_pool:
        return await invoke(profile, prompt, timeout)
    candidates = _candidates(profile, platform, session_uuid)
    deadline = None if timeout is None else time.monotonic() + timeout
    attempt_prompt = prompt
    async with asyncio.timeout(timeout):
        for name in candidates:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Account fallback turn deadline expired")
            candidate = accounts.apply_profile(
                profile, accounts.named(name, profile.backend)
            )
            if name != candidates[0]:
                log.info("Account switch: backend=%s account=%s", profile.backend, name)
            log.info("Account attempt: backend=%s account=%s", profile.backend, name)
            try:
                response = await invoke(candidate, attempt_prompt, remaining)
            except AccountExhaustedError as failure:
                if failure.backend != profile.backend:
                    raise
                if failure.tools_started and not failure.resume_ready:
                    raise AccountFallbackError(
                        "The account is exhausted. Saved progress is incomplete, "
                        "so automatic switching stopped to avoid repeated actions."
                    ) from failure
                if failure.tools_started:
                    attempt_prompt = _CONTINUE_PROMPT
                log.info(
                    "Account exhausted: backend=%s account=%s", profile.backend, name
                )
                continue
            if name != candidates[0]:
                accounts.remember(
                    platform, session_uuid, profile.backend, name, profile.account_pool
                )
            log.info("Account succeeded: backend=%s account=%s", profile.backend, name)
            return response
    aliases = ", ".join(candidates)
    raise AccountFallbackError(
        f"All configured {profile.backend} accounts have exhausted their credits "
        f"or usage quota: {aliases}."
    )
