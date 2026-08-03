"""Tests for the shim a sandboxed backend runs to ask about a tool call."""

from __future__ import annotations

import json

import pytest

from claude_on_the_fly import cotf_approve


@pytest.fixture
def answered(monkeypatch):
    """Point the shim at a fake service and record what it was sent."""
    sent: list[dict] = []
    reply: dict = {"behavior": "allow", "message": "approved"}

    def fake_ask(payload):
        sent.append(payload)
        return reply.get("behavior") == "allow", str(reply.get("message") or "")

    monkeypatch.setattr(cotf_approve, "_ask", fake_ask)
    return sent, reply


# --- codex hook mode ---


def test_hook_says_nothing_when_allowed(answered):
    """Silence is how a hook tells codex it has no opinion, which lets the command
    run. Printing an "allow" object would be codex-invalid noise."""
    assert (
        cotf_approve.run_hook('{"tool_name":"Bash","tool_input":{"command":"ls"}}')
        == ""
    )


def test_hook_blocks_with_a_reason_when_denied(answered):
    """codex rejects a block whose reason is empty, so the reason can never be
    blank however the denial arose."""
    _sent, reply = answered
    reply["behavior"] = "deny"
    reply["message"] = "operator declined"
    answer = json.loads(cotf_approve.run_hook('{"tool_name":"Bash","tool_input":{}}'))
    assert answer == {"decision": "block", "reason": "operator declined"}


def test_hook_forwards_the_payload_the_event_carried(answered):
    sent, _reply = answered
    cotf_approve.run_hook(
        json.dumps(
            {
                "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch"},
                "tool_use_id": "call_9",
            }
        )
    )
    assert sent == [
        {
            "source": "cotf",
            "tool_name": "apply_patch",
            "input": {"command": "*** Begin Patch"},
            "tool_use_id": "call_9",
        }
    ]


@pytest.mark.parametrize("stdin_text", ["", "not json", "[]", '{"tool_input": 3}'])
def test_a_malformed_event_still_asks_rather_than_assuming(answered, stdin_text):
    """A hook that crashed or printed nothing would let the command run, so every
    unreadable input has to become an ordinary question."""
    sent, _reply = answered
    cotf_approve.run_hook(stdin_text)
    assert len(sent) == 1
    assert sent[0]["input"] == {}


# --- claude mcp mode ---


def _call_tool(**arguments) -> dict:
    response = cotf_approve.handle_mcp(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "approve", "arguments": arguments},
        }
    )
    assert response is not None
    return json.loads(response["result"]["content"][0]["text"])


def test_mcp_handshake_advertises_the_one_tool():
    init = cotf_approve.handle_mcp(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    )
    assert init is not None
    assert init["result"]["capabilities"] == {"tools": {}}
    listed = cotf_approve.handle_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert listed is not None
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == [cotf_approve.TOOL_NAME]


def test_mcp_echoes_updated_input_byte_identical(answered):
    """Returning anything else here would let the daemon silently rewrite the call
    the operator was shown and agreed to."""
    original = {"file_path": "/tmp/x", "content": "hello\nworld"}
    assert _call_tool(tool_name="Write", input=original)["updatedInput"] == original


def test_mcp_denial_carries_the_message(answered):
    _sent, reply = answered
    reply["behavior"] = "deny"
    reply["message"] = "no thanks"
    assert _call_tool(tool_name="Bash", input={}) == {
        "behavior": "deny",
        "message": "no thanks",
    }


def test_mcp_marks_the_question_as_claudes_own(answered):
    """The service filters cotf-sourced calls and forwards claude-sourced ones
    untouched, so the source field decides whether anything is filtered at all."""
    sent, _reply = answered
    _call_tool(tool_name="Bash", input={"command": "ls"})
    assert sent[0]["source"] == "claude"


def test_a_notification_gets_no_response():
    assert (
        cotf_approve.handle_mcp({"jsonrpc": "2.0", "method": "notifications/x"}) is None
    )


def test_an_unknown_method_is_an_error_not_a_silent_allow():
    response = cotf_approve.handle_mcp({"jsonrpc": "2.0", "id": 5, "method": "nope"})
    assert response is not None
    assert response["error"]["code"] == -32601


def test_a_non_object_input_is_normalised_rather_than_forwarded(answered):
    sent, _reply = answered
    assert _call_tool(tool_name="Bash", input="not an object")["behavior"] == "allow"
    assert sent[0]["input"] == {}


# --- failing closed ---


def test_no_endpoint_denies(monkeypatch):
    """The shim is only on PATH because approvals are on, so a missing endpoint
    means the wiring broke, not that everything is permitted."""
    monkeypatch.delenv(cotf_approve.ENDPOINT_ENV, raising=False)
    allowed, message = cotf_approve._ask({})
    assert not allowed
    assert cotf_approve.ENDPOINT_ENV in message


def test_an_unreachable_daemon_denies(monkeypatch):
    monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:1/decide")
    allowed, message = cotf_approve._ask({})
    assert not allowed
    assert "could not reach the operator" in message


def test_an_unreadable_answer_denies(monkeypatch):
    class FakeResponse:
        def read(self):
            return b'"a bare string"'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:9/decide")
    monkeypatch.setattr(
        cotf_approve.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
    )
    allowed, message = cotf_approve._ask({})
    assert not allowed
    assert "unreadable" in message


def test_a_denial_with_no_message_still_gets_one(monkeypatch):
    class FakeResponse:
        def read(self):
            return b'{"behavior": "deny"}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:9/decide")
    monkeypatch.setattr(
        cotf_approve.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
    )
    allowed, message = cotf_approve._ask({})
    assert not allowed
    assert message == cotf_approve.DENY_MESSAGE


def test_transport_timeout_follows_the_brokers_answer_window(monkeypatch):
    class FakeResponse:
        def read(self):
            return b'{"behavior": "deny"}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    seen: list[float] = []

    def fake_urlopen(_request, timeout):
        seen.append(timeout)
        return FakeResponse()

    monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:9/decide")
    monkeypatch.setenv(cotf_approve.REQUEST_TIMEOUT_ENV, "1005")
    monkeypatch.setattr(cotf_approve.urllib.request, "urlopen", fake_urlopen)
    cotf_approve._ask({})
    assert seen == [1005.0]


# --- entry point ---


@pytest.mark.parametrize("mode", ["", "wat"])
def test_an_unknown_mode_exits_nonzero(mode, capsys):
    assert cotf_approve.main([mode] if mode else []) == 2
    assert "usage" in capsys.readouterr().err


def test_hook_mode_writes_its_answer_to_stdout(answered, monkeypatch, capsys):
    _sent, reply = answered
    reply["behavior"] = "deny"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"tool_name":"Bash"}'))
    assert cotf_approve.main(["hook"]) == 0
    assert "block" in capsys.readouterr().out


def test_hook_mode_prints_nothing_when_allowed(answered, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"tool_name":"Bash"}'))
    assert cotf_approve.main(["hook"]) == 0
    assert capsys.readouterr().out == ""


def test_mcp_mode_serves_until_stdin_closes(answered, monkeypatch):
    import io

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}),
        "",
        "not json",
        json.dumps(["not an object"]),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]
    out = io.StringIO()
    cotf_approve.run_mcp(io.StringIO("\n".join(lines)), out)
    written = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [message["id"] for message in written] == [0]


def test_mcp_mode_is_reachable_from_the_entry_point(monkeypatch, capsys):
    """claude spawns this as `cotf-approve mcp`, so the dispatch to the stdio loop
    is on the live path and not just an internal helper."""
    import io

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})),
    )
    assert cotf_approve.main(["mcp"]) == 0
    assert '"id": 7' in capsys.readouterr().out


# --- failing closed structurally, not case by case ---


CORPUS = [
    "",
    "not json",
    "[]",
    "null",
    "3",
    '"a string"',
    "{}",
    '{"tool_input": 3}',
    '{"tool_name": null, "tool_input": null}',
    '{"tool_name": {"nested": "object"}}',
    '{"tool_input": [1, 2, 3]}',
    '{"tool_use_id": {"not": "a string"}}',
    "\x00\x01binary",
    '{"tool_name": "Bash", "tool_input": {"command": "\\ud800"}}',
]


@pytest.mark.parametrize("stdin_text", CORPUS)
def test_no_input_can_make_the_hook_permit_by_crashing(stdin_text, monkeypatch):
    """The invariant, asserted directly rather than one bug at a time. codex treats a
    crashed hook exactly like a hook with no opinion and runs the command, so any
    input that raises is an input that grants. Two such paths were found by hand
    before the wrapper existed."""
    monkeypatch.setattr(cotf_approve, "_ask", lambda _payload: (False, "declined"))
    answer = cotf_approve.run_hook(stdin_text)
    # Either a valid block, or silence -- never a traceback, and never silence when
    # the service said no.
    assert answer, f"{stdin_text!r} produced silence, which codex reads as allowed"
    assert json.loads(answer)["decision"] == "block"


def test_an_unexpected_failure_inside_the_hook_still_blocks(monkeypatch):
    """The wrapper is deliberately untyped: narrowing it would mean deciding in
    advance which bugs are safe to fail open on."""

    def explode(_payload):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(cotf_approve, "_ask", explode)
    answer = json.loads(cotf_approve.run_hook('{"tool_name":"Bash"}'))
    assert answer["decision"] == "block"
    assert "RuntimeError" in answer["reason"]
    assert answer["reason"], "codex rejects a block with an empty reason"


def test_an_unexpected_failure_in_the_mcp_path_denies(monkeypatch):
    def explode(_payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(cotf_approve, "_ask", explode)
    body = _call_tool(tool_name="Bash", input={"command": "ls"})
    assert body["behavior"] == "deny"
    assert "RuntimeError" in body["message"]


# --- pty notify mode ---


def test_notify_forwards_only_a_permission_prompt(monkeypatch):
    """The same hook event also fires for idle and task-complete notifications.
    Relaying those would send the daemon hunting for a dialog nobody drew."""
    posted: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout=None):
        posted.append(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setenv(cotf_approve.NOTIFY_ENV, "http://127.0.0.1:9/notify")
    monkeypatch.setattr(cotf_approve.urllib.request, "urlopen", fake_urlopen)

    assert (
        cotf_approve.run_notify(
            json.dumps(
                {
                    "notification_type": "permission_prompt",
                    "session_id": "s1",
                    "transcript_path": "/tmp/t.jsonl",
                }
            )
        )
        == ""
    )
    assert posted == [{"session_id": "s1", "transcript_path": "/tmp/t.jsonl"}]

    posted.clear()
    for other in ("idle_prompt", "task_complete", None):
        cotf_approve.run_notify(json.dumps({"notification_type": other}))
    assert posted == []


@pytest.mark.parametrize(
    "stdin_text", ["", "not json", "[]", '{"notification_type":"permission_prompt"}']
)
def test_notify_never_fails_the_hook(monkeypatch, stdin_text):
    """A Notification hook cannot approve or refuse anything -- claude is blocked on
    the dialog it drew, not on this -- so there is no outcome a non-zero exit could
    improve, and crashing would just add noise to the pane."""
    monkeypatch.setenv(cotf_approve.NOTIFY_ENV, "http://127.0.0.1:1/notify")
    assert cotf_approve.run_notify(stdin_text) == ""


def test_notify_is_a_no_op_without_an_endpoint(monkeypatch):
    monkeypatch.delenv(cotf_approve.NOTIFY_ENV, raising=False)
    assert cotf_approve.run_notify('{"notification_type":"permission_prompt"}') == ""


def test_notify_mode_is_reachable_from_the_entry_point(monkeypatch, capsys):
    import io

    monkeypatch.delenv(cotf_approve.NOTIFY_ENV, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"notification_type":"idle_prompt"}'))
    assert cotf_approve.main(["notify"]) == 0
    assert capsys.readouterr().out == ""


class TestRequestTimeoutIsAlwaysUsable:
    """The daemon publishes this, but the shim runs inside the sandbox where the
    value is just an environment string. A junk one must not make the request
    non-blocking, which would deny every call instantly."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not-a-number", id="unparseable"),
            pytest.param("", id="empty"),
            pytest.param("0", id="zero"),
            pytest.param("-5", id="negative"),
        ],
    )
    def test_an_unusable_value_falls_back_to_the_default(self, monkeypatch, raw):
        seen: dict = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b'{"behavior": "allow"}'

        def fake_urlopen(_request, timeout):
            seen["timeout"] = timeout
            return _Response()

        monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:1/decide")
        monkeypatch.setenv(cotf_approve.REQUEST_TIMEOUT_ENV, raw)
        monkeypatch.setattr(cotf_approve.urllib.request, "urlopen", fake_urlopen)

        allowed, _message = cotf_approve._ask({"tool_name": "Bash"})

        assert allowed is True
        assert seen["timeout"] == cotf_approve.REQUEST_TIMEOUT_SECONDS

    def test_a_sane_value_is_honoured(self, monkeypatch):
        seen: dict = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b'{"behavior": "allow"}'

        monkeypatch.setenv(cotf_approve.ENDPOINT_ENV, "http://127.0.0.1:1/decide")
        monkeypatch.setenv(cotf_approve.REQUEST_TIMEOUT_ENV, "42")
        monkeypatch.setattr(
            cotf_approve.urllib.request,
            "urlopen",
            lambda _r, timeout: seen.__setitem__("timeout", timeout) or _Response(),
        )
        cotf_approve._ask({"tool_name": "Bash"})
        assert seen["timeout"] == 42
