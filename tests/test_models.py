"""Test cho contract (task C0 / 2.1).

Tiêu chí nghiệm thu 2.1: 3 mẫu hợp lệ pass, 3 mẫu sai bị reject đúng lý do.
Các test còn lại khoá các thuộc tính bảo mật của contract để không ai vô tình nới lỏng.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, StringConstraints, TypeAdapter, ValidationError

from harness_agent.models import (
    TOOL_NAMES,
    ChangeRequest,
    Interface,
    NetworkContext,
    TestCase,
    ToolCall,
    parse_tool_call,
)
from harness_agent.models.common import FreeText

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

MODEL_BY_PREFIX: dict[str, type[BaseModel]] = {
    "network_context": NetworkContext,
    "change_request": ChangeRequest,
    "testcase": TestCase,
}


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    # Khoá bắt đầu bằng "_" là ghi chú cho người đọc, không thuộc contract.
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _model_for(path: Path) -> type[BaseModel]:
    for prefix, model in MODEL_BY_PREFIX.items():
        if path.stem.startswith(prefix):
            return model
    raise AssertionError(f"không biết model cho {path.name}")


VALID = sorted((EXAMPLES / "valid").glob("*.json"))
INVALID = sorted((EXAMPLES / "invalid").glob("*.json"))


def test_example_sets_not_empty():
    assert len(VALID) >= 3 and len(INVALID) >= 3


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_examples_pass(path: Path):
    _model_for(path).model_validate(_load(path))


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_examples_rejected(path: Path):
    with pytest.raises(ValidationError):
        _model_for(path).model_validate(_load(path))


# ---------- Thuộc tính bảo mật của contract ----------


def _vlan(**args) -> dict:
    return {"tool": "create_vlan", "args": {"device": "SW1", "vlan_id": 30, "name": "X", **args}}


def test_unknown_field_rejected():
    """LLM tự thêm trường lạ thì bị reject, không bị bỏ qua âm thầm (cả trong args lẫn ngoài)."""
    parse_tool_call(_vlan())  # đối chứng: bản không có trường lạ thì hợp lệ
    with pytest.raises(ValidationError):
        parse_tool_call(_vlan(privilege=15))
    with pytest.raises(ValidationError):
        parse_tool_call({**_vlan(), "privilege": 15})


def test_unknown_tool_rejected():
    with pytest.raises(ValidationError):
        parse_tool_call({"tool": "run_cli", "args": {"device": "SW1", "command": "write erase"}})


@pytest.mark.parametrize("bad", [{"tool": "create_vlan"}, {"tool": "create_vlan", "args": {}}, {"args": {}}])
def test_tool_call_cannot_skip_registry_validation(bad: dict):
    """Không có đường nào tạo ToolCall mà args chưa qua args_model của registry."""
    with pytest.raises(ValidationError):
        parse_tool_call(bad)


def test_tool_names_come_from_registry():
    assert len(TOOL_NAMES) == 11 and "apply_acl" in TOOL_NAMES


@pytest.mark.parametrize("vlan_id", [0, 4095, 5000])
def test_vlan_outside_ios_range_rejected(vlan_id: int):
    """Schema chặn VLAN ngoài 1-4094. Dải cấu hình được 2-1001 (D-004) do gate G4 kiểm tra:
    xem test_gate_vlan_range trong tests/test_harness_pipeline.py."""
    with pytest.raises(ValidationError):
        parse_tool_call(_vlan(vlan_id=vlan_id))


def test_args_are_normalised_so_equal_calls_compare_equal():
    """G5 normalizes valid values; numeric strings are invalid under A10a."""
    with pytest.raises(ValidationError):
        parse_tool_call(_vlan(vlan_id="30"))
    a = parse_tool_call(_vlan(vlan_id=30))
    assert a.args["vlan_id"] == 30 and a == parse_tool_call(_vlan())
    trunk = {"device": "SW1", "interface": "GigabitEthernet0/1", "native_vlan": 99}
    t1 = parse_tool_call({"tool": "set_trunk", "args": {**trunk, "allowed_vlans": [20, 10, 20]}})
    t2 = parse_tool_call({"tool": "set_trunk", "args": {**trunk, "allowed_vlans": [10, 20]}})
    assert t1 == t2 and t1.args["allowed_vlans"] == [10, 20]
    route = {"device": "R1", "prefix": "10.20.0.0/16", "next_hop": "10.0.12.2"}
    r = parse_tool_call({"tool": "add_static_route", "args": {**route, "exit_interface": None}})
    assert r.args == route and r.device == "R1"
    assert parse_tool_call(r.model_dump(mode="json")) == r  # round trip


@pytest.mark.parametrize(
    "bad",
    [
        "Guest\nusername evil privilege 15 secret x",  # chèn lệnh qua xuống dòng
        "Guest\r\nend",
        "tab\there",
    ],
)
def test_description_rejects_control_characters(bad: str):
    with pytest.raises(ValidationError):
        ToolCall(
            tool="set_interface_description",
            args={"device": "SW1", "interface": "GigabitEthernet0/5", "text": bad},
        )


# D-003, bổ sung 06/10/2026: ngoài C0 và DEL, cấm cả C1 (gồm NEL \x85) và LINE/PARAGRAPH SEPARATOR. Python
# `str.splitlines()` tách dòng ở cả ba ký tự \x85, \u2028, \u2029.
_C1_AND_SEPARATORS = {
    "NEL": "\x85",
    "C1-CSI": "\x9b",
    "C1-first": "\x80",
    "C1-last": "\x9f",
    "LS": "\u2028",
    "PS": "\u2029",
}
_FREE_TEXT_V01 = TypeAdapter(Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{0,240}$")])


def _description(text: str) -> Interface:
    return Interface(name="GigabitEthernet0/1", mode="access", access_vlan=10, description=text)


@pytest.mark.parametrize("where", ["start", "middle", "end"])
@pytest.mark.parametrize("char", list(_C1_AND_SEPARATORS.values()), ids=list(_C1_AND_SEPARATORS))
def test_free_text_rejects_c1_and_line_separators(char: str, where: str):
    text = {"start": char + "Guest", "middle": "Gu" + char + "est", "end": "Guest" + char}[where]
    if char in ("\x85", "\u2028", "\u2029") and where != "end":
        assert len(text.splitlines()) > 1  # vì sao ba ký tự này nguy hiểm: splitlines() tách dòng ở đó
    with pytest.raises(ValidationError):
        _description(text)
    # Đối chứng âm: pattern của v0.1 (chỉ cấm C0 và DEL) cho qua, tức test này bắt đúng phần đã thêm.
    assert _FREE_TEXT_V01.validate_python(text) == text


@pytest.mark.parametrize(
    "ok", ["Uplink to SW1 (Gi0/0), VLAN 10-20: HR/Sales", "Cổng tầng 3 – phòng họp", "a\u00a0b", ""]
)
def test_free_text_still_accepts_printable_text(ok: str):
    assert _description(ok).description == ok


def test_validation_errors_do_not_echo_the_input():
    """Thông báo lỗi không chép giá trị đầu vào: đầu vào có thể là secret; thông báo lỗi đi vào trace/log."""
    marker = "s3cr3t-marker-for-error-test"
    with pytest.raises(ValidationError) as exc:
        _description(marker + "\n")
    assert marker not in str(exc.value)
    # `.errors()` mặc định VẪN có `input`; code của repo chỉ đọc `loc`, `msg` (gate, server, operations).
    assert marker not in repr(exc.value.errors(include_input=False))

    # Đối chứng âm: cùng kiểu trường, model không bật hide_input_in_errors thì lỗi có chép giá trị.
    class Echoing(BaseModel):
        description: FreeText

    with pytest.raises(ValidationError) as echoed:
        Echoing(description=marker + "\n")
    assert marker in str(echoed.value)


@pytest.mark.parametrize("bad", ["GUEST NET", "GUEST;end", "", "A" * 33])
def test_vlan_name_restricted(bad: str):
    with pytest.raises(ValidationError):
        parse_tool_call(_vlan(name=bad))


@pytest.mark.parametrize("bad", ["Gi0/5", "gigabitethernet0/5", "GigabitEthernet0/5; shutdown"])
def test_interface_name_must_be_canonical(bad: str):
    with pytest.raises(ValidationError):
        Interface(name=bad, mode="access", access_vlan=10)


def test_models_are_frozen():
    op = parse_tool_call(_vlan())
    with pytest.raises(ValidationError):
        op.tool = "set_aaa_login"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        op.parsed.vlan_id = 1  # type: ignore[attr-defined]


# ---------- Ràng buộc cấu trúc ----------


def test_access_interface_requires_access_vlan():
    with pytest.raises(ValidationError):
        Interface(name="GigabitEthernet0/5", mode="access")


def test_routed_interface_cannot_have_access_vlan():
    with pytest.raises(ValidationError):
        Interface(name="GigabitEthernet0/0", mode="routed", access_vlan=10)


def test_change_request_clarify_has_no_changes():
    with pytest.raises(ValidationError):
        ChangeRequest(
            status="needs_clarification",
            clarification_question="Thiết bị nào?",
            changes=[parse_tool_call(_vlan())],
        )
    cr = ChangeRequest(status="needs_clarification", clarification_question="Thiết bị nào?")
    assert cr.target_devices == set()


def test_testcase_adversarial_requires_attack_label():
    data = _load(EXAMPLES / "valid" / "testcase.json")
    data["case_id"] = "adversarial-vlan-0001"
    data["subset"] = "adversarial"
    with pytest.raises(ValidationError):
        TestCase.model_validate(data)


def test_context_lookup_helpers():
    ctx = NetworkContext.model_validate(_load(EXAMPLES / "valid" / "network_context.json"))
    sw = ctx.get_device("SW-ACC-01")
    assert sw is not None and sw.get_interface("GigabitEthernet0/5") is not None
    assert ctx.get_device("DOES-NOT-EXIST") is None


def test_round_trip_json():
    """Serialize rồi parse lại phải ra object bằng nhau (cần cho trace và record/replay).

    Từ v0.2 (D-042), `secrets` là SecretStr: JSON ghi chuỗi sao thay cho giá trị, nên round trip qua
    JSON chỉ bằng nhau khi bỏ `secrets`; phần secret thì được kiểm là đã bị che, không phải giữ nguyên."""
    data = _load(EXAMPLES / "valid" / "network_context.json")
    ctx = NetworkContext.model_validate(data)
    assert NetworkContext.model_validate(ctx.model_dump()) == ctx  # dạng Python giữ SecretStr
    no_secrets = {**data, "devices": [{**d, "secrets": {}} for d in data["devices"]]}
    plain = NetworkContext.model_validate(no_secrets)
    assert NetworkContext.model_validate_json(plain.model_dump_json()) == plain
    masked = NetworkContext.model_validate_json(ctx.model_dump_json())
    assert masked.secret_values() == {"**********"} != ctx.secret_values()
