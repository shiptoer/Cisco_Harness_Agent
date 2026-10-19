r"""Hai lưới an toàn cho contract (v0.2).

1. Lỗi validate của `ToolCall` / `ChangeRequest` không chép giá trị đầu vào. Tham số tool call do LLM
   sinh, có thể chứa secret của context, và thông báo lỗi đi vào trace (`submit_invalid`) và câu trả lời
   gửi lại model. Dùng một chuỗi đánh dấu, KHÔNG dùng secret thật.
2. Không file nào trong `src/`, `tests/`, `docs/design/` chứa ký tự THẬT trong C1 (`\x80`-`\x9f`) hay
   LINE/PARAGRAPH SEPARATOR (`\u2028`, `\u2029`): chỉ được viết dạng escape. Công cụ sửa file đã một lần
   tự đổi chuỗi escape thành ký tự thật; ký tự đó vô hình khi đọc code, và `str.splitlines()` tách dòng ở đó.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from registry_samples import VALID_ARGS

from harness_agent.models import ChangeRequest, ToolCall
from harness_agent.models.operations import REDACTED_ERROR_MSG, safe_error_message
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
MARKER = "MARKER-x7q"


def _bad_args(tool: str) -> list[tuple[str, dict]]:
    """Các bộ tham số SAI cho `tool`, mỗi bộ có MARKER ở đúng chỗ gây lỗi: một trường lạ, và từng trường chuỗi
    bị thay bằng MARKER kèm xuống dòng (không pattern nào của registry nhận)."""
    valid = VALID_ARGS[tool]
    out = [("extra-field", {**valid, "junk": MARKER})]
    out += [(f"{k}", {**valid, k: MARKER + "\n"}) for k, v in valid.items() if isinstance(v, str)]
    return out


CASES = [(tool, how, args) for tool in TOOL_REGISTRY for how, args in _bad_args(tool)]


@pytest.mark.parametrize(("tool", "how", "args"), CASES, ids=[f"{t}-{h}" for t, h, _ in CASES])
def test_tool_call_errors_do_not_echo_the_arguments(tool: str, how: str, args: dict):
    with pytest.raises(ValidationError) as exc:
        ToolCall.model_validate({"tool": tool, "args": args})
    assert MARKER not in str(exc.value)
    assert MARKER not in repr(exc.value)


@pytest.mark.parametrize("kind", ["value_error", "assertion_error", "a_type_added_by_a_future_pydantic"])
def test_error_types_outside_the_allowlist_are_redacted(kind: str):
    """Đối chứng âm (i): loại lỗi ngoài allowlist có `msg` chứa đầu vào thì bị thay bằng thông báo cố định."""
    err = {"type": kind, "loc": ("ip",), "msg": f"Value error, Expected 4 octets in {MARKER!r}"}
    assert safe_error_message(err) == REDACTED_ERROR_MSG
    assert (
        safe_error_message({"type": "missing", "loc": ("ip",), "msg": "Field required"}) == "Field required"
    )


def test_old_formatting_that_copied_msg_leaks_on_the_same_input():
    """Đối chứng âm (ii): cách định dạng trước đây (chép nguyên `msg` của registry) làm lộ MARKER
    trên đúng đầu vào mà test ở trên dùng; tức test ở trên đỏ nếu `ToolCall` quay lại cách cũ."""
    spec = TOOL_REGISTRY["set_interface_ip"]
    with pytest.raises(ValidationError) as exc:
        spec.args_model.model_validate({**VALID_ARGS["set_interface_ip"], "ip": MARKER + "\n"})
    err = exc.value.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    old_message = f"tham số của set_interface_ip không hợp lệ ({loc}): {err.get('msg')}"
    assert MARKER in old_message
    with pytest.raises(ValidationError) as new:
        ToolCall.model_validate(
            {"tool": "set_interface_ip", "args": {**VALID_ARGS["set_interface_ip"], "ip": MARKER}}
        )
    assert MARKER not in str(new.value)


def test_every_tool_has_a_bad_argument_case():
    assert {tool for tool, _, _ in CASES} == set(TOOL_REGISTRY)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "ready",
            "changes": [
                {"tool": "create_vlan", "args": {**VALID_ARGS["create_vlan"], "name": MARKER + "\n"}}
            ],
        },
        {
            "status": "ready",
            "changes": [{"tool": "create_vlan", "args": {**VALID_ARGS["create_vlan"], "junk": MARKER}}],
        },
        {
            "status": "ready",
            "changes": [{"tool": "create_vlan", "args": VALID_ARGS["create_vlan"]}],
            "rationale": MARKER * 200,
        },
        {"status": "needs_clarification", "clarification_question": MARKER * 100},
        {"status": "ready", "changes": [], "junk": MARKER},
    ],
    ids=["bad-arg", "extra-arg", "rationale-too-long", "question-too-long", "extra-field"],
)
def test_change_request_errors_do_not_echo_the_input(payload: dict):
    with pytest.raises(ValidationError) as exc:
        ChangeRequest.model_validate(payload)
    assert MARKER not in str(exc.value)
    assert MARKER not in repr(exc.value)


def _is_raw_control(c: str) -> bool:
    return 0x80 <= ord(c) <= 0x9F or ord(c) in (0x2028, 0x2029)


def test_no_raw_c1_or_line_separator_characters_in_source_tests_or_design_docs():
    hits: list[str] = []
    for top in ("src", "tests", "docs/design"):
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # file nhị phân: ngoài phạm vi phép kiểm này
            for number, line in enumerate(text.split("\n"), 1):
                found = sorted({f"U+{ord(c):04X}" for c in line if _is_raw_control(c)})
                if found:
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {', '.join(found)}")
    assert hits == [], "viết dạng escape (vd \\u2028), không chèn ký tự thật:\n" + "\n".join(hits)


def test_raw_character_check_negative_control(tmp_path: Path):
    """Đối chứng âm: hàm nhận diện bắt đúng các ký tự đó, và không bắt chữ tiếng Việt hay dạng escape."""
    raw = "a" + chr(0x2028) + "b" + chr(0x85) + "c" + chr(0x9F)
    assert [c for c in raw if _is_raw_control(c)] == [chr(0x2028), chr(0x85), chr(0x9F)]
    assert not any(_is_raw_control(c) for c in "Cổng tầng 3 – phòng họp \\u2028 \\x85")
