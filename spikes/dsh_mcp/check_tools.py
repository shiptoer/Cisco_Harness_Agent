"""Assert that the dsh agent sees exactly the tools of MCP server "hello".

Expected set: `mcp__hello__<name>` for every tool hello_server.py lists over stdio.

Sources (pick one):
  ndjson  <file>   headless --json output. The session id it reports is used to open the
                   session log under $DSH_HOME/sessions; every `request/header` event there
                   carries the exact tool list sent to the model (authoritative). Every
                   `tool_call` in the NDJSON must also be an expected tool.
  session <file>   a session.v*.jsonl[.zstd] file directly (same check, no tool_call check).
  config  <file>   `--dump-config` output. Static check: every row from a tool-providing
                   package must be disabled, and every mcp-client row must be server "hello".
                   Heuristic only: it cannot see tools a non-tool plugin registers.

Exit 0 when the sets match; otherwise print the extra/missing tools and exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import anyio
import zstandard
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER_NAME = "hello"
PREFIX = f"mcp__{SERVER_NAME}__"

# Packages whose rows register model-facing tools (docs/tool-catalog.md, dsh 0.2.0-rc.1).
TOOL_PACKAGES = {
    "@deepseek-ai/dsh-plugin-manager/tools",
    "@deepseek-ai/dsh-mcp-resources",
    "@deepseek-ai/dsh-plan-mode",
    "@deepseek-ai/dsh-schedule",
}
TOOL_PACKAGE_RE = re.compile(r"^@deepseek-ai/dsh-(experimental-)?tool-")
# Match TOOL_PACKAGE_RE by name but register no tool (a timeout policy on the tool chain).
NON_TOOL_PACKAGES = {"@deepseek-ai/dsh-tool-call-timeout-policy"}


async def _server_tools() -> set[str]:
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "hello_server.py")])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return {PREFIX + t.name for t in (await session.list_tools()).tools}


def expected_tools() -> set[str]:
    return anyio.run(_server_tools)


def read_session_events(path: Path) -> list[dict]:
    raw = path.read_bytes()
    if path.suffix == ".zstd":
        with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
            raw = reader.read()
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def find_session_file(session_id: str) -> Path:
    home = Path(os.environ.get("DSH_HOME", HERE / ".dsh_home"))
    hits = sorted((home / "sessions").glob(f"*/{session_id}/session*.jsonl*"))
    if not hits:
        sys.exit(f"no session log for {session_id} under {home / 'sessions'}")
    return hits[-1]


def seen_tools_from_session(path: Path) -> set[str]:
    headers = [e for e in read_session_events(path) if e.get("type") == "request/header"]
    if not headers:
        sys.exit(f"{path}: no request/header event (did the run reach the model request?)")
    seen: set[str] = set()
    for event in headers:
        seen |= {t["name"] for t in event["data"]["header"].get("tools", [])}
    print(f"session log: {path}")
    print(f"request/header events: {len(headers)}")
    return seen


def called_tools_from_ndjson(path: Path) -> tuple[str, list[str]]:
    session_id, called = None, []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "session":
            session_id = event["sessionId"]
        elif event.get("type") == "tool_call":
            called.append(event.get("name") or event.get("toolName") or event.get("tool") or "?")
    if session_id is None:
        sys.exit(f"{path}: no session event")
    return session_id, called


def enabled_tool_rows_from_config(path: Path) -> set[str]:
    """Return a pseudo tool name per enabled tool-providing row."""
    rows, cur = [], None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if m := re.match(r"^- id: (\S+)", line):
            cur = {"id": m.group(1), "name": "", "disabled": "", "server": ""}
            rows.append(cur)
        elif cur and line.startswith("  name:"):
            cur["name"] = line.split(":", 1)[1].strip().strip("'\"")
        elif cur and line.startswith("  disabled:"):
            cur["disabled"] = line.split(":", 1)[1].strip()
        elif cur and (m := re.match(r"^    serverName: (\S+)", line)):
            cur["server"] = m.group(1).strip("'\"")
    seen: set[str] = set()
    for row in rows:
        if row["disabled"] == "true":
            continue
        if row["name"] == "@deepseek-ai/dsh-mcp-client":
            seen.add(f"mcp__{row['server']}__*")
        elif row["name"] in NON_TOOL_PACKAGES:
            continue
        elif row["name"] in TOOL_PACKAGES or TOOL_PACKAGE_RE.match(row["name"]):
            state = f" (disabled: {row['disabled']})" if row["disabled"] else ""
            seen.add(f"row:{row['id']} [{row['name']}]{state}")
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", choices=["ndjson", "session", "config"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    expected = expected_tools()
    print(f"expected (from hello_server.py): {sorted(expected)}")
    called: list[str] = []

    if args.source == "config":
        seen = enabled_tool_rows_from_config(args.path)
        expected = {PREFIX + "*"}
    elif args.source == "session":
        seen = seen_tools_from_session(args.path)
    else:
        session_id, called = called_tools_from_ndjson(args.path)
        print(f"ndjson session: {session_id}")
        seen = seen_tools_from_session(find_session_file(session_id))

    print(f"seen by agent:  {sorted(seen)}")
    extra, missing = sorted(seen - expected), sorted(expected - seen)
    bad_calls = [c for c in called if c not in expected]
    if args.source == "ndjson":
        print(f"tool_call events: {called}")

    ok = not extra and not missing and not bad_calls
    for label, items in (("EXTRA", extra), ("MISSING", missing), ("UNEXPECTED CALL", bad_calls)):
        for item in items:
            print(f"{label}: {item}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
