"""Tool call có kiểu — từ vựng chung của hệ thống (D-002, D-009).

Nguồn DUY NHẤT về tool và tham số là tool registry (`harness_agent.tools.registry`):
- Context-Building Agent mô tả thay đổi bằng danh sách `ToolCall` (trong ChangeRequest).
- Tool-Selection Agent phát ra tool call cùng dạng `{tool, args}`.
- Registry ánh xạ mỗi `tool` sang args_model, min_role, blast radius và một Jinja2 template.
- Corpus dùng `ToolCall` làm nhãn kỳ vọng.

Một `ToolCall` chỉ tạo được khi `tool` có trong registry và `args` qua được args_model của tool đó
(extra="forbid", pattern cấm ký tự điều khiển). Không có bước validate thứ hai để quên.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator

from harness_agent.tools.registry.tools import TOOL_REGISTRY

from .common import StrictModel

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_REGISTRY)

# Loại lỗi (`err["type"]`) của pydantic-core mà `msg` được dựng hoàn toàn từ schema (pattern, giới
# hạn, danh sách giá trị cho phép, độ dài), KHÔNG chứa giá trị đầu vào. Chỉ các loại này giữ
# nguyên `msg` khi báo lỗi tham số. Mọi loại khác dùng thông báo cố định, đặc biệt `value_error` /
# `assertion_error`: `msg` của chúng là chuỗi do validator viết tay tự dựng, và vài validator của
# registry chép nguyên giá trị vào (thư viện `ipaddress`: "Expected 4 octets in '<giá trị>'";
# `KeyRef`: "key_ref không hợp lệ: '<giá trị>'"). Loại lạ, kể cả loại do phiên bản pydantic sau
# thêm vào, mặc định bị che (fail-closed). Tham số do LLM sinh: có thể chứa secret, và thông báo
# lỗi đi vào trace, rejects.jsonl và câu trả lời gửi lại model.
_ERROR_TYPES_WITHOUT_INPUT = frozenset(
    {
        "missing",  # Field required
        "extra_forbidden",  # Extra inputs are not permitted
        "string_type",
        "int_type",
        "int_parsing",  # ... unable to parse string as an integer (không chép chuỗi)
        "int_from_float",
        "float_type",
        "float_parsing",
        "bool_type",
        "bool_parsing",
        "list_type",
        "dict_type",
        "model_type",  # tên class, không phải giá trị
        "model_attributes_type",
        "none_required",
        "string_pattern_mismatch",  # pattern lấy từ schema
        "string_too_short",
        "string_too_long",
        "too_short",  # số phần tử, không phải nội dung
        "too_long",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "enum",  # danh sách giá trị cho phép lấy từ schema
        "literal_error",
    }
)
REDACTED_ERROR_MSG = "giá trị không hợp lệ"


def safe_error_message(err: dict[str, Any]) -> str:
    """`msg` của một lỗi pydantic nếu loại lỗi đó không chép đầu vào; ngược lại một thông báo cố định."""
    return str(err.get("msg")) if err.get("type") in _ERROR_TYPES_WITHOUT_INPUT else REDACTED_ERROR_MSG


class ToolCall(StrictModel):
    tool: str
    # Tham số ở dạng JSON đã chuẩn hoá bởi args_model của registry (kiểu đúng, bỏ trường None),
    # để hai tool call cùng nghĩa thì bằng nhau khi so sánh (gate G5).
    args: dict[str, Any]

    @field_validator("args")
    @classmethod
    def _validate_by_registry(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        tool = info.data.get("tool")  # không có nếu trường `tool` đã lỗi
        spec = TOOL_REGISTRY.get(tool) if isinstance(tool, str) else None
        if spec is None:
            raise ValueError(f"tool không có trong registry: {tool!r}")
        try:
            parsed = spec.args_model.model_validate(value)
        except ValidationError as exc:
            # include_input=False: phần `input` không cần ở đây. `msg` đi qua allowlist ở trên, vì `msg` của
            # validator viết tay có thể chứa giá trị.
            err = exc.errors(include_input=False)[0]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            kind = err.get("type")
            # `from None`: không nối lỗi gốc của registry (lỗi đó KHÔNG bật hide_input_in_errors)
            # vào traceback.
            raise ValueError(
                f"tham số của {tool} không hợp lệ ({loc}) [{kind}]: {safe_error_message(err)}"
            ) from None
        return parsed.model_dump(mode="json", exclude_none=True)

    @property
    def device(self) -> str:
        return self.args["device"]

    @property
    def parsed(self) -> BaseModel:
        """Tham số ở dạng args_model của registry (validate lại từ dữ liệu đã lưu)."""
        return TOOL_REGISTRY[self.tool].args_model.model_validate(self.args)


def parse_tool_call(data: Any) -> ToolCall:
    """Parse `{tool, args}` (ví dụ tool call từ LLM) thành ToolCall. Lỗi thì raise ValidationError."""
    return ToolCall.model_validate(data)
