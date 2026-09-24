"""codex's ChatGPT login as the broker holds it: when it refreshes, what it
writes, and what it does when another process refreshed first. The token
endpoint is a fake `post`; the file, the lock and the rename are real."""

from __future__ import annotations

import base64
import datetime
import io
import json
import logging
import stat
import urllib.error

import pytest

from claude_on_the_fly import codex_auth
from claude_on_the_fly.codex_auth import ChatGPTLogin, RefreshRejected

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
# Kept before the autouse fixture freezes it, so the real clock is tested once.
_REAL_NOW = codex_auth._now


def _jwt(expires: datetime.datetime) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(expires.timestamp())})}.sig"


def _auth(*, refreshed: datetime.datetime, expires: datetime.datetime, n: int = 1):
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": f"id-{n}",
            "access_token": _jwt(expires),
            "refresh_token": f"refresh-{n}",
            "account_id": "acct",
        },
        "last_refresh": refreshed.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(codex_auth, "_now", lambda: NOW)


class Endpoint:
    """A fake token endpoint that records every body it was sent."""

    def __init__(self, answer=None, error: Exception | None = None, before=None):
        self.bodies: list[dict] = []
        self.answer = answer or {
            "access_token": _jwt(NOW + datetime.timedelta(days=10)),
            "id_token": "id-new",
            "refresh_token": "refresh-new",
        }
        self.error = error
        self.before = before

    def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        if self.before is not None:
            self.before()
        if self.error is not None:
            raise self.error
        return self.answer


def _login(tmp_path, auth: dict, endpoint: Endpoint) -> tuple[ChatGPTLogin, object]:
    path = tmp_path / "codex" / "auth.json"
    path.parent.mkdir()
    path.write_text(json.dumps(auth))
    return ChatGPTLogin(path, tmp_path / "state" / "codex-auth.lock", endpoint), path


def _fresh() -> dict:
    return _auth(
        refreshed=NOW - datetime.timedelta(days=1),
        expires=NOW + datetime.timedelta(days=9),
    )


def _stale() -> dict:
    return _auth(
        refreshed=NOW - datetime.timedelta(days=7),
        expires=NOW + datetime.timedelta(days=3),
    )


def test_the_real_clock_is_utc():
    assert _REAL_NOW().tzinfo is datetime.UTC


# --- setting, URL and argv ---


def test_enabled_reads_the_setting(monkeypatch):
    monkeypatch.setenv(codex_auth.SETTING, "true")
    assert codex_auth.enabled()
    monkeypatch.setenv(codex_auth.SETTING, "0")
    assert not codex_auth.enabled()
    monkeypatch.delenv(codex_auth.SETTING)
    assert not codex_auth.enabled()


def test_published_url_is_none_until_the_broker_publishes(monkeypatch):
    monkeypatch.delenv(codex_auth.BASE_URL_ENV, raising=False)
    assert codex_auth.published_url() is None
    monkeypatch.setenv(codex_auth.BASE_URL_ENV, "http://127.0.0.1:1/_session/t/chatgpt")
    assert codex_auth.published_url() == "http://127.0.0.1:1/_session/t/chatgpt"


def test_provider_args_point_the_model_and_chatgpt_calls_at_the_broker():
    args = codex_auth.provider_args("http://127.0.0.1:1/_session/t/chatgpt/")
    assert args == [
        "-c",
        'model_providers.cotf={name="cotf",'
        'base_url="http://127.0.0.1:1/_session/t/chatgpt/backend-api/codex",'
        'wire_api="responses",requires_openai_auth=false}',
        "-c",
        "model_provider=cotf",
        "-c",
        'chatgpt_base_url="http://127.0.0.1:1/_session/t/chatgpt/backend-api/"',
    ]


# --- when a refresh is due ---


@pytest.mark.parametrize(
    ("auth", "due"),
    [
        (_fresh(), False),
        (_stale(), True),
        (
            _auth(
                refreshed=NOW - datetime.timedelta(days=1),
                expires=NOW + datetime.timedelta(minutes=4),
            ),
            True,
        ),
        ({**_fresh(), "last_refresh": None}, True),
        ({**_fresh(), "last_refresh": "yesterday"}, True),
        (
            {**_fresh(), "tokens": {**_fresh()["tokens"], "access_token": "opaque"}},
            True,
        ),
        (
            {**_fresh(), "tokens": {**_fresh()["tokens"], "access_token": "a.!!.b"}},
            True,
        ),
    ],
    ids=[
        "fresh",
        "seven-days",
        "about-to-expire",
        "no-last-refresh",
        "bad-last-refresh",
        "not-a-jwt",
        "bad-payload",
    ],
)
def test_refresh_due(auth, due):
    assert codex_auth.refresh_due(auth, NOW) is due


# --- the token endpoint ---


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_post_refresh_sends_json_and_returns_the_answer(monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append((request.full_url, request.get_method(), json.loads(request.data)))
        return _Response(b'{"access_token": "a"}')

    monkeypatch.setattr(codex_auth.urllib.request, "urlopen", urlopen)
    assert codex_auth._post_refresh({"grant_type": "refresh_token"}) == {
        "access_token": "a"
    }
    assert sent == [(codex_auth.REFRESH_URL, "POST", {"grant_type": "refresh_token"})]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b'{"error": {"code": "refresh_token_reused"}}', "refresh_token_reused"),
        (b'{"error": {"type": "invalid_request_error"}}', "invalid_request_error"),
        (b'{"error": {}}', "unknown"),
        (b'{"error": "invalid_grant"}', "invalid_grant"),
        (b"{}", "unknown"),
        (b"<html>", "unreadable"),
        (b"[]", "unreadable"),
    ],
)
def test_post_refresh_reports_the_error_code_only(monkeypatch, body, code):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(body))

    monkeypatch.setattr(codex_auth.urllib.request, "urlopen", urlopen)
    with pytest.raises(RefreshRejected) as caught:
        codex_auth._post_refresh({})
    assert (caught.value.status, caught.value.code) == (401, code)


# --- the login ---


def test_check_accepts_a_chatgpt_login(tmp_path):
    login, _ = _login(tmp_path, _fresh(), Endpoint())
    login.check()


def test_check_refuses_a_file_without_a_chatgpt_login(tmp_path):
    login, _ = _login(tmp_path, {"OPENAI_API_KEY": "k", "tokens": None}, Endpoint())
    with pytest.raises(RuntimeError, match="codex login"):
        login.check()


async def test_headers_when_fresh_spend_no_refresh(tmp_path):
    endpoint = Endpoint()
    auth = _fresh()
    login, _ = _login(tmp_path, auth, endpoint)
    assert await login.headers() == {
        "Authorization": f"Bearer {auth['tokens']['access_token']}",
        "ChatGPT-Account-Id": "acct",
    }
    assert endpoint.bodies == []


async def test_a_due_login_is_refreshed_and_written_back_in_place(tmp_path):
    endpoint = Endpoint()
    login, path = _login(tmp_path, _stale(), endpoint)
    inode = path.stat().st_ino

    headers = await login.headers()

    assert endpoint.bodies == [
        {
            "grant_type": "refresh_token",
            "client_id": codex_auth.CLIENT_ID,
            "refresh_token": "refresh-1",
        }
    ]
    written = json.loads(path.read_text())
    assert written["tokens"] == {
        "id_token": "id-new",
        "access_token": endpoint.answer["access_token"],
        "refresh_token": "refresh-new",
        "account_id": "acct",
    }
    # codex owns the format, so the fields the broker does not touch survive.
    assert written["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" in written
    assert written["last_refresh"] == "2026-09-24T12:00:00.000000Z"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert headers["Authorization"] == f"Bearer {endpoint.answer['access_token']}"
    # Same inode: a rename would detach the Linux jail's mask over the file.
    assert path.stat().st_ino == inode
    assert sorted(p.name for p in path.parent.iterdir()) == ["auth.json"]


async def test_an_answer_without_a_new_refresh_token_keeps_the_old_one(tmp_path):
    endpoint = Endpoint(
        answer={"access_token": _jwt(NOW + datetime.timedelta(days=10))}
    )
    login, path = _login(tmp_path, _stale(), endpoint)
    await login.headers()
    tokens = json.loads(path.read_text())["tokens"]
    assert tokens["refresh_token"] == "refresh-1"
    assert tokens["id_token"] == "id-1"


async def test_a_refresh_by_another_process_is_picked_up_without_one_of_ours(
    tmp_path,
):
    endpoint = Endpoint()
    login, path = _login(tmp_path, _fresh(), endpoint)
    await login.headers()
    theirs = _auth(refreshed=NOW, expires=NOW + datetime.timedelta(days=10), n=2)
    path.write_text(json.dumps(theirs, indent=4))
    headers = await login.headers()
    assert headers["Authorization"] == f"Bearer {theirs['tokens']['access_token']}"
    assert endpoint.bodies == []


async def test_losing_the_race_to_a_hand_run_codex_is_not_a_failure(tmp_path, caplog):
    # The operator's codex refreshes between our read and our POST, so the
    # endpoint calls our token already used, and the file holds theirs.
    theirs = _auth(refreshed=NOW, expires=NOW + datetime.timedelta(days=10), n=2)
    holder: dict = {}

    def their_refresh():
        holder["path"].write_text(json.dumps(theirs, indent=4))

    endpoint = Endpoint(
        error=RefreshRejected(401, "refresh_token_reused"), before=their_refresh
    )
    login, path = _login(tmp_path, _stale(), endpoint)
    holder["path"] = path

    with caplog.at_level(logging.INFO, logger="claude_on_the_fly.codex_auth"):
        headers = await login.headers()

    assert headers["Authorization"] == f"Bearer {theirs['tokens']['access_token']}"
    assert "another process refreshed first" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_dead_login_logs_once_and_is_retried_after_codex_login(
    tmp_path, caplog
):
    endpoint = Endpoint(error=RefreshRejected(401, "refresh_token_expired"))
    login, path = _login(tmp_path, _stale(), endpoint)

    await login.headers()
    await login.headers()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "codex login" in errors[0].getMessage()
    assert "refresh_token_expired" in errors[0].getMessage()
    assert len(endpoint.bodies) == 1

    # The operator logs in again: a new file, so the broker tries again.
    path.write_text(
        json.dumps(
            _auth(
                refreshed=NOW - datetime.timedelta(days=8),
                expires=NOW + datetime.timedelta(days=2),
                n=3,
            )
        )
    )
    endpoint.error = None
    await login.headers()
    assert endpoint.bodies[-1]["refresh_token"] == "refresh-3"


@pytest.mark.parametrize("error", [OSError("offline"), ValueError("bad json")])
async def test_a_transient_failure_keeps_the_current_token_and_retries(
    tmp_path, caplog, error
):
    endpoint = Endpoint(error=error)
    auth = _stale()
    login, _ = _login(tmp_path, auth, endpoint)

    headers = await login.headers()
    assert headers["Authorization"] == f"Bearer {auth['tokens']['access_token']}"
    assert "will retry" in caplog.text

    endpoint.error = None
    await login.headers()
    assert len(endpoint.bodies) == 2


async def test_an_unwritable_file_keeps_the_new_token_in_memory(tmp_path, caplog):
    endpoint = Endpoint()
    login, path = _login(tmp_path, _stale(), endpoint)
    path.chmod(0o400)
    headers = await login.headers()

    assert headers["Authorization"] == f"Bearer {endpoint.answer['access_token']}"
    assert "could not write" in caplog.text
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "refresh-1"
    assert sorted(p.name for p in path.parent.iterdir()) == ["auth.json"]


async def test_recover_uses_a_token_another_process_already_refreshed(tmp_path):
    endpoint = Endpoint()
    login, path = _login(tmp_path, _fresh(), endpoint)
    sent = await login.headers()
    path.write_text(
        json.dumps(_auth(refreshed=NOW, expires=NOW + datetime.timedelta(days=10), n=2))
    )
    assert await login.recover(sent) is True
    assert endpoint.bodies == []


async def test_recover_refreshes_a_refused_token_whatever_its_age(tmp_path):
    endpoint = Endpoint()
    login, _ = _login(tmp_path, _fresh(), endpoint)
    sent = await login.headers()
    assert await login.recover(sent) is True
    assert len(endpoint.bodies) == 1
    assert (await login.headers()) != sent


async def test_recover_reports_false_when_the_login_is_dead(tmp_path):
    endpoint = Endpoint(error=RefreshRejected(400, "refresh_token_invalidated"))
    login, _ = _login(tmp_path, _fresh(), endpoint)
    sent = await login.headers()
    assert await login.recover(sent) is False


# --- the route ---


def test_route_serves_the_operators_login(tmp_path, monkeypatch):
    from claude_on_the_fly import agent, envfile

    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(_fresh()))
    monkeypatch.setattr(envfile, "codex_home", lambda: home)
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "data")

    route = codex_auth.route()

    assert route.prefix == "/chatgpt"
    assert route.upstream == "https://chatgpt.com"
    assert route.base_url_env_var == codex_auth.BASE_URL_ENV
    assert route.allowed_tails == codex_auth.ALLOWED_TAILS
    assert route.methods == codex_auth.ALLOWED_METHODS
    assert route.source._path == home / "auth.json"
    assert route.source._lock_path == tmp_path / "data" / "state" / "codex-auth.lock"


def test_the_route_allows_codex_apps():
    """With plugins on, codex's MCP client polls ChatGPT's apps endpoint. A
    refusal asks the operator and denies every 10 to 30 seconds for the run."""
    assert "backend-api/ps/mcp" in codex_auth.ALLOWED_TAILS


def test_route_refuses_to_start_without_a_login(tmp_path, monkeypatch):
    from claude_on_the_fly import envfile

    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "k"}))
    monkeypatch.setattr(envfile, "codex_home", lambda: home)
    with pytest.raises(RuntimeError, match="holds no ChatGPT login"):
        codex_auth.route()


async def test_a_read_that_lands_mid_write_uses_the_last_good_copy(tmp_path):
    auth = _fresh()
    login, path = _login(tmp_path, auth, Endpoint())
    first = await login.headers()
    path.write_text('{"tokens": {"access_')
    assert await login.headers() == first


def test_a_first_read_of_a_broken_file_fails(tmp_path):
    login, path = _login(tmp_path, _fresh(), Endpoint())
    path.write_text("{")
    with pytest.raises(ValueError):
        login.check()
