"""Simulate Codex exhaustion through real subprocesses and native rollouts."""

import json
import sys
from uuid import uuid4

import pytest
import yaml

from claude_on_the_fly import (
    account_fallback,
    accounts,
    agent,
    codex_auth,
    codex_state,
    envfile,
    settings,
)


@pytest.fixture
def simulated_codex(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "codex"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

root = Path(os.environ['COTF_SIMULATION_DIR'])
mode = (root / 'mode').read_text()
home = Path(os.environ['CODEX_HOME'])
account = json.loads((home / 'auth.json').read_text())['simulation_account']
arguments = sys.argv[1:]
resuming = 'resume' in arguments
thread = arguments[arguments.index('resume') + 1] if resuming else str(uuid4())
prompt = arguments[-1]
rollout = home / 'sessions' / ('rollout-2026-10-23-' + thread + '.jsonl')
with (root / 'attempts.jsonl').open('a') as handle:
    handle.write(json.dumps({'account': account, 'prompt': prompt,
        'resume': resuming, 'thread': thread, 'home': str(home)}) + '\n')

def emit(kind, payload):
    with rollout.open('a') as handle:
        handle.write(json.dumps({'type': kind, 'payload': payload}) + '\n')

if not resuming:
    emit('session_meta', {'session_id': thread, 'id': thread, 'cwd': str(Path.cwd())})
else:
    assert rollout.exists(), 'Resume must use the existing native rollout'
emit('response_item', {'type': 'message', 'role': 'user',
    'content': [{'type': 'input_text', 'text': prompt}]})
if account == 'first' or mode == 'all-exhausted':
    if mode in ('partial', 'pending'):
        emit('response_item', {'type': 'function_call', 'call_id': 'write-once',
            'name': 'exec_command', 'arguments': '{"cmd":"write marker"}'})
        counter = root / 'side-effects'
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
        if mode == 'partial':
            emit('response_item', {'type': 'function_call_output',
                'call_id': 'write-once', 'output': 'Marker written once.'})
    failure = {'code': 'rate_limit_exceeded', 'message': '429 Too Many Requests'} if mode == 'transient' else {
        'code': 'insufficient_quota', 'message': 'Account quota exhausted'}
    emit('event_msg', {'type': 'task_complete', 'error': failure})
    sys.exit(1)
if mode == 'partial':
    assert resuming
    assert 'Continue the interrupted turn' in prompt
    assert 'perform the original write' not in prompt
    assert (root / 'side-effects').read_text() == '1'
emit('response_item', {'type': 'message', 'role': 'assistant',
    'content': [{'type': 'output_text', 'text': 'Codex simulation complete.'}]})
emit('event_msg', {'type': 'task_complete', 'last_agent_message': 'Codex simulation complete.'})
"""
    )
    executable.chmod(0o700)
    registry = {}
    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"simulation_account": name}))
        registry[name] = {"backend": "codex", "home": str(home)}
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "accounts": registry,
                "routing": {"defaults": {"codex": ["first", "second"]}},
                "sandbox": {"mode": "off"},
            }
        )
    )
    monkeypatch.setattr(settings, "operator_settings", lambda: config)
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {})
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "daemon")
    monkeypatch.setattr(codex_state, "HOMES_DIR", tmp_path / "codex-homes")
    monkeypatch.setattr(codex_state, "MAPPINGS_DIR", tmp_path / "codex-sessions")
    monkeypatch.setattr(codex_auth, "_account_urls", {})
    environment = {
        "CODEX_HOME": str(tmp_path / "base-codex"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "base-claude"),
    }
    monkeypatch.setattr(envfile, "daemon_environment", lambda: environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("COTF_SIMULATION_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_CHATGPT_VIA_BROKER", "false")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return tmp_path, workspace


async def call_simulation(workspace, session):
    return await agent.run(
        workspace,
        session,
        "perform the original write",
        "slack",
        timeout=15,
        nudge_prompt="perform the original write again",
    )


def attempts(root):
    return [
        json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()
    ]


@pytest.mark.parametrize("mode", ["before-work", "partial"])
async def test_codex_cli_switch_preserves_native_thread_and_completed_actions(
    simulated_codex, mode
):
    root, workspace = simulated_codex
    (root / "mode").write_text(mode)
    session = str(uuid4())
    response = await call_simulation(workspace, session)
    assert response.body == "Codex simulation complete."
    observed = attempts(root)
    assert [item["account"] for item in observed] == ["first", "second"]
    assert [item["resume"] for item in observed] == [False, True]
    assert observed[0]["thread"] == observed[1]["thread"]
    assert observed[0]["home"] != observed[1]["home"]
    assert codex_state.read_thread_id(workspace, session) == observed[0]["thread"]
    saved = accounts.saved_selection("slack", session, "codex")
    assert saved.account.name == "second"
    assert saved.pool == ("first", "second")
    if mode == "partial":
        assert (root / "side-effects").read_text() == "1"
    (root / "mode").write_text("before-work")
    await call_simulation(workspace, session)
    observed = attempts(root)
    assert [item["account"] for item in observed] == ["first", "second", "second"]
    assert observed[2]["resume"]
    assert len({item["thread"] for item in observed}) == 1
    assert accounts.current() is None


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("all-exhausted", account_fallback.AccountFallbackError),
        ("pending", account_fallback.AccountFallbackError),
        ("transient", RuntimeError),
    ],
)
async def test_codex_cli_stops_on_unsafe_or_nonquota_failures(
    simulated_codex, mode, expected
):
    root, workspace = simulated_codex
    (root / "mode").write_text(mode)
    session = str(uuid4())
    with pytest.raises(expected):
        await call_simulation(workspace, session)
    observed = attempts(root)
    assert [item["account"] for item in observed] == (
        ["first", "second"] if mode == "all-exhausted" else ["first"]
    )
    saved = accounts.saved_selection("slack", session, "codex")
    assert saved.account.name == "first"
    assert saved.pool == ("first", "second")
    if mode == "all-exhausted":
        assert observed[1]["resume"]
        assert observed[0]["thread"] == observed[1]["thread"]
    if mode == "pending":
        assert (root / "side-effects").read_text() == "1"
