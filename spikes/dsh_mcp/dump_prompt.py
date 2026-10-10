"""Print, verbatim, what dsh sent to the model in one session: system prompt(s), the injected
runtime-context message, the request config (provider/model/reasoningEffort), the offered tools,
and the model id each assistant message reports.

Usage: dump_prompt.py <session-id>   (reads $DSH_HOME/sessions, default spikes/dsh_mcp/.dsh_home)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from check_tools import find_session_file, read_session_events

HERE = Path(__file__).resolve().parent


def texts(message: dict) -> str:
    return "\n".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("DSH_HOME", str(HERE / ".dsh_home"))
    path = find_session_file(sys.argv[1])
    events = read_session_events(path)
    print(f"# Session {sys.argv[1]}\n# Source: {path}\n")
    for e in events:
        data = e.get("data", {})
        if e["type"] == "system/message":
            print(f"## system/message (seq {e['seq']}) — verbatim system prompt")
            print("-----BEGIN-----")
            print(texts(data["message"]))
            print("-----END-----\n")
        elif e["type"] == "user/message" and data.get("source", {}).get("kind") != "user":
            kind = data["source"]["kind"]
            print(f"## user/message (seq {e['seq']}, source={kind}) — injected by dsh, not typed by the user")
            print("-----BEGIN-----")
            print(texts(data))
            print("-----END-----\n")
        elif e["type"] == "request/header":
            header = data["header"]
            print(f"## request/header (seq {e['seq']}, reason={data.get('reason')})")
            print(f"config:         {json.dumps(header.get('config'))}")
            print(f"adapterDefaults:{json.dumps(header.get('adapterDefaults'))}")
            print(f"tools:          {[t['name'] for t in header.get('tools', [])]}\n")
        elif e["type"] == "request/context":
            print(f"## request/context (seq {e['seq']})\n{json.dumps(data)}\n")
        elif e["type"] == "assistant/message":
            src = data["message"]["source"]
            response = src.get("replayState", {}).get("response", {})
            print(
                f"## assistant/message (seq {e['seq']}, step {data.get('step')}): "
                f"provider={src.get('provider')} model={src.get('model')} "
                f"response.model={response.get('model')} usage={json.dumps(data.get('usage'))}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
