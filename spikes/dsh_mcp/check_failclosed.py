"""Judge the fail-closed run (run_failclosed.ps1): the cisco MCP server cannot start.

dsh verdict (the experiment): PASS only if dsh exits non-zero AND emits no successful final
(turn_end completed with a non-empty final text). Otherwise FAIL: dsh answered without its harness.

Runner detector (proposed fix, evaluated on the same run): the tools dsh offered the model, read
from the session log's `request/header` events, must include every expected mcp__cisco__* tool.
It is reported as DETECTED / MISSED; it does not change the dsh verdict.

Usage: check_failclosed.py <ndjson> <stderr.txt> <dsh exit code>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from check_tools import find_session_file, read_session_events

HERE = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"mcp__cisco__get_device"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ndjson, stderr, exit_code = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    os.environ.setdefault("DSH_HOME", str(HERE / ".dsh_home"))

    session_id, turn_end, final, calls = None, "?", "", []
    for line in ndjson.read_bytes().decode("utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "session":
            session_id = event["sessionId"]
        elif event.get("type") == "status" and event.get("phase") == "turn_end":
            turn_end = event["reason"]["kind"]
        elif event.get("type") == "final":
            final = event.get("text", "")
        elif event.get("type") == "tool_call":
            calls.append(event["tool"])
    successful_final = turn_end == "completed" and bool(final.strip())

    offered: set[str] | None = None
    if session_id:
        try:
            events = read_session_events(find_session_file(session_id))
            offered = {
                t["name"]
                for e in events
                if e.get("type") == "request/header"
                for t in e["data"]["header"].get("tools", [])
            }
        except SystemExit:
            offered = None
    inactive_warning = "did not activate" in stderr.read_bytes().decode("utf-8", errors="replace")

    print(f"session:            {session_id}")
    print(f"dsh exit code:      {exit_code}")
    print(f"turn_end:           {turn_end}")
    print(f"tool calls:         {calls or '(none)'}")
    print(f"successful final:   {successful_final}")
    print(f"final text:         {' '.join(final.split())[:300]}")
    print(f"stderr warning:     {'dsh: warning ... did not activate' if inactive_warning else '(none)'}")
    print(f"offered tools:      {sorted(offered) if offered is not None else 'session log not found'}")

    dsh_ok = exit_code != 0 and not successful_final
    detected = offered is None or not EXPECTED_TOOLS <= offered
    print(
        f"\nDSH FAIL-CLOSED:    {'PASS' if dsh_ok else 'FAIL'} (expected exit != 0 and no successful final)"
    )
    print(
        f"RUNNER DETECTOR:    {'DETECTED' if detected else 'MISSED'}"
        f" (request/header tools must include {sorted(EXPECTED_TOOLS)})"
    )
    return 0 if dsh_ok else 1


if __name__ == "__main__":
    sys.exit(main())
