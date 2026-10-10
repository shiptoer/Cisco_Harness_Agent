"""Invariants của context (D-005, task 2.2): `check_invariants` trên file context thật và các đột biến của nó.

- File thật và context ví dụ: 0 vi phạm (đối chứng âm cho mọi đột biến).
- Mỗi mã trong INVARIANT_CODES có một đột biến chỉ đổi một chỗ, cho ĐÚNG MỘT vi phạm, đúng mã và đúng `path`.
- Gom đủ mọi vi phạm theo thứ tự trong file; `detail` không chứa free-text hay giá trị secret.

Không test nào in giá trị secret: so sánh bằng tập, chuỗi đánh dấu là chuỗi giả.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from harness_agent.models import INVARIANT_CODES, NetworkContext, check_invariants
from harness_agent.models.invariants import REDACTED_DETAIL

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
EXAMPLE = ROOT / "examples" / "valid" / "network_context.json"
EXAMPLE_SECRET = "example-not-a-real-secret-01"  # noqa: S105 — giá trị giả trong examples/
MARKER = "MARKER-free-text-q9"


def _campus() -> dict:
    return json.loads(CAMPUS.read_text(encoding="utf-8"))


def _check(data: dict):
    return check_invariants(NetworkContext.model_validate(data))


def _itf(data: dict, d: int, i: int) -> dict:
    return data["devices"][d]["interfaces"][i]


def _set(d: int, i: int, field: str, value) -> Callable[[dict], None]:
    return lambda data: _itf(data, d, i).__setitem__(field, value)


def _add_interface(d: int, itf: dict) -> Callable[[dict], None]:
    return lambda data: data["devices"][d]["interfaces"].append({"description": "", **itf})


def _reserved_on_access(data: dict) -> None:
    data["devices"][2]["vlans"].append({"vlan_id": 1002, "name": "FDDI"})
    _itf(data, 2, 1)["access_vlan"] = 1002


# (mã, đột biến, path kỳ vọng). Chỉ số: devices 0=R1, 1=SW1, 2=SW2 trong file thật.
MUTATIONS: list[tuple[str, Callable[[dict], None], str]] = [
    ("CTX_DANGLING_VLAN", _set(2, 1, "access_vlan", 30), "devices[2].interfaces[1].access_vlan"),
    (
        "CTX_TRUNK_MISMATCH",
        lambda data: _itf(data, 2, 0)["trunk_allowed"].append(30),
        "devices[2].interfaces[0].trunk_allowed[4]",
    ),
    ("CTX_NATIVE_VLAN_MISSING", _set(2, 0, "native_vlan", 998), "devices[2].interfaces[0].native_vlan"),
    ("CTX_ACL_REF", _set(0, 0, "acl_in", "NO-SUCH-ACL"), "devices[0].interfaces[0].acl_in"),
    (
        "CTX_SVI_VLAN_MISSING",
        _add_interface(1, {"name": "Vlan77", "mode": "routed", "ipv4": "10.77.0.1/24"}),
        "devices[1].interfaces[12].name",
    ),
    ("CTX_UNUSED_NOT_PARKED", _set(2, 5, "access_vlan", 10), "devices[2].interfaces[5].access_vlan"),
    ("CTX_RESERVED_VLAN_IN_USE", _reserved_on_access, "devices[2].interfaces[1].access_vlan"),
    ("CTX_DUP_IP", _set(2, 8, "ipv4", "10.99.0.1/24"), "devices[2].interfaces[8].ipv4"),
    (
        "CTX_SUBNET_OVERLAP",
        _add_interface(1, {"name": "Loopback5", "mode": "routed", "ipv4": "10.10.10.200/32"}),
        "devices[1].interfaces[12].ipv4",
    ),
    (
        "CTX_NEXT_HOP_UNREACHABLE",
        lambda data: data["devices"][1]["static_routes"][0].__setitem__("next_hop", "10.0.13.1"),
        "devices[1].static_routes[0].next_hop",
    ),
    (
        "CTX_GATEWAY_UNREACHABLE",
        lambda data: data["devices"][2].__setitem__("default_gateway", "10.98.0.1"),
        "devices[2].default_gateway",
    ),
    (
        "CTX_SECRET_REF",
        lambda data: data["devices"][0]["aaa"]["tacacs_servers"][0].__setitem__("key_ref", "no_such_key"),
        "devices[0].aaa.tacacs_servers[0].key_ref",
    ),
    (
        "CTX_LOGIN_LIST_REF",
        lambda data: data["devices"][0]["aaa"].__setitem__("vty_login_list", "NO-LIST"),
        "devices[0].aaa.vty_login_list",
    ),
    # R1 không có interface trong mgmt_prefix; nó tới mạng quản trị bằng route 10.99.0.0/24.
    # Bỏ route đó thì vi phạm.
    ("CTX_MGMT_UNREACHABLE", lambda data: data["devices"][0]["static_routes"].pop(2), "devices[0]"),
    (
        "CTX_LINK_REF",
        lambda data: data["links"][0].__setitem__("b_interface", "GigabitEthernet0/9"),
        "links[0].b_interface",
    ),
    (
        "CTX_PROTECTED_REF",
        lambda data: data["policy"]["protected_interfaces"]["SW2"].append("GigabitEthernet0/9"),
        "policy.protected_interfaces.SW2[2]",
    ),
]


def test_real_context_and_example_have_no_violation():
    assert _check(_campus()) == []
    assert check_invariants(NetworkContext.model_validate_json(EXAMPLE.read_bytes())) == []


@pytest.mark.parametrize(("code", "mutate", "path"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_mutation_gives_exactly_one_violation_with_the_right_code_and_path(code: str, mutate, path: str):
    data = _campus()
    mutate(data)
    assert [(v.code, v.path) for v in _check(data)] == [(code, path)]


def test_every_code_has_a_mutation():
    assert [m[0] for m in MUTATIONS] == list(INVARIANT_CODES)


def test_mutations_change_the_file_they_claim():
    """Đối chứng cho bảng trên: mỗi đột biến thật sự đổi dữ liệu (bỏ đột biến thì 0 vi phạm, xem test đầu)."""
    for code, mutate, _ in MUTATIONS:
        data = _campus()
        mutate(data)
        assert data != _campus(), code


def test_subnet_shared_by_the_two_ends_of_a_link_is_not_an_overlap():
    """R1 Gi0/1 (10.0.12.1/30) và SW1 Gi0/0 (10.0.12.2/30) cùng subnet: hợp lệ, vì khác thiết bị."""
    data = _campus()
    assert _itf(data, 0, 1)["ipv4"].rsplit(".", 1)[0] == _itf(data, 1, 0)["ipv4"].rsplit(".", 1)[0]
    assert _check(data) == []


def test_all_violations_are_collected_in_file_order():
    """Ba đột biến áp theo thứ tự bất kỳ: đủ ba vi phạm, theo thứ tự trong file (R1 interfaces, R1 mạng quản
    trị, rồi SW2), và gọi lại cho đúng kết quả đó."""
    data = _campus()
    by_code = {m[0]: m for m in MUTATIONS}
    for code in ("CTX_DANGLING_VLAN", "CTX_MGMT_UNREACHABLE", "CTX_ACL_REF"):
        by_code[code][1](data)
    expected = [
        ("CTX_ACL_REF", "devices[0].interfaces[0].acl_in"),
        ("CTX_MGMT_UNREACHABLE", "devices[0]"),
        ("CTX_DANGLING_VLAN", "devices[2].interfaces[1].access_vlan"),
    ]
    assert [(v.code, v.path) for v in _check(data)] == expected
    assert [(v.code, v.path) for v in _check(data)] == expected


def test_detail_has_no_free_text():
    """Một interface vi phạm có description, banner và remark chứa chuỗi đánh dấu: không vi phạm nào
    chép chúng."""
    data = _campus()
    _itf(data, 2, 1)["description"] = MARKER
    _itf(data, 2, 1)["access_vlan"] = 30
    data["devices"][0]["banner_motd"] = MARKER
    data["devices"][0]["acls"]["EDGE-IN"][0]["remark"] = MARKER
    _itf(data, 0, 0)["acl_in"] = "NO-SUCH-ACL"
    ctx = NetworkContext.model_validate(data)
    assert MARKER in ctx.model_dump_json()  # đối chứng: chuỗi đánh dấu có trong context
    found = check_invariants(ctx)
    assert {v.code for v in found} == {"CTX_DANGLING_VLAN", "CTX_ACL_REF"}
    assert not any(MARKER in str(v) or MARKER in v.model_dump_json() for v in found)


def test_detail_never_contains_a_secret_value():
    data = _campus()
    data["devices"][0]["aaa"]["tacacs_servers"][0]["key_ref"] = "no_such_key"
    ctx = NetworkContext.model_validate(data)
    found = check_invariants(ctx)
    assert [v.detail for v in found] == [
        "không có khoá no_such_key trong secrets của R1"
    ]  # tên, không phải giá trị
    assert not any(s in str(v) for s in ctx.secret_values() for v in found)


def test_a_secret_value_put_where_a_name_belongs_is_redacted():
    """Context sai đặt chính giá trị secret vào `key_ref`: vi phạm vẫn có, nhưng `detail` bị che."""
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["devices"][0]["aaa"]["tacacs_servers"] = [
        {"name": "TAC1", "address": "10.0.99.5", "key_ref": EXAMPLE_SECRET}
    ]
    found = check_invariants(NetworkContext.model_validate(data))
    assert [(v.code, v.path, v.detail) for v in found] == [
        ("CTX_SECRET_REF", "devices[0].aaa.tacacs_servers[0].key_ref", REDACTED_DETAIL)
    ]
    assert EXAMPLE_SECRET not in str(found)


def test_check_does_not_modify_the_context():
    ctx = NetworkContext.model_validate(_campus())
    before = ctx.model_dump()
    check_invariants(ctx)
    assert ctx.model_dump() == before and copy.deepcopy(ctx) == ctx
