"""
nlcfg.registry.audit
======================
Trace JSONL append-only — đây LÀ công cụ đo lường cho toàn bộ thực nghiệm
ablation B0->B4 (không judge lại bằng LLM, chỉ đọc log này).
Một dòng JSON cho mỗi lần: stage một tool, hoặc submit_change_set.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

_TRACE_PATH = Path(os.environ.get("IBNA_AUDIT_LOG", "./audit_trace.jsonl"))


def append_trace(
    tool: str,
    args: dict[str, Any],
    verdict: str,
    op_id: str | None = None,
    reason: str | None = None,
    cli: str | None = None,
) -> None:
    record = {
        "ts": time.time(),
        "tool": tool,
        "args": _safe_args(args),
        "verdict": verdict,
        "op_id": op_id,
        "reason": reason,
        "cli": cli,
    }
    _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Không log giá trị secret — chỉ log rằng field đó có mặt.
    (key_ref không phải secret thật, nhưng phòng hờ nếu sau này thêm field khác.)"""
    redacted = {}
    for k, v in args.items():
        if "secret" in k.lower() or "password" in k.lower():
            redacted[k] = "<redacted>"
        else:
            redacted[k] = v
    return redacted
