"""Xuất JSON Schema của contract ra thư mục schemas/.

Dùng cho: (1) structured output / function calling của LLM, (2) tài liệu cho nhóm.
Chạy: python -m harness_agent.models.export_schema
"""

from __future__ import annotations

import json
from pathlib import Path

from harness_agent.tools.schema import tool_call_schema

from . import ChangeRequest, NetworkContext, TestCase

OUT = Path(__file__).resolve().parents[3] / "schemas"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    targets = {
        "network_context": NetworkContext.model_json_schema(),
        "change_request": ChangeRequest.model_json_schema(),
        "tool_call": tool_call_schema(),
        "test_case": TestCase.model_json_schema(),
    }
    for name, schema in targets.items():
        path = OUT / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent)}")


if __name__ == "__main__":
    main()
