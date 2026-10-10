import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from harness_agent.harness.roles import role_level
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import run_case

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
print(
    "[web-pipeline] DEEPSEEK_API_KEY present:",
    bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
    file=sys.stderr,
    flush=True,
)

# Cấu hình cố định cho bản thử nghiệm local.
# Model không được truyền hoặc thay đổi role.
ROLE = os.environ.get("NETWORK_ROLE", "access_operator")

role_level(ROLE)
CONTEXT = Path(os.environ.get("NETWORK_CONTEXT", str(ROOT / "data/contexts/campus-small-01.json")))
if not CONTEXT.is_absolute():
    CONTEXT = ROOT / CONTEXT
CONTEXT = CONTEXT.resolve(strict=True)
CONFIG = Path(os.environ.get("NETWORK_CONFIG", str(ROOT / "config/local.toml")))
if not CONFIG.is_absolute():
    CONFIG = ROOT / CONFIG
CONFIG = CONFIG.resolve(strict=True)

# Operator selects this path before starting the server.
# It is never taken from model tool arguments.
_contract_env = os.environ.get("NETWORK_CONFIRMED_INTENT", "").strip()

if not _contract_env:
    raise RuntimeError(
        "Chưa chọn contract. Hãy đặt NETWORK_CONFIRMED_INTENT trong PowerShell trước khi khởi động web."
    )

CONFIRMED_INTENT = Path(_contract_env)
if not CONFIRMED_INTENT.is_absolute():
    CONFIRMED_INTENT = ROOT / CONFIRMED_INTENT

CONFIRMED_INTENT = CONFIRMED_INTENT.resolve(strict=True)
if not CONFIRMED_INTENT.is_file():
    raise RuntimeError("NETWORK_CONFIRMED_INTENT phải trỏ tới một file.")

server = Server("network_pipeline")
lock = asyncio.Lock()


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="run_network_request",
            description=(
                "Run the project's CBA/TSA pipeline against a "
                "confirmation contract selected locally by the operator. "
                "Pass the user's network request faithfully as intent. "
                "Requests that do not match the contract are blocked. "
                "Returns the actual verdict and review package. "
                "Does not deploy configuration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 12000,
                    }
                },
                "required": ["intent"],
                "additionalProperties": False,
            },
        )
    ]


def execute(intent):
    cfg = LocalConfig.load(CONFIG)
    run_id = "web-" + uuid4().hex
    result = run_case(
        cfg,
        CONTEXT,
        intent,
        ROLE,
        ROOT / "runs",
        run_id,
        confirmed_intent_path=CONFIRMED_INTENT,
    )
    payload = asdict(result)

    # Chỉ trả package khi pipeline chấp nhận và không báo rò rỉ.
    package = ROOT / "runs" / run_id / "change_package.md"
    if (
        result.status == "ok"
        and result.verdict == "accept"
        and not result.leak_in_package
        and package.is_file()
    ):
        payload["change_package"] = package.read_text(encoding="utf-8")

    return payload


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    if name != "run_network_request":
        raise ValueError("Unknown tool")

    if not isinstance(arguments, dict) or set(arguments) != {"intent"}:
        raise ValueError("Only the intent argument is allowed")

    intent = arguments["intent"]
    if not isinstance(intent, str) or not 1 <= len(intent.strip()) <= 12000:
        raise ValueError("Invalid intent")

    async with lock:
        result = await asyncio.to_thread(execute, intent)

    return [
        types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False),
        )
    ]


async def main():
    async with stdio_server() as (reader, writer):
        await server.run(
            reader,
            writer,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
