"""The shim a sandboxed backend runs to ask cotf whether a tool call may proceed.

Two callers, two wire formats, one question. This exists because neither caller
can talk to the daemon in the daemon's own terms:

  `mcp`   claude spawns this as an MCP server and names it with
          --permission-prompt-tool. MCP is a JSON-RPC dialect over stdio, and the
          daemon has no reason to speak it.
  `hook`  codex runs this as a PreToolUse hook. A hook is a command that gets one
          JSON object on stdin and answers on stdout, and cannot speak MCP at all.

So the framing lives here, out at the edge, and both modes POST the same body to
the same endpoint. Stdio was chosen over letting the CLI reach the daemon over
HTTP directly because stdio MCP is the transport that was actually verified end to
end against claude; an HTTP MCP server was not.

**Fails closed, in both directions.** No endpoint, an unreachable daemon, a
timeout, malformed JSON back: every one of them denies. A shim that failed open
would turn any daemon hiccup into an ungated turn, which is precisely the state
the operator switched approvals on to avoid.

Runs inside the sandbox, so it holds no credential and knows nothing except the
loopback URL it was given.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Env the daemon sets when it spawns the backend. Absent means "not configured",
# which denies rather than allows.
ENDPOINT_ENV = "COTF_APPROVE_URL"

# Bounded because the caller is holding a turn open. The daemon runs its own,
# longer operator timeout and answers before this fires; this only catches a
# daemon that has stopped answering at all.
REQUEST_TIMEOUT_SECONDS = 600

DENY_MESSAGE = (
    "The operator did not approve this. Do not retry it. Say what you would need "
    "instead, or continue with the rest of the task."
)

TOOL_NAME = "approve"
TOOL_DESCRIPTION = "Ask the operator whether this tool call may proceed."


def _ask(payload: dict) -> tuple[bool, str]:
    """POST a decision request. Returns (allowed, message); every failure denies."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        # Denying is the only safe reading. This shim is only on PATH because the
        # operator turned approvals on, so a missing endpoint means the wiring
        # broke, not that everything is permitted.
        return False, f"approvals are enabled but {ENDPOINT_ENV} is not set"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            answer = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return False, f"could not reach the operator ({exc.__class__.__name__})"
    if not isinstance(answer, dict):
        return False, "the approval service returned something unreadable"
    allowed = answer.get("behavior") == "allow"
    return allowed, str(answer.get("message") or DENY_MESSAGE)


def _payload(tool_name: str, tool_input: dict, tool_use_id: str, source: str) -> dict:
    return {
        "source": source,
        "tool_name": tool_name,
        "input": tool_input,
        "tool_use_id": tool_use_id,
    }


def run_hook(stdin_text: str) -> str:
    """codex PreToolUse, wrapped so no escape from `_run_hook` can permit anything.

    codex treats a crashed hook exactly like a hook with no opinion: it runs the
    command. That makes an unhandled exception here a silent grant, so the default
    outcome of "something unexpected happened" has to be a block rather than a
    traceback. Two specific fail-open paths were found and fixed by hand before this
    existed; this closes the class instead of waiting for the third.

    The except is deliberately bare of any type filter. Narrowing it would mean
    deciding in advance which bugs are safe to fail open on, and there is no such
    set.
    """
    try:
        return _run_hook(stdin_text)
    except Exception as exc:
        return json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"the approval shim failed ({exc.__class__.__name__}), so this "
                    "call was not approved"
                ),
            }
        )


def _run_hook(stdin_text: str) -> str:
    """One event object in, one decision object out.

    Silence means "no opinion" to codex, which then runs the command. So an allow
    prints nothing and a denial prints a block with a reason -- codex rejects a
    block whose reason is empty, which is why DENY_MESSAGE is never blank.
    """
    try:
        parsed = json.loads(stdin_text or "{}")
    except ValueError:
        parsed = {}
    # Valid JSON that is not an object counts as malformed. Without this the
    # `.get` below raises, and codex treats a crashed hook as no opinion and runs
    # the command -- a fail-open path reachable from a single unexpected event
    # shape.
    event = parsed if isinstance(parsed, dict) else {}
    tool_input = event.get("tool_input")
    allowed, message = _ask(
        _payload(
            str(event.get("tool_name") or ""),
            tool_input if isinstance(tool_input, dict) else {},
            str(event.get("tool_use_id") or ""),
            "cotf",
        )
    )
    if allowed:
        return ""
    return json.dumps({"decision": "block", "reason": message})


def _mcp_result(request_id: object, body: dict) -> dict:
    """An MCP tool result. claude reads the decision out of the text content."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": json.dumps(body)}]},
    }


def handle_mcp(message: dict) -> dict | None:
    """One JSON-RPC message in, one response out. None for notifications."""
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cotf-approve", "version": "1"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "input": {"type": "object"},
                            },
                            "required": ["tool_name", "input"],
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        try:
            return _handle_tool_call(request_id, message)
        except Exception as exc:
            return _mcp_result(
                request_id,
                {
                    "behavior": "deny",
                    "message": (
                        f"the approval shim failed ({exc.__class__.__name__}), so "
                        "this call was not approved"
                    ),
                },
            )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"no method {method}"},
    }


def _handle_tool_call(request_id: object, message: dict) -> dict:
    arguments = (message.get("params") or {}).get("arguments") or {}
    tool_input = arguments.get("input")
    allowed, reason = _ask(
        _payload(
            str(arguments.get("tool_name") or ""),
            tool_input if isinstance(tool_input, dict) else {},
            str(arguments.get("tool_use_id") or ""),
            "claude",
        )
    )
    if allowed:
        # updatedInput echoes the original untouched. Returning anything else
        # would let the daemon silently rewrite the call the operator approved.
        return _mcp_result(
            request_id,
            {
                "behavior": "allow",
                "updatedInput": tool_input if isinstance(tool_input, dict) else {},
            },
        )
    return _mcp_result(request_id, {"behavior": "deny", "message": reason})


def run_mcp(stdin=None, stdout=None) -> None:
    """Serve MCP over stdio until the client closes it.

    The streams are resolved per call rather than defaulted at import, because
    binding them at def time captures whatever `sys.stdin` was when the module
    loaded and makes the loop impossible to point anywhere else.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle_mcp(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else ""
    if mode == "hook":
        answer = run_hook(sys.stdin.read())
        if answer:
            sys.stdout.write(answer + "\n")
        return 0
    if mode == "mcp":
        run_mcp()
        return 0
    sys.stderr.write("usage: cotf-approve {mcp|hook}\n")
    return 2


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
