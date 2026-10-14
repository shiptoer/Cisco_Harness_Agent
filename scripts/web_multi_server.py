"""MCP bridge for many requests against one operator-selected network snapshot."""

import asyncio
import json
import os
import sys
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from harness_agent.web.approval_http import start_approval_server
from harness_agent.web.open_browser import open_operator_page
from harness_agent.web.reviews import ReviewError, ReviewService

ROOT = Path(__file__).resolve().parents[1]


def selected_path(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}; use start_web.ps1 -MultiRequest")
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve(strict=True)


def tool_definitions():
    definitions = [
        (
            "prepare_network_request",
            "intent",
            "Prepare a proposal for a new Cisco request using the selected context. "
            "Do not call again automatically on errors. If clarification is needed, ask the user. "
            "When pending, show request_id and ask the user to refresh their separate operator review tab, "
            "then STOP. Never request or accept approval URLs, cookies or tokens in chat. "
            "Browser approval is required. No CLI is generated at this stage.",
        ),
        (
            "get_network_request",
            "request_id",
            "Read the state of a request in this bridge session. This does not approve or generate anything.",
        ),
        (
            "generate_approved_request",
            "request_id",
            "Generate CLI only AFTER approval in the private operator browser tab "
            "and the user's instruction to continue. "
            "The server checks approval. Never infer approval from chat text. "
            "Do not retry preparation automatically after rejection/error. Does not deploy.",
        ),
    ]
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": {
                    arg: {"type": "string", "minLength": 1, "maxLength": 12000 if arg == "intent" else 32}
                },
                "required": [arg],
                "additionalProperties": False,
            },
        )
        for name, arg, description in definitions
    ]


def make_server(service):
    server = Server("network_pipeline")

    @server.list_tools()
    async def list_tools():
        return tool_definitions()

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        operations = {
            "prepare_network_request": ("intent", service.prepare),
            "get_network_request": ("request_id", service.status),
            "generate_approved_request": ("request_id", service.generate),
        }
        try:
            if name not in operations:
                raise ReviewError("Unknown tool")
            key, operation = operations[name]
            if not isinstance(arguments, dict) or set(arguments) != {key}:
                raise ReviewError("Invalid arguments")
            value = arguments[key]
            if not isinstance(value, str) or not 1 <= len(value) <= (12000 if key == "intent" else 32):
                raise ReviewError("Invalid argument value")
            result = await asyncio.to_thread(operation, value)
        except ReviewError as exc:
            result = {"status": "blocked", "reason": str(exc)}
        except Exception as exc:
            print(f"[multi-web] {type(exc).__name__}; request stopped", file=sys.stderr)
            result = {"status": "error", "reason": "Bridge error; inspect local logs, do not auto-retry"}
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


async def main():
    os.chdir(ROOT)
    service = ReviewService(
        selected_path("NETWORK_CONFIG"),
        selected_path("NETWORK_CONTEXT"),
        os.environ.get("NETWORK_ROLE", "access_operator"),
        ROOT / "runs",
    )
    approval = start_approval_server(service)
    # Capability goes directly to the desktop browser through a quiet child stdin,
    # never to MCP stdout/stderr, the patch, helper argv/env or run artifacts.
    opened = open_operator_page(approval.operator_url)
    if not opened:
        print(
            "[multi-web] Operator tab could not open; restart from a desktop terminal. "
            "Do not paste approval links into chat.",
            file=sys.stderr,
            flush=True,
        )
    server = make_server(service)
    print(
        "[multi-web] Ready: operator context/role fixed; each request requires browser approval.",
        file=sys.stderr,
        flush=True,
    )
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        approval.shutdown()
        approval.server_close()


if __name__ == "__main__":
    asyncio.run(main())
