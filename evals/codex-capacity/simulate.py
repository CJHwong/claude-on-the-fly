"""Run a real Codex process against a controlled local provider."""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from aiohttp import web

run_root = Path(tempfile.mkdtemp(prefix="cotf-capacity-live-"))
os.environ["COTF_DATA_DIR"] = str(run_root / "daemon")
os.environ["CODEX_HOME"] = str(run_root / ".codex")
os.environ.pop("CLAUDE_CONFIG_DIR", None)

# Module paths must bind to the isolated stores before the backend import.
from claude_on_the_fly.backends import codex  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")
requests = []


async def respond(request):
    body = await request.json()
    requests.append(body)
    number = len(requests)
    print(f"PROVIDER request={number} model={body.get('model')}", flush=True)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    if number == 1:
        marker = run_root / "action-count.txt"
        command = f"printf 'executed\\n' >> {marker}"
        tool = {
            "type": "function_call",
            "id": "tool-item-1",
            "call_id": "call_simulation_1",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": command, "max_output_tokens": 100}),
        }
        events = [
            {
                "type": "response.created",
                "response": {"id": "sim-tool", "status": "in_progress", "output": []},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**tool, "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "tool-item-1",
                "delta": tool["arguments"],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": tool},
            {
                "type": "response.completed",
                "response": {
                    "id": "sim-tool",
                    "status": "completed",
                    "output": [tool],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            },
        ]
    elif number <= 3:
        events = [
            {
                "type": "response.failed",
                "response": {
                    "id": f"sim-{number}",
                    "status": "failed",
                    "error": {
                        "code": "server_overloaded",
                        "message": "Selected model is at capacity. Please try a different model.",
                    },
                },
            }
        ]
    else:
        message = {
            "id": "msg-success",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "CAPACITY_RETRY_OK", "annotations": []}
            ],
        }
        events = [
            {
                "type": "response.created",
                "response": {
                    "id": "sim-success",
                    "status": "in_progress",
                    "output": [],
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**message, "status": "in_progress", "content": []},
            },
            {
                "type": "response.content_part.added",
                "item_id": "msg-success",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg-success",
                "output_index": 0,
                "content_index": 0,
                "delta": "CAPACITY_RETRY_OK",
            },
            {
                "type": "response.output_text.done",
                "item_id": "msg-success",
                "output_index": 0,
                "content_index": 0,
                "text": "CAPACITY_RETRY_OK",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {
                "type": "response.completed",
                "response": {
                    "id": "sim-success",
                    "status": "completed",
                    "output": [message],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            },
        ]
    for event in events:
        await response.write(
            (
                "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
            ).encode()
        )
    await response.write_eof()
    return response


async def main():
    application = web.Application()
    application.router.add_post("/responses", respond)
    runner = web.AppRunner(application)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    codex_home = Path(os.environ["CODEX_HOME"])
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(f"""model = "capacity-simulation"
model_provider = "simulation"
[model_providers.simulation]
name = "Capacity simulation"
base_url = "http://127.0.0.1:{port}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
supports_websockets = false
""")
    workspace = run_root / "workspace"
    workspace.mkdir()
    try:
        backend = codex.CodexBackend(model="capacity-simulation")
        response = await backend.run(
            workspace,
            "live-simulation",
            "Reply CAPACITY_RETRY_OK. Do not use tools.",
            "cli",
            timeout=180,
        )
        result = {
            "body": response.body,
            "thread_id": backend._thread_id(workspace, "live-simulation"),
            "tool_counts": response.tool_counts,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "duration": response.duration,
        }
        print("RESULT", json.dumps(result), flush=True)
        assert result["body"] == "CAPACITY_RETRY_OK"
        assert len(requests) == 4
        assert (run_root / "action-count.txt").read_text().splitlines() == ["executed"]
        assert result["tool_counts"] == {"CommandExecution": 1}
        print(
            "PASS real Codex recovered after two capacity failures; completed tool action ran once",
            flush=True,
        )
    finally:
        await runner.cleanup()
        print("ARTIFACTS", run_root, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
