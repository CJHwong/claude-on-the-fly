"""codex's ChatGPT login, held by the daemon instead of the agent.

A codex turn signed in with ChatGPT reads `auth.json` for its bearer token, so a
jailed turn used to hold the operator's refresh token: a credential that renews
itself and outlives the turn. With `agent.codex.chatgpt_via_broker` on, the
credential broker owns the file. The turn talks to a custom model provider on
the broker's loopback port, the broker adds the real `Authorization` and
`ChatGPT-Account-Id` headers on the upstream leg, and the jail hides the file.

The broker is then the one regular refresher. The file stays where codex keeps
it because the operator still runs `codex` by hand on the same machine. Their
run may refresh too, so the file is read again on every request (cached by its
stat), a refresh happens under a file lock that the chat daemon and the jobs
worker share, and a refused refresh re-reads the file before it counts as a
failure: a changed file means another process won the race.

Measured against the live token endpoint: a refresh answers 200 with a new
access, id and refresh token, the refresh token rotates on every call, and
codex keeps working with the tokens written back. A second refresh 17 seconds
later also succeeded, so the `earliest_refresh_at` hint the endpoint returns
(nine days out) is advisory.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import fcntl
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from claude_on_the_fly import settings

logger = logging.getLogger(__name__)

SETTING = "COTF_CODEX_CHATGPT_VIA_BROKER"
# Published by the broker once the route serves. A `*_BASE_URL` name on purpose:
# the jail derives its loopback allow from those, and the codex backend and the
# jail both key on this variable, so they cannot disagree about whether a turn
# is brokered.
BASE_URL_ENV = "COTF_CHATGPT_BASE_URL"
ROUTE_PREFIX = "/chatgpt"
UPSTREAM = "https://chatgpt.com"
# What codex 0.156 sends to a custom provider plus `chatgpt_base_url`, measured
# through the route. With auth.json hidden (the jail on) it makes the first two
# calls only. With auth.json visible (the jail off) it also makes the rest, and
# they are allowed so that stage behaves like codex does without the broker.
# A jailed turn can reach them too, with the operator's login: plugin lists,
# user settings, and analytics posts.
ALLOWED_TAILS = frozenset(
    {
        "backend-api/codex/responses",
        "backend-api/plugins/featured",
        "backend-api/codex/models",
        "backend-api/codex/analytics-events/events",
        "backend-api/ps/plugins/installed",
        "backend-api/ps/plugins/list",
        "backend-api/ps/plugins/suggested/codex",
        "backend-api/wham/settings/user",
    }
)
ALLOWED_METHODS = frozenset({"GET", "POST"})

# codex's own values, from codex-rs/login/src/auth/manager.rs.
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# A day earlier than codex's own eight, so the operator's hand-run codex rarely
# races the broker for the single-use refresh token.
REFRESH_AFTER = datetime.timedelta(days=7)
EXPIRY_MARGIN = datetime.timedelta(minutes=5)
_TIMEOUT_SECONDS = 30
_TRUTHY = {"1", "true", "yes", "on"}

Post = Callable[[dict[str, str]], dict[str, Any]]


class RefreshRejected(Exception):
    """The token endpoint answered with an error status."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"token refresh rejected: {status} {code}")
        self.status = status
        self.code = code


def enabled() -> bool:
    """Whether the operator asked for the brokered login. Off by default."""
    return settings.get(SETTING).strip().lower() in _TRUTHY


def published_url() -> str | None:
    """The broker URL codex should use, or None when no broker serves the route."""
    return os.environ.get(BASE_URL_ENV) or None


def provider_args(base_url: str) -> list[str]:
    """`-c` overrides that send codex's model and ChatGPT calls to the broker.

    `requires_openai_auth=false` because the broker, not codex, supplies the
    bearer. Measured with no `auth.json` visible: a turn, a tool call and a
    resume all completed, and codex made one side call instead of about fifty.
    The built-in `openai` provider cannot be pointed here, because codex refuses
    a plain-HTTP ChatGPT origin for it.
    """
    root = base_url.rstrip("/")
    provider = (
        'model_providers.cotf={name="cotf",'
        f'base_url="{root}/backend-api/codex",'
        'wire_api="responses",requires_openai_auth=false}'
    )
    return [
        "-c",
        provider,
        "-c",
        "model_provider=cotf",
        "-c",
        f'chatgpt_base_url="{root}/backend-api/"',
    ]


def _claims(jwt: str) -> dict[str, Any]:
    """A JWT's payload, unverified. Only its timestamps are read."""
    try:
        part = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _parse_time(value: object) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def refresh_due(auth: Mapping[str, Any], now: datetime.datetime) -> bool:
    """Seven days since the last refresh, or the access token about to expire.

    A missing or unreadable `last_refresh` counts as due: codex treats it the
    same way, and a refresh is cheap next to a turn failing on a stale token.
    """
    last = _parse_time(auth.get("last_refresh"))
    if last is None or now - last >= REFRESH_AFTER:
        return True
    expiry = _claims(auth["tokens"].get("access_token", "")).get("exp")
    if not isinstance(expiry, int | float):
        return True
    return datetime.datetime.fromtimestamp(expiry, datetime.UTC) - now <= EXPIRY_MARGIN


def _post_refresh(body: dict[str, str]) -> dict[str, Any]:
    """POST one refresh to the token endpoint. Raises RefreshRejected on 4xx/5xx."""
    request = urllib.request.Request(
        REFRESH_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RefreshRejected(error.code, _error_code(error.read())) from None


def _error_code(raw: bytes) -> str:
    """The endpoint's error code, never the body: an error body can echo input."""
    try:
        detail = json.loads(raw).get("error")
    except (ValueError, AttributeError):
        return "unreadable"
    if isinstance(detail, dict):
        return str(detail.get("code") or detail.get("type") or "unknown")
    return str(detail or "unknown")


class ChatGPTLogin:
    """The broker's view of `auth.json`: current headers, and refresh when due.

    A refresh runs in a worker thread, because the lock is a blocking `flock`
    and the refresh is a blocking HTTP call. Reading the cached file does not.
    """

    def __init__(self, path: Path, lock_path: Path, post: Post = _post_refresh) -> None:
        self._path = path
        self._lock_path = lock_path
        self._post = post
        self._cached: tuple[tuple[int, int], dict[str, Any]] | None = None
        # The refresh token a permanent failure was for. Not retried until the
        # file changes, so one expired login logs once instead of per request.
        self._failed_token: str | None = None
        self._guard = asyncio.Lock()

    def check(self) -> None:
        """Fail at startup when the file holds no ChatGPT login to broker.

        The operator opted in, so a missing login is a configuration error to
        report before serving, not a 401 to discover on the first turn.
        """
        tokens = self._read().get("tokens")
        if not isinstance(tokens, dict) or not all(
            tokens.get(name) for name in ("access_token", "refresh_token", "account_id")
        ):
            raise RuntimeError(
                f"agent.codex.chatgpt_via_broker is on but {self._path} holds no "
                "ChatGPT login; run `codex login` on this host first"
            )

    async def headers(self) -> dict[str, str]:
        """The two headers the upstream needs, refreshing first when due."""
        async with self._guard:
            await asyncio.to_thread(self._refresh_if, refresh_due)
        return self._headers_from(self._read())

    async def recover(self, sent: Mapping[str, str]) -> bool:
        """After an upstream 401: whether a retry now carries a different token.

        Another process may already have refreshed, in which case the file holds
        a new token and no refresh is spent. Otherwise the token that was just
        refused is refreshed whatever its age.
        """

        def refused(auth: Mapping[str, Any], _now: datetime.datetime) -> bool:
            return self._headers_from(auth) == dict(sent)

        async with self._guard:
            await asyncio.to_thread(self._refresh_if, refused)
        return self._headers_from(self._read()) != dict(sent)

    @staticmethod
    def _headers_from(auth: Mapping[str, Any]) -> dict[str, str]:
        tokens = auth["tokens"]
        return {
            "Authorization": f"Bearer {tokens['access_token']}",
            "ChatGPT-Account-Id": tokens["account_id"],
        }

    def _read(self) -> dict[str, Any]:
        """The file's content, re-read only when its stat changed."""
        stat = self._path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if self._cached is None or self._cached[0] != key:
            self._cached = (key, json.loads(self._path.read_text()))
        return self._cached[1]

    def _refresh_if(
        self, needed: Callable[[Mapping[str, Any], datetime.datetime], bool]
    ) -> None:
        """Refresh under the shared lock when `needed` still holds after a re-read."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self._read()
            refresh_token = current["tokens"]["refresh_token"]
            if refresh_token == self._failed_token or not needed(current, _now()):
                return
            try:
                answer = self._post(
                    {
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": refresh_token,
                    }
                )
            except RefreshRejected as error:
                self._rejected(error, refresh_token)
                return
            except (OSError, ValueError) as error:
                # Transient: the current access token may well still be valid, so
                # the request goes ahead with it and the next one tries again.
                logger.warning("codex auth: refresh failed, will retry: %s", error)
                return
            self._write(current, answer)

    def _rejected(self, error: RefreshRejected, refresh_token: str) -> None:
        # Single-use tokens: "already used" usually means the operator's own codex
        # refreshed first. The file then holds its new token and nothing failed.
        self._cached = None
        if self._read()["tokens"]["refresh_token"] != refresh_token:
            logger.info("codex auth: another process refreshed first; using its token")
            return
        self._failed_token = refresh_token
        logger.error(
            "codex auth: the ChatGPT login cannot be refreshed (%d %s). Codex turns "
            "will fail until the operator runs `codex login` on this host.",
            error.status,
            error.code,
        )

    def _write(self, current: dict[str, Any], answer: Mapping[str, Any]) -> None:
        """Write the new tokens beside the old file, then rename over it.

        Every other field is kept, because codex owns the format. The temporary
        name starts with `auth.json` so the jail rule hiding the file covers it
        too.
        """
        updated = dict(current)
        tokens = dict(current["tokens"])
        for field in ("id_token", "access_token", "refresh_token"):
            if answer.get(field):
                tokens[field] = answer[field]
        updated["tokens"] = tokens
        updated["last_refresh"] = _now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            handle, temp = tempfile.mkstemp(
                dir=self._path.parent, prefix="auth.json.cotf-"
            )
            try:
                with os.fdopen(handle, "w") as out:
                    json.dump(updated, out, indent=2)
                os.chmod(temp, 0o600)
                os.replace(temp, self._path)
            finally:
                Path(temp).unlink(missing_ok=True)
        except OSError as error:
            # The old refresh token is spent, so dropping the answer would lose
            # the login. Serve the new tokens from memory, keyed to the file as it
            # stands, and say loudly that the file is now stale.
            stat = self._path.stat()
            self._cached = ((stat.st_mtime_ns, stat.st_size), updated)
            logger.error(
                "codex auth: refreshed but could not write %s (%s). This daemon "
                "keeps the new token in memory; a restart or a hand-run codex will "
                "need `codex login`.",
                self._path,
                error,
            )
            return
        self._cached = None
        self._failed_token = None
        logger.info("codex auth: refreshed the ChatGPT login")


def route() -> Any:
    """The broker route serving the operator's ChatGPT login.

    Raises when the login is missing, so an operator who opted in learns at
    startup rather than on the first turn.
    """
    from claude_on_the_fly import broker, envfile
    from claude_on_the_fly.agent import DATA_DIR

    login = ChatGPTLogin(
        envfile.codex_home() / "auth.json", DATA_DIR / "state" / "codex-auth.lock"
    )
    login.check()
    return broker.Route(
        prefix=ROUTE_PREFIX,
        upstream=UPSTREAM,
        header="authorization",
        keychain_service="",
        base_url_env_var=BASE_URL_ENV,
        methods=ALLOWED_METHODS,
        allowed_tails=ALLOWED_TAILS,
        source=login,
    )
