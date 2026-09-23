"""The jobs daemon's sandbox services: the chat daemon's brokers, minus the asking.

A job runs unattended, so nobody can answer a prompt. It gets the same command
broker, credential broker and egress proxy a chat turn gets, and anything that
would ask the operator is refused instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

import pytest
from aiohttp import ClientSession

from claude_on_the_fly import commands, sandbox
from claude_on_the_fly.jobs.brokers import JobBrokers
from tests.test_egress import connect_through

ECHO = commands.ShimmedTool(name="echo", allow=((),))


@pytest.fixture(autouse=True)
def no_keychain_routes(monkeypatch):
    """The credential broker reads the host keychain; a test must not."""
    from claude_on_the_fly import broker

    monkeypatch.setattr(broker, "routes_from_keychain", lambda _routes: {})


async def test_the_credential_broker_never_asks(monkeypatch):
    """Chat routes a credential-broker approval to the operator. A job cannot,
    so its broker gets the gate that denies without asking."""
    from claude_on_the_fly import approvals, broker

    monkeypatch.setenv("COTF_SANDBOX", "env")
    seen: dict = {}

    class FakeBroker:
        async def stop(self):
            seen["stopped"] = True

    async def start_default_broker(approvals=None):
        seen["gate"] = approvals._gate
        return FakeBroker()

    monkeypatch.setattr(broker, "start_default_broker", start_default_broker)
    brokers = JobBrokers(tools=(ECHO,))
    await brokers.start()
    await brokers.stop()
    assert isinstance(seen["gate"], approvals.DenyAllGate)
    assert seen["stopped"] is True


async def _run_echo(env: dict[str, str], workspace: Path) -> tuple[int, int | None]:
    """HTTP status and command rc a shim would get for `echo hi` with this env."""
    async with ClientSession() as client:
        resp = await client.post(
            f"{env[commands.ENDPOINT_ENV]}/run",
            json={"tool": "echo", "argv": ["hi"], "cwd": str(workspace)},
            headers={commands.TOKEN_HEADER: env[commands.TOKEN_ENV]},
        )
        return resp.status, (await resp.json()).get("rc")


async def test_nothing_starts_without_a_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "off")
    monkeypatch.delenv(commands.ENDPOINT_ENV, raising=False)
    brokers = JobBrokers(tools=(ECHO,))
    await brokers.start()
    try:
        async with brokers.for_job(tmp_path, "k") as env:
            assert env == {}
        assert commands.ENDPOINT_ENV not in os.environ
    finally:
        await brokers.stop()


async def test_a_job_token_is_bound_to_its_workspace_and_revoked_after(
    monkeypatch, tmp_path
):
    """Before this, a job reached the shim with no endpoint and every brokered
    tool failed. The endpoint is published daemon-wide like the chat daemon does;
    the token is per job, so it dies with the job."""
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("COTF_SANDBOX_EGRESS", "off")
    monkeypatch.delenv(commands.ENDPOINT_ENV, raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    brokers = JobBrokers(tools=(ECHO,))
    await brokers.start()
    try:
        assert os.environ[commands.ENDPOINT_ENV].startswith("http://127.0.0.1:")
        assert commands.TOKEN_ENV not in os.environ
        async with brokers.for_job(workspace, "k") as env:
            job_env = dict(env)
            assert await _run_echo(job_env, workspace) == (200, 0)
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            assert await _run_echo(job_env, elsewhere) == (200, 126)
        assert (await _run_echo(job_env, workspace))[0] == 403
    finally:
        await brokers.stop()
    assert commands.ENDPOINT_ENV not in os.environ


async def test_a_host_off_the_allowlist_is_refused_without_asking(
    monkeypatch, tmp_path, caplog
):
    """Gated egress asks the operator in a chat. A job has nobody to ask, so the
    gate is DenyAllGate and the answer is always no."""
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("COTF_SANDBOX_EGRESS", "gated")
    caplog.set_level(logging.INFO)
    brokers = JobBrokers(tools=(ECHO,))
    await brokers.start()
    try:
        async with brokers.for_job(tmp_path, "k") as env:
            proxy = env["HTTPS_PROXY"]
            port = int(proxy.rsplit(":", 1)[1].rstrip("/"))

            # A public answer, so the refusal comes from the gate and not DNS.
            async def resolve_public(host, port, **kwargs):
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))
                ]

            monkeypatch.setattr(
                asyncio.get_running_loop(), "getaddrinfo", resolve_public, raising=False
            )
            status, _body = await connect_through(port, "not-on-the-list.example:443")
            assert status.startswith(b"HTTP/1.1 403")
        assert "no approval channel configured" in caplog.text
    finally:
        await brokers.stop()


async def test_the_relay_is_opened_with_the_job_env_and_closed(monkeypatch, tmp_path):
    """On a Linux jail the namespace holds nothing until the relay bridges the
    brokered ports in, and chat turns were the only ones that opened it."""
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("COTF_SANDBOX_EGRESS", "off")
    seen: dict = {}

    class FakeRelay:
        async def close(self):
            seen["closed"] = True

    async def open_relay(overrides, key):
        seen["overrides"] = dict(overrides)
        seen["key"] = key
        return FakeRelay()

    monkeypatch.setattr(sandbox, "open_session_relay", open_relay)
    brokers = JobBrokers(tools=(ECHO,))
    await brokers.start()
    try:
        model_env = {"OLLAMA_HOST": "http://127.0.0.1:11434"}
        async with brokers.for_job(tmp_path, "job-7", model_env) as env:
            assert commands.TOKEN_ENV in seen["overrides"]
            # The model server rides into the relay with the brokers, or an
            # ollama job has no route to it.
            assert seen["overrides"]["OLLAMA_HOST"] == model_env["OLLAMA_HOST"]
            assert seen["key"] == "job-7"
            assert "closed" not in seen
            assert env[commands.TOKEN_ENV] == seen["overrides"][commands.TOKEN_ENV]
        assert seen["closed"] is True
    finally:
        await brokers.stop()


async def test_a_failed_start_revokes_what_already_started(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.delenv(commands.ENDPOINT_ENV, raising=False)

    async def refuse(self, *args, **kwargs):
        raise OSError("port in use")

    monkeypatch.setattr(commands.CommandBroker, "start", refuse)
    brokers = JobBrokers(tools=(ECHO,))
    with pytest.raises(OSError):
        await brokers.start()
    assert commands.ENDPOINT_ENV not in os.environ
    await brokers.stop()
