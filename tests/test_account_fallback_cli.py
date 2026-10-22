"""Simulate Claude provider failures through the real subprocess and resume path."""

import json
import os
import sys
from uuid import uuid4

import pytest
import yaml

from claude_on_the_fly import (
    account_fallback,
    accounts,
    agent,
    envfile,
    sandbox,
    settings,
)


@pytest.fixture
def simulated_cli(tmp_path, monkeypatch, account_sandbox_mode):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "claude"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import json
import os
from pathlib import Path
import sys

root=Path(os.environ['COTF_SIMULATION_DIR'])
mode=(root/'mode').read_text()
arguments=sys.argv[1:]
prompt=sys.stdin.read()
account=Path(os.environ['CLAUDE_CONFIG_DIR']).name
if os.environ.get('COTF_SIMULATION_JAIL') == '1':
    other = 'second' if account == 'first' else 'first'
    protected = [root / name / '.credentials.json' for name in ('first', 'second')]
    protected.append(root / other / 'settings.json')
    for secret in protected:
        try:
            secret.read_text()
        except OSError:
            continue
        raise AssertionError('Jail exposed a protected profile file')
session=arguments[arguments.index('--resume')+1] if '--resume' in arguments else arguments[arguments.index('--session-id')+1]
workspace=Path.cwd().resolve()
slug=str(workspace).replace('/','-').replace('.','-').replace('_','-')
transcript=Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects'/slug/(session+'.jsonl')
transcript.parent.mkdir(parents=True,exist_ok=True)
with (root/'attempts.jsonl').open('a') as handle:
    handle.write(json.dumps({'account':account,'prompt':prompt,'resume':'--resume' in arguments,'session':session})+'\n')

def emit(record):
    with transcript.open('a') as handle:
        handle.write(json.dumps(record)+'\n')
    print(json.dumps(record),flush=True)

emit({'type':'user','message':{'role':'user','content':prompt}})
if account=='first' or mode=='all-exhausted':
    if mode in ('partial','pending'):
        emit({'type':'assistant','message':{'role':'assistant','content':[{'type':'tool_use','id':'write-once','name':'Bash','input':{'command':'write marker'}}]}})
        counter=root/'side-effects'
        counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
        if mode=='partial':
            emit({'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'write-once','content':'Marker written once.'}]}})
    message='429 Too Many Requests' if mode=='transient' else 'Credit balance is too low.'
    emit({'type':'result','subtype':'error_during_execution','is_error':True,'result':message,'session_id':session})
    sys.exit(1)
if mode=='partial':
    assert '--resume' in arguments
    assert 'Continue the interrupted turn' in prompt
    assert 'perform the original write' not in prompt
    assert (root/'side-effects').read_text()=='1'
emit({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'Simulation complete.'}]}})
emit({'type':'result','subtype':'success','is_error':False,'result':'Simulation complete.','session_id':session,'total_cost_usd':0,'duration_ms':1})
"""
    )
    executable.chmod(0o700)
    config = tmp_path / "config.yaml"
    document = {
        "accounts": {
            name: {
                "backend": "claude",
                "home": str(tmp_path / name),
                "model": "simulation-model",
            }
            for name in ("first", "second")
        },
        "routing": {"defaults": {"claude": ["first", "second"]}},
        "sandbox": {"mode": account_sandbox_mode},
    }
    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        (home / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "simulation-only"}})
        )
        (home / "settings.json").write_text("{}")
    config.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(settings, "operator_settings", lambda: config)
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {})
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "daemon")
    monkeypatch.setattr(
        envfile,
        "daemon_environment",
        lambda: {"CLAUDE_CONFIG_DIR": str(tmp_path / "base")},
    )
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.defpath)
    monkeypatch.setenv("COTF_SIMULATION_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BACKEND", "claude")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = sandbox.session_env(
        {
            "COTF_SIMULATION_DIR": str(tmp_path),
            "COTF_SIMULATION_JAIL": "1" if account_sandbox_mode == "jail" else "0",
        }
    )
    try:
        yield tmp_path, workspace, document, config
    finally:
        sandbox.reset_session_env(token)


async def call_simulation(workspace, session):
    return await agent.run(
        workspace,
        session,
        "perform the original write",
        "slack",
        timeout=15,
        nudge_prompt="perform the original write again",
    )


@pytest.mark.parametrize("mode", ["before-work", "partial"])
async def test_real_cli_fallback_preserves_resume_and_writes_once(simulated_cli, mode):
    root, workspace, _, _ = simulated_cli
    (root / "mode").write_text(mode)
    session = str(uuid4())
    response = await call_simulation(workspace, session)
    assert response.body == "Simulation complete."
    attempts = [
        json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()
    ]
    assert [item["account"] for item in attempts] == ["first", "second"]
    assert attempts[1]["resume"]
    assert attempts[0]["session"] == attempts[1]["session"] == session
    saved = accounts.saved_selection("slack", session, "claude")
    assert saved.account.name == "second"
    assert saved.pool == ("first", "second")
    if mode == "partial":
        assert (root / "side-effects").read_text() == "1"
    (root / "mode").write_text("before-work")
    await call_simulation(workspace, session)
    attempts = [
        json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()
    ]
    assert [item["account"] for item in attempts] == ["first", "second", "second"]
    assert accounts.current() is None


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("all-exhausted", account_fallback.AccountFallbackError),
        ("pending", account_fallback.AccountFallbackError),
        ("transient", RuntimeError),
    ],
)
async def test_real_cli_stops_on_unsafe_or_nonquota_failures(
    simulated_cli, mode, expected
):
    root, workspace, _, _ = simulated_cli
    (root / "mode").write_text(mode)
    with pytest.raises(expected):
        await call_simulation(workspace, str(uuid4()))
    attempts = [
        json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()
    ]
    assert len(attempts) == (2 if mode == "all-exhausted" else 1)
    if mode == "pending":
        assert (root / "side-effects").read_text() == "1"


@pytest.mark.parametrize("edited_route", [["first"], "first"])
async def test_resumed_default_pool_keeps_saved_list(simulated_cli, edited_route):
    root, workspace, document, config = simulated_cli
    (root / "mode").write_text("before-work")
    session = str(uuid4())
    await call_simulation(workspace, session)
    document["routing"]["defaults"]["claude"] = edited_route
    config.write_text(yaml.safe_dump(document))
    response = await call_simulation(workspace, session)
    assert response.body == "Simulation complete."
    attempts = [
        json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()
    ]
    assert [attempt["account"] for attempt in attempts] == ["first", "second", "second"]
    assert accounts.saved_selection("slack", session, "claude").pool == (
        "first",
        "second",
    )
