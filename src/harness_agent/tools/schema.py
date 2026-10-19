"""JSON Schema của tool, sinh từ args_model của registry, đã khai triển `$ref` inline.

Pydantic đặt enum vào `$defs` và tham chiếu bằng `$ref`. Không phải client/model nào cũng xử lý
`$ref`, và schema lồng vào schema khác thì `$ref` trỏ sai gốc. Vì vậy khai triển hết ở đây.

Schema này chỉ là GỢI Ý cho phía không tin cậy. Phía tin cậy luôn validate lại bằng args_model
(extra="forbid") trong gate.
"""

from __future__ import annotations

from typing import Any

from .registry.tools import TOOL_REGISTRY

_DEFS_PREFIX = "#/$defs/"


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Trả về bản sao của schema, thay mọi `$ref` cục bộ bằng định nghĩa, bỏ `$defs`."""
    defs = schema.get("$defs", {})

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [walk(x, stack) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if ref is None:
            return {k: walk(v, stack) for k, v in node.items() if k != "$defs"}
        if not isinstance(ref, str) or not ref.startswith(_DEFS_PREFIX):
            raise ValueError(f"$ref không hỗ trợ: {ref!r}")
        key = ref.removeprefix(_DEFS_PREFIX)
        if key not in defs:
            raise ValueError(f"$ref trỏ tới định nghĩa không tồn tại: {ref!r}")
        if key in stack:
            raise ValueError(f"$ref đệ quy: {ref!r}")
        siblings = {k: walk(v, stack) for k, v in node.items() if k != "$ref"}
        return {**walk(defs[key], (*stack, key)), **siblings}

    return walk(schema, ())


def tool_input_schema(tool: str) -> dict[str, Any]:
    """Input schema của một tool trong registry (KeyError nếu tool lạ)."""
    return inline_refs(TOOL_REGISTRY[tool].args_model.model_json_schema())


def tool_call_schema() -> dict[str, Any]:
    """Schema của một ToolCall `{tool, args}`: một nhánh `oneOf` cho mỗi tool."""
    return {
        "title": "ToolCall",
        "oneOf": [
            {
                "type": "object",
                "title": name,
                "properties": {"tool": {"const": name}, "args": tool_input_schema(name)},
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
            for name in TOOL_REGISTRY
        ],
    }
