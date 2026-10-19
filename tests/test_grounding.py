"""Intent grounding tất định (D-061, hiện thực D-051 / D-030): mọi giá trị thực thể trong ChangeRequest phải
neo được vào intent gốc (sau chuẩn hoá) hoặc suy ra duy nhất từ context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.grounding import GROUNDED_FIELDS, GroundingViolation, ground, normalize_interface
from harness_agent.models import TestCase, ToolCall
from harness_agent.runner.cases import CASES_DIR, load_case
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
CORPUS_V2 = ROOT / "data" / "corpus" / "v2"


@pytest.fixture(scope="module")
def store() -> ContextStore:
    return ContextStore.load(CAMPUS)


def call(tool: str, **args) -> ToolCall:
    return ToolCall.model_validate({"tool": tool, "args": args})


def fields(violations: list[GroundingViolation]) -> list[str]:
    return [v.field for v in violations]


# ------------------------------------------------------------------ đơn vị
def test_vlan_id_not_in_intent_is_a_violation(store):
    v = ground(
        "Gán GigabitEthernet1/2 trên SW2 vào VLAN 20",
        [call("set_access_vlan", device="SW2", interface="GigabitEthernet1/2", vlan_id=10)],
        store,
    )
    assert fields(v) == ["vlan_id"]
    assert v[0].index == 0 and v[0].value == "10"


def test_matching_values_have_no_violation(store):
    assert (
        ground(
            "Gán GigabitEthernet1/2 trên SW2 vào VLAN 20",
            [call("set_access_vlan", device="SW2", interface="GigabitEthernet1/2", vlan_id=20)],
            store,
        )
        == []
    )


def test_abbreviated_interface_in_intent_is_anchored(store):
    changes = [call("set_access_vlan", device="SW2", interface="GigabitEthernet1/2", vlan_id=20)]
    assert ground("Gán Gi1/2 trên SW2 vào VLAN 20", changes, store) == []
    assert ground("Gán gi 1/2 trên sw2 vào vlan20", changes, store) == []


def test_digits_inside_a_name_are_not_an_anchor(store):
    v = ground(
        "Tạo VLAN tên GUEST_0438 trên SW2",
        [call("create_vlan", device="SW2", vlan_id=438, name="GUEST_0438")],
        store,
    )
    assert fields(v) == ["vlan_id"]


def test_unknown_tool_is_fail_closed(store):
    # ToolCall không dựng được với tool lạ (registry chặn); model_construct mô phỏng một tool mới được thêm
    # vào registry mà quên bảng grounding.
    rogue = ToolCall.model_construct(tool="nonexistent", args={"device": "SW2"})
    v = ground("SW2", [rogue], store)
    assert len(v) == 1 and v[0].reason == "tool chưa có bảng grounding"


def test_grounded_fields_cover_every_registry_tool_with_real_arg_fields():
    assert set(GROUNDED_FIELDS) == set(TOOL_REGISTRY)
    for tool, names in GROUNDED_FIELDS.items():
        model_fields = set(TOOL_REGISTRY[tool].args_model.model_fields)
        assert names and set(names) <= model_fields, tool
        assert "device" in names, tool


def test_normalize_interface():
    assert normalize_interface("Gi0/1") == "GigabitEthernet0/1"
    assert normalize_interface("fa0/2") == "FastEthernet0/2"
    assert normalize_interface("Te1/0/1") == "TenGigabitEthernet1/0/1"
    assert normalize_interface("Lo0") == "Loopback0"
    assert normalize_interface("Vl20") == "Vlan20"
    assert normalize_interface("Po1") == "Port-channel1"
    assert normalize_interface("Eth1/1") == "Ethernet1/1"
    assert normalize_interface("gigabitethernet0/1") == "GigabitEthernet0/1"
    assert normalize_interface("GigabitEthernet0/1") == "GigabitEthernet0/1"


def test_device_is_inferred_only_when_one_device_owns_the_interface(store):
    # GigabitEthernet0/0 có trên cả R1, SW1, SW2: không suy được thiết bị.
    v = ground(
        "Tắt GigabitEthernet0/0",
        [call("set_interface_state", device="R1", interface="GigabitEthernet0/0", enabled=False)],
        store,
    )
    assert fields(v) == ["device"]
    # Vlan50 chỉ có trên SW1: suy duy nhất ra SW1; SW2 thì không neo được.
    intent = "Áp ACL SERVERS-PROTECT chiều in lên Vlan50"
    apply = {"interface": "Vlan50", "acl_name": "SERVERS-PROTECT", "direction": "in"}
    assert ground(intent, [call("apply_acl", device="SW1", **apply)], store) == []
    assert fields(ground(intent, [call("apply_acl", device="SW2", **apply)], store)) == ["device"]


def test_wrong_device_is_a_violation_even_if_interface_matches(store):
    v = ground(
        "Tắt cổng GigabitEthernet0/2 trên SW2",
        [call("set_interface_state", device="SW1", interface="GigabitEthernet0/2", enabled=False)],
        store,
    )
    assert fields(v) == ["device"]


def test_ip_cidr_and_ports_are_whole_token_anchors(store):
    acl = {"device": "SW1", "acl_name": "SERVERS-PROTECT", "action": "permit", "protocol": "tcp"}
    ok = call("add_acl_entry", **acl, seq=60, src="10.10.20.0/24", dst="host 10.10.50.20", dst_port="eq 22")
    intent = (
        "Thêm vào ACL SERVERS-PROTECT trên SW1 dòng 60: permit tcp từ 10.10.20.0/24 tới host 10.10.50.20 "
        "cổng 22."
    )
    assert ground(intent, [ok], store) == []
    bad = call("add_acl_entry", **acl, seq=61, src="10.10.20.0/25", dst="host 10.10.50.2", dst_port="eq 23")
    assert fields(ground(intent, [bad], store)) == ["seq", "src", "dst", "dst_port"]


def test_set_interface_ip_reads_ip_and_prefix_from_cidr_token(store):
    intent = "Assign IP 10.197.0.1/24 to R1 GigabitEthernet0/2"
    good = call(
        "set_interface_ip", device="R1", interface="GigabitEthernet0/2", ip="10.197.0.1", prefix_len=24
    )
    assert ground(intent, [good], store) == []
    bad = call(
        "set_interface_ip", device="R1", interface="GigabitEthernet0/2", ip="10.197.0.2", prefix_len=25
    )
    assert fields(ground(intent, [bad], store)) == ["ip", "prefix_len"]


def test_description_must_appear_as_a_token_sequence(store):
    intent = "Đặt description cho SW1 GigabitEthernet0/2 là 'Printer floor 2'"
    good = call(
        "set_interface_description", device="SW1", interface="GigabitEthernet0/2", text="Printer floor 2"
    )
    bad = call(
        "set_interface_description", device="SW1", interface="GigabitEthernet0/2", text="Printer floor 3"
    )
    assert ground(intent, [good], store) == []
    assert fields(ground(intent, [bad], store)) == ["text"]


def test_remove_route_next_hop_inferred_from_the_single_matching_route(store):
    # R1 có đúng một route tới 10.10.0.0/16 (qua 10.0.12.2): next-hop suy duy nhất từ context.
    intent = "Xoá static route 10.10.0.0/16 trên R1"
    assert (
        ground(
            intent,
            [call("remove_static_route", device="R1", prefix="10.10.0.0/16", next_hop="10.0.12.2")],
            store,
        )
        == []
    )
    v = ground(
        intent, [call("remove_static_route", device="R1", prefix="10.10.0.0/16", next_hop="10.0.12.9")], store
    )
    assert fields(v) == ["next_hop"]


def test_unchecked_fields_are_ignored(store):
    # `enabled`, `direction`, `methods`, `action`, `protocol`: loại thao tác, không phải thực thể (D-061).
    assert (
        ground(
            "Áp ACL SERVERS-PROTECT lên SW1 Vlan50",
            [
                call(
                    "apply_acl", device="SW1", interface="Vlan50", acl_name="SERVERS-PROTECT", direction="out"
                )
            ],
            store,
        )
        == []
    )


def test_violation_as_dict_round_trip():
    v = GroundingViolation(index=1, field="vlan_id", value="10", reason="không neo được vào intent")
    assert v.as_dict() == {
        "index": 1,
        "field": "vlan_id",
        "value": "10",
        "reason": "không neo được vào intent",
    }


# ------------------------------------------------------------------ dữ liệu thật
def _corpus_v2() -> list[tuple[str, str, TestCase, list[ToolCall]]]:
    manifest = json.loads((CORPUS_V2 / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for cid, meta in manifest["cases"].items():
        case = TestCase.model_validate_json((CORPUS_V2 / meta["file"]).read_text(encoding="utf-8"))
        scenario = json.loads((CORPUS_V2 / meta["fake_scenario"]).read_text(encoding="utf-8"))
        submits = [c for c in scenario.get("cba") or [] if c.get("tool") == "submit_change_request"]
        proposed = (
            [ToolCall.model_validate(c) for c in (submits[-1]["args"].get("changes") or [])]
            if submits
            else []
        )
        out.append((cid, meta["group"], case, proposed))
    return out


def _corpus_store(case: TestCase) -> ContextStore:
    return ContextStore.load(CORPUS_V2 / case.context_ref.path)


def test_all_clean_cases_of_corpus_v2_and_hand_cases_ground_with_zero_violations():
    checked = 0
    for cid, group, case, proposed in _corpus_v2():
        if group != "clean":
            continue
        store = _corpus_store(case)
        # Cả nhãn đúng lẫn thao tác kịch bản giả nộp (thứ thật sự tới bước grounding).
        for changes in (list(case.expected_tool_calls), proposed):
            assert ground(case.intent, changes, store) == [], (cid, case.intent, changes)
        checked += 1
    assert checked == 20
    hand = sorted(CASES_DIR.glob("clean-*.json"))
    assert len(hand) == 4
    for path in hand:
        case = load_case(path)
        store = ContextStore.load(ROOT / case.context)
        assert ground(case.intent, list(case.label.expected_tool_calls), store) == [], path.name
        checked += 1
    assert checked == 24


def test_drift_group_of_corpus_v2_has_violations():
    drift = [(cid, case, proposed) for cid, group, case, proposed in _corpus_v2() if group == "drift"]
    assert len(drift) == 5
    for cid, case, proposed in drift:
        store = _corpus_store(case)
        assert ground(case.intent, proposed, store), (cid, case.intent, proposed)
        # Nhãn đúng của chính case đó thì neo được: vi phạm là do lệch, không do bộ kiểm quá chặt.
        assert ground(case.intent, list(case.expected_tool_calls), store) == [], cid
