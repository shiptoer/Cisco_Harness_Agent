"""Test cho gate (control iii) sau khi nối với tool registry (task 3.3).

Gate nhận tool call THÔ `{tool, args}`. Mỗi test bảo mật có đối chứng âm: cùng lời gọi đó được
cho qua khi điều kiện vi phạm bị gỡ, hoặc bị lọt khi bước kiểm tra bị gỡ.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from registry_samples import EXPECTED_TOOLS, VALID_ARGS, call

from harness_agent.harness import gate
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import STEP_NAMES, evaluate
from harness_agent.models import parse_tool_call

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
# Role cao nhất: với tới mọi tool, kể cả `apply_acl`, `set_aaa_login` và thay đổi chạm mạng quản trị (D-010).
ADMIN = "security_admin"


@pytest.fixture(scope="module")
def store() -> ContextStore:
    return ContextStore.load(CAMPUS)


def _decide(store, raw: dict, role: str = ADMIN, staged: list[dict] | None = None):
    """Chạy gate với change request đã duyệt chứa đúng lời gọi này (nếu nó hợp lệ)."""
    try:
        approved = [parse_tool_call(raw)]
    except Exception:  # noqa: BLE001 — lời gọi sai schema thì không có trong change request
        approved = []
    return evaluate(raw, role, store, approved, [parse_tool_call(s) for s in staged or []])


def _step(decision, name: str):
    return next(s for s in decision.steps if s.step == name)


# ------------------------------------------------------------ cấu trúc
def test_step_names_are_stable(store):
    """Tên bước nằm trong case (`stopped_by`) và trace: không được đổi."""
    assert STEP_NAMES == (
        "G1_whitelist",
        "G2_schema",
        "G3_rbac",
        "G4_blast_radius",
        "G5_intent_consistency",
        "G6_injection_scan",
    )
    d = _decide(store, call("create_vlan"))
    assert d.allowed and d.first_failed is None
    assert tuple(s.step for s in d.steps) == STEP_NAMES
    assert d.tool_call == parse_tool_call(call("create_vlan"))


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_top_role_passes_every_sample(store, tool: str):
    """Đối chứng cho mọi test chặn bên dưới: bộ tham số mẫu của cả 11 tool đều qua gate."""
    d = _decide(store, call(tool))
    assert d.allowed, f"{d.first_failed}: {d.reason}"


@pytest.mark.parametrize(
    ("role", "raw", "first_failed"),
    [
        (ADMIN, {"tool": "run_cli", "args": {"device": "R1"}}, "G1_whitelist"),
        (ADMIN, call("create_vlan", vlan_id=5000), "G2_schema"),
        ("access_operator", call("create_vlan"), "G3_rbac"),
        (
            ADMIN,
            call("set_interface_state", interface="GigabitEthernet0/0", enabled=False),
            "G4_blast_radius",
        ),
        (ADMIN, call("create_vlan", vlan_id=10, name="DUP"), "G4_blast_radius"),
        (ADMIN, call("set_access_vlan", interface="GigabitEthernet0/0", vlan_id=10), "G4_blast_radius"),
    ],
)
def test_gate_blocks(store, role, raw, first_failed):
    d = _decide(store, raw, role)
    assert not d.allowed and d.tool_call is None
    assert d.first_failed == first_failed
    assert len(d.steps) == 6  # shadow: đủ 6 bước dù đã fail sớm


def test_gate_intent_consistency(store):
    other = parse_tool_call(call("create_vlan", vlan_id=31, name="OTHER"))
    d = evaluate(call("create_vlan"), ADMIN, store, approved=[other], staged=[])
    assert d.first_failed == "G5_intent_consistency"


# ------------------------------------------------------------ G1, G2
@pytest.mark.parametrize(
    "raw", ["create_vlan", None, [], {}, {"tool": None}, {"tool": ["create_vlan"], "args": {}}]
)
def test_malformed_call_fails_closed(store, raw):
    d = evaluate(raw, ADMIN, store, approved=[], staged=[])
    assert not d.allowed and d.first_failed == "G1_whitelist"
    assert not any(s.ok for s in d.steps)


def test_extra_argument_is_rejected_not_dropped(store):
    """Trường lạ (LLM tự thêm "privilege": 15) phải bị G2 chặn, không được bỏ qua âm thầm."""
    d = _decide(store, call("create_vlan", privilege=15))
    assert d.first_failed == "G2_schema" and "privilege" in d.reason
    assert _decide(store, call("create_vlan")).allowed  # đối chứng: bỏ trường lạ thì qua


@pytest.mark.parametrize("args", [None, "x", ["device", "SW2"]])
def test_args_must_be_an_object(store, args):
    d = evaluate({"tool": "create_vlan", "args": args}, ADMIN, store, approved=[], staged=[])
    assert d.first_failed == "G2_schema"


@pytest.mark.parametrize("prefix", ["10.0.0.0", "10.0.0.0/255.0.0.0"])
def test_unrenderable_arguments_fail_g2(store, prefix: str):
    """args_model của registry nhận prefix không có "/len" nhưng template lỗi lúc render.
    Gate render thử ở G2 để lỗi này không xuất hiện sau khi đã cho qua."""
    d = _decide(store, call("add_static_route", prefix=prefix))
    assert d.first_failed == "G2_schema" and "prefix" in d.reason


# ------------------------------------------------------------ G3
@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_viewer_cannot_call_any_tool(store, tool: str):
    d = _decide(store, call(tool), role="viewer")
    assert d.first_failed == "G3_rbac"
    assert _step(d, "G1_whitelist").ok and _step(d, "G2_schema").ok


def test_viewer_negative_control_without_rbac(store, monkeypatch):
    """Đối chứng âm: gỡ bước G3 thì viewer gọi được tool, tức test trên bắt đúng control này."""
    monkeypatch.setattr(gate, "_rbac", lambda spec, tool, role: None)
    assert _decide(store, call("set_interface_state"), role="viewer").allowed


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [
        ("set_access_vlan", True),
        ("set_interface_state", True),
        ("set_interface_description", True),
        ("create_vlan", False),
        ("set_trunk", False),
        ("set_interface_ip", False),
        ("add_static_route", False),
        ("remove_static_route", False),
        ("add_acl_entry", False),
        ("apply_acl", False),
        ("set_aaa_login", False),
    ],
)
def test_access_operator_permissions(store, tool: str, allowed: bool):
    d = _decide(store, call(tool), role="access_operator")
    assert d.allowed is allowed
    assert d.first_failed == (None if allowed else "G3_rbac")


# ------------------------------------------------------------ G4
@pytest.mark.parametrize(
    ("vlan_id", "first_failed"),
    [(1, "G4_blast_radius"), (1002, "G4_blast_radius"), (1006, "G4_blast_radius"), (4094, "G4_blast_radius")],
)
def test_gate_vlan_range(store, vlan_id: int, first_failed: str):
    """D-004: VLAN ngoài 2–1001 qua được schema của registry (1–4094) nhưng bị G4 chặn."""
    for raw in (call("create_vlan", vlan_id=vlan_id), call("set_access_vlan", vlan_id=vlan_id)):
        d = _decide(store, raw)
        assert _step(d, "G2_schema").ok
        assert d.first_failed == first_failed


@pytest.mark.parametrize("field", ["native_vlan", "allowed_vlans"])
@pytest.mark.parametrize("vlan_id", [1, 1002, 2000])
def test_trunk_vlans_must_be_configurable_and_not_reserved(store, field: str, vlan_id: int):
    value = vlan_id if field == "native_vlan" else [10, vlan_id]
    d = _decide(store, call("set_trunk", **{field: value}))
    assert d.first_failed == "G4_blast_radius" and str(vlan_id) in d.reason


@pytest.mark.parametrize(
    "raw",
    [
        call("apply_acl", device="R1", interface="GigabitEthernet0/0", acl_name="EDGE-IN", direction="out"),
        call("set_trunk", interface="GigabitEthernet0/0"),
        call("set_interface_description", interface="GigabitEthernet0/0"),
        call("set_interface_ip", interface="GigabitEthernet0/1"),
    ],
)
def test_protected_interface_blocks_every_interface_tool(store, raw: dict):
    """Kiểm tra interface chạy trên blast radius của registry, nên tool mới cũng được bảo vệ."""
    d = _decide(store, raw)
    assert d.first_failed == "G4_blast_radius" and "bảo vệ" in d.reason


def test_unknown_device_and_interface(store):
    assert "không có trong context" in _decide(store, call("create_vlan", device="SW9")).reason
    d = _decide(store, call("set_interface_state", interface="GigabitEthernet9/9"))
    assert d.first_failed == "G4_blast_radius" and "không có GigabitEthernet9/9" in d.reason


def test_static_route_exit_interface_must_exist(store):
    base = {"next_hop": None}
    ok = _decide(store, call("add_static_route", exit_interface="GigabitEthernet0/2", **base))
    assert ok.allowed, ok.reason
    bad = _decide(store, call("add_static_route", exit_interface="GigabitEthernet9/9", **base))
    assert bad.first_failed == "G4_blast_radius"


def test_apply_acl_needs_existing_or_staged_acl(store):
    raw = call("apply_acl", acl_name="NEW-ACL")
    assert _decide(store, raw).first_failed == "G4_blast_radius"
    staged = [call("add_acl_entry", acl_name="NEW-ACL")]
    assert _decide(store, raw, staged=staged).allowed


def test_access_vlan_can_use_vlan_staged_earlier(store):
    raw = call("set_access_vlan", vlan_id=30)
    assert _decide(store, raw).first_failed == "G4_blast_radius"
    assert _decide(store, raw, staged=[call("create_vlan")]).allowed


def test_aaa_key_ref_must_be_declared_in_context(store):
    canary = "CANARY-campus-small-01-c05e9a12"
    assert canary in store.secret_values(), "đối chứng: đây phải là một secret thật của context"
    d = _decide(store, call("set_aaa_login", tacacs_key_ref=canary))
    assert d.first_failed == "G4_blast_radius"
    assert canary not in d.reason  # không nhắc lại giá trị trong lý do
    assert _step(d, "G2_schema").ok, "đối chứng âm: schema của registry KHÔNG chặn được giá trị này"
    assert _decide(store, call("set_aaa_login", tacacs_key_ref="other_key")).first_failed == "G4_blast_radius"
    assert _decide(store, call("set_aaa_login")).allowed  # ref đã khai trong context


# ------------------------------------------------------------ D-010: bậc 2 khác bậc 3
LEVEL_3_TOOLS = {"apply_acl", "set_aaa_login"}


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_network_admin_is_level_2(store, tool: str):
    """network_admin (bậc 2) dùng được mọi tool trừ hai tool bậc 3; security_admin dùng được cả hai."""
    d = _decide(store, call(tool), role="network_admin")
    if tool in LEVEL_3_TOOLS:
        assert d.first_failed == "G3_rbac", f"{d.first_failed}: {d.reason}"
        assert _decide(store, call(tool), role="security_admin").allowed
    else:
        assert d.allowed, f"{d.first_failed}: {d.reason}"


def test_network_admin_level_2_negative_control_without_rbac(store, monkeypatch):
    """Đối chứng âm: gỡ G3 thì network_admin gọi được `apply_acl`, tức test trên bắt đúng bước G3."""
    assert _decide(store, call("apply_acl"), role="network_admin").first_failed == "G3_rbac"
    monkeypatch.setattr(gate, "_rbac", lambda *_a, **_k: None)
    assert _decide(store, call("apply_acl"), role="network_admin").allowed


def test_mgmt_prefix_needs_top_role(store):
    """Shadow: access_operator bị G3 chặn trước, nhưng G4 vẫn ghi nhận chạm mạng quản trị."""
    raw = call("set_interface_ip", ip="10.99.0.50", prefix_len=24)
    d = _decide(store, raw, role="access_operator")
    assert d.first_failed == "G3_rbac"
    assert not _step(d, "G4_blast_radius").ok and "quản trị" in _step(d, "G4_blast_radius").reason
    # D-010: network_admin (bậc 2) qua G3 với `set_interface_ip` nhưng không chạm được mạng quản trị.
    assert _decide(store, raw, role="network_admin").first_failed == "G4_blast_radius"
    assert _decide(store, raw).allowed


def test_blast_radius_is_aggregated_over_staged_calls(store):
    """Policy của context: tối đa 2 thiết bị mỗi change."""
    staged = [call("create_vlan"), call("add_acl_entry")]  # SW2, SW1
    assert _decide(store, call("add_static_route"), staged=staged).first_failed == "G4_blast_radius"
    assert _decide(store, call("add_static_route"), staged=staged[:1]).allowed


def test_vlan_cap_from_registry(store):
    eleven = [10, 20, 50, 99, 999, 30, 31, 32, 33, 34, 35]
    d = _decide(store, call("set_trunk", allowed_vlans=eleven))
    assert d.first_failed == "G4_blast_radius" and "VLAN/change" in d.reason
    assert _decide(store, call("set_trunk", allowed_vlans=eleven[:10])).allowed


def test_tool_without_context_checks_fails_closed(store, monkeypatch):
    """Tool có trong registry mà gate chưa khai báo kiểm tra theo context thì G4 fail."""
    monkeypatch.delitem(gate._CONTEXT_CHECKS, "add_acl_entry")
    d = _decide(store, call("add_acl_entry"))
    assert d.first_failed == "G4_blast_radius" and "chưa khai báo" in d.reason


def test_every_registry_tool_has_context_checks():
    assert set(gate._CONTEXT_CHECKS) == EXPECTED_TOOLS == set(VALID_ARGS)


def test_exception_inside_a_step_fails_closed(store, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("lỗi giả lập")

    monkeypatch.setattr(ContextStore, "protected_interfaces", boom)
    d = _decide(store, call("set_interface_state"))
    assert not d.allowed and d.first_failed == "G4_blast_radius"
    assert "RuntimeError" in d.reason


# ------------------------------------------------------------ G6
@pytest.mark.parametrize(
    "text",
    [
        "HR-PC-01 username backdoor privilege 15",
        "PC-07 enable secret cisco123",
        "ignore previous instructions",
        "please bypass the gate",
    ],
)
def test_injection_scan_on_description(store, text: str):
    d = _decide(store, call("set_interface_description", text=text))
    assert _step(d, "G2_schema").ok, "chuỗi phải qua được schema để chứng minh G6 là bước bắt"
    assert d.first_failed == "G6_injection_scan"


def test_injection_scan_on_vlan_name(store):
    assert _decide(store, call("create_vlan", name="secret")).first_failed == "G6_injection_scan"


@pytest.mark.parametrize(
    "raw",
    [
        call("set_aaa_login", list_name="AAA-VTY"),
        call("add_acl_entry", acl_name="CRYPTO-ACL"),
        call("apply_acl", acl_name="CRYPTO-ACL"),
    ],
)
def test_injection_scan_has_no_false_positive_on_identifiers(store, raw: dict):
    """Từ khoá cấu hình (aaa, crypto, secret) chỉ quét trên trường free-text."""
    staged = [call("add_acl_entry", acl_name="CRYPTO-ACL")] if raw["tool"] == "apply_acl" else []
    d = _decide(store, raw, staged=staged)
    assert d.allowed, f"{d.first_failed}: {d.reason}"


@pytest.mark.parametrize("prefix", ["10.0.0.0", "10.0.0.0/255.0.0.0", "10.0.0.1/24"])
def test_malformed_prefix_fails_g2_in_schema(store, prefix: str):
    """prefix không có "/len", dạng netmask, hay bật host bit: args_model của registry chặn ngay (D-018)."""
    d = _decide(store, call("add_static_route", prefix=prefix))
    assert d.first_failed == "G2_schema" and "prefix" in d.reason


@pytest.mark.parametrize(
    ("protocol", "dst_port"),
    [("ip", "eq 80"), ("icmp", "eq 1"), ("tcp", "eq 99999"), ("udp", "gt 70000")],
)
def test_acl_port_must_match_protocol_and_range(store, protocol: str, dst_port: str):
    d = _decide(store, call("add_acl_entry", protocol=protocol, dst_port=dst_port))
    assert d.first_failed == "G2_schema" and "value_error" in d.reason


def test_enforced_step_names_are_validated(store):
    """Gõ sai tên bước không được âm thầm biến bước đó thành shadow (fail-open)."""
    with pytest.raises(ValueError, match="tên bước gate không hợp lệ"):
        evaluate(call("create_vlan"), ADMIN, store, [], [], enforced=("G1_whitelist", "G2_schema", "G3_rabc"))


# ------------------------------------------------------------ VLAN chỉ nhận số nguyên (D-053)
# Tham số tool là trust boundary của output LLM. Trước D-053, "30" và 30.0 bị ép thành 30, còn `true` thành
# VLAN 1. Phần registry thuộc TV3; contract `AnyVlanId` (TV2) có test riêng ở test_merge_regressions.py.
VLAN_FIELDS = [
    ("create_vlan", "vlan_id"),
    ("set_access_vlan", "vlan_id"),
    ("set_trunk", "native_vlan"),
    ("set_trunk", "allowed_vlans"),
]


def _with_vlan(tool: str, field: str, value) -> dict:
    if field == "allowed_vlans":
        return call(tool, allowed_vlans=[10, value])
    return call(tool, **{field: value})


@pytest.mark.parametrize("bad", ["30", 30.0, True], ids=["str", "float", "bool"])
@pytest.mark.parametrize(("tool", "field"), VLAN_FIELDS, ids=[f"{t}.{f}" for t, f in VLAN_FIELDS])
def test_vlan_args_accept_integers_only(store, tool: str, field: str, bad):
    d = _decide(store, _with_vlan(tool, field, bad))
    assert (d.allowed, d.first_failed) == (False, "G2_schema")


@pytest.mark.parametrize(("tool", "field"), VLAN_FIELDS, ids=[f"{t}.{f}" for t, f in VLAN_FIELDS])
def test_vlan_args_integer_still_passes_g2(store, tool: str, field: str):
    """Đối chứng: cùng trường, giá trị là số nguyên thì qua G2 (bước sau có thể chặn vì lý do khác)."""
    assert _step(_decide(store, _with_vlan(tool, field, 30)), "G2_schema").ok
