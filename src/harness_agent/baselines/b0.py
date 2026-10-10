"""Baseline B0 (task 1.3): LLM sinh CLI trực tiếp từ intent + context, không tool, không harness.

Chạy qua CÙNG runtime dsh với B1–B4 (D-008, F2): patch tắt mọi tool và cả khối MCP client, nên model
chỉ trả lời bằng text. Ở B0 text của model CHÍNH LÀ change package (đây là điểm khác biệt được đo),
nên — chỉ ở mức này — runner phân tích text để tách cấu hình từng thiết bị. Quy tắc "không bao giờ phân
tích text của model" (F11/F12) áp dụng cho B1–B4.

Định dạng text B0 yêu cầu trong `prompts/b0.md`:
    === <hostname> ===            # bắt đầu khối cấu hình một thiết bị
    <lệnh IOS, mỗi dòng một lệnh>
    end
    CLARIFY: <câu hỏi>            # hoặc: cả output chỉ một dòng này
    REJECT: <lý do>               # hoặc: cả output chỉ một dòng này
Text không theo định dạng → không có khối thiết bị nào → kết cục AGENT_INCOMPLETE (model không ra cấu hình).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HEADER = re.compile(r"^\s*===\s*(?P<host>[A-Za-z][A-Za-z0-9-]{0,62})\s*===\s*$")
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*$")


@dataclass
class B0Output:
    kind: str  # "configs" | "clarify" | "reject" | "empty"
    configs: dict[str, str] = field(default_factory=dict)
    message: str = ""


def build_prompt(template: str, intent: str, context_path: Path) -> str:
    """Prompt B0 = template + inventory THÔ (toàn bộ JSON, kể cả trường bí mật — đúng với "không harness")."""
    raw = json.loads(context_path.read_text(encoding="utf-8-sig"))
    return template.replace("{{CONTEXT}}", json.dumps(raw, ensure_ascii=False, indent=2)).replace(
        "{{INTENT}}", intent
    )


def parse_output(text: str) -> B0Output:
    """Tách text của model thành cấu hình theo thiết bị. Tất định; không sửa nội dung lệnh."""
    stripped = text.strip()
    if not stripped:
        return B0Output("empty")
    first = stripped.split("\n", 1)[0].strip()
    if first.upper().startswith("CLARIFY:"):
        return B0Output("clarify", message=first[len("CLARIFY:") :].strip())
    if first.upper().startswith("REJECT:"):
        return B0Output("reject", message=first[len("REJECT:") :].strip())

    configs: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.split("\n"):
        if _FENCE.match(line):
            continue  # model lỡ bọc markdown: bỏ dòng fence, giữ nội dung
        m = _HEADER.match(line)
        if m:
            current = m["host"]
            configs.setdefault(current, [])
            continue
        if current is not None and line.strip():
            configs[current].append(line.rstrip())
    out: dict[str, str] = {}
    for host, lines in configs.items():
        if not lines:
            continue
        if lines[-1].strip() != "end":
            lines.append("end")
        out[host] = "\n".join(lines) + "\n"
    if not out:
        return B0Output("empty", message=stripped[:200])
    return B0Output("configs", configs=out)


def as_dict(o: B0Output) -> dict[str, Any]:
    return {"kind": o.kind, "devices": sorted(o.configs), "message": o.message}
