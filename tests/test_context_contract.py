"""Contract context v0.2 (D-042): một schema khớp file context thật.

- File context thật hợp lệ; mỗi đột biến của nó ra ĐÚNG MỘT lỗi, ở đúng đường dẫn (đối chứng âm: file
  gốc hợp lệ).
- Secret là SecretStr: dump, repr, str không lộ giá trị; lỗi validate không chép giá trị (đối chứng âm:
  model tạm dùng `str`, hoặc không bật hide_input_in_errors, thì lộ).
- `NetworkContext.secret_values()` bằng đúng `ContextStore.secret_values()` trên file thật, để PR chuyển
  ContextStore sang contract không làm mất secret nào của phép quét leak.

Không test nào in giá trị secret của file thật: so sánh bằng tập, thông báo lỗi chỉ dùng
`include_input=False`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ConfigDict, ValidationError

from harness_agent.harness.context_store import ContextStore
from harness_agent.models import NetworkContext, TestCase
from harness_agent.models.common import StrictModel

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
EXAMPLE = ROOT / "examples" / "valid" / "network_context.json"
TESTCASE_EXAMPLE = ROOT / "examples" / "valid" / "testcase.json"
# Giá trị giả trong examples/, không phải secret của context thật.
EXAMPLE_SECRET = "example-not-a-real-secret-01"  # noqa: S105


def _campus() -> dict:
    return json.loads(CAMPUS.read_text(encoding="utf-8"))


def _example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _errors(data: dict) -> list[tuple[str, tuple]]:
    with pytest.raises(ValidationError) as exc:
        NetworkContext.model_validate(data)
    return [(e["type"], tuple(e["loc"])) for e in exc.value.errors(include_input=False)]


# ------------------------------------------------------------ file thật
def test_real_context_file_is_valid():
    ctx = NetworkContext.model_validate(_campus())
    assert [d.hostname for d in ctx.devices] == ["R1", "SW1", "SW2"]
    assert {d.role.value for d in ctx.devices} == {"router", "distribution_switch", "access_switch"}
    unused = [i for d in ctx.devices for i in d.interfaces if i.mode.value == "unused"]
    assert len(unused) == 9 and not any(i.enabled for i in unused)


def _set(path: tuple, value) -> Callable[[dict], None]:
    def apply(data: dict) -> None:
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


# (tên, đột biến, loại lỗi, đường dẫn). Mỗi đột biến chỉ đổi đúng một chỗ của file thật.
MUTATIONS: list[tuple[str, Callable[[dict], None], str, tuple]] = [
    ("device-role", _set(("devices", 0, "role"), "core"), "enum", ("devices", 0, "role")),
    (
        "interface-mode",
        _set(("devices", 1, "interfaces", 4, "mode"), "shutdown"),
        "enum",
        ("devices", 1, "interfaces", 4, "mode"),
    ),
    ("extra-root", _set(("site",), "HQ"), "extra_forbidden", ("site",)),
    (
        "extra-device",
        _set(("devices", 0, "mgmt_ip"), "10.0.0.1"),
        "extra_forbidden",
        ("devices", 0, "mgmt_ip"),
    ),
    (
        "extra-interface",
        _set(("devices", 0, "interfaces", 0, "is_uplink"), True),
        "extra_forbidden",
        ("devices", 0, "interfaces", 0, "is_uplink"),
    ),
    (
        "extra-aaa",
        _set(("devices", 0, "aaa", "radius"), []),
        "extra_forbidden",
        ("devices", 0, "aaa", "radius"),
    ),
    (
        "extra-policy",
        _set(("policy", "reserved_vlan_ids"), [1]),
        "extra_forbidden",
        ("policy", "reserved_vlan_ids"),
    ),
    (
        "vlan-id-0",
        _set(("devices", 1, "vlans", 0, "vlan_id"), 0),
        "greater_than_equal",
        ("devices", 1, "vlans", 0, "vlan_id"),
    ),
    (
        "acl-seq-0",
        _set(("devices", 0, "acls", "EDGE-IN", 0, "seq"), 0),
        "greater_than_equal",
        ("devices", 0, "acls", "EDGE-IN", 0, "seq"),
    ),
    (
        "acl-action",
        _set(("devices", 0, "acls", "EDGE-IN", 0, "action"), "allow"),
        "enum",
        ("devices", 0, "acls", "EDGE-IN", 0, "action"),
    ),
    (
        "acl-endpoint",
        _set(("devices", 0, "acls", "EDGE-IN", 0, "src"), "10.0.0.0/8"),
        "string_pattern_mismatch",
        ("devices", 0, "acls", "EDGE-IN", 0, "src"),
    ),
    (
        "privilege-16",
        _set(("devices", 0, "aaa", "local_users", 0, "privilege"), 16),
        "less_than_equal",
        ("devices", 0, "aaa", "local_users", 0, "privilege"),
    ),
    (
        "auth-method",
        _set(("devices", 0, "aaa", "login_lists", "default"), ["none"]),
        "enum",
        ("devices", 0, "aaa", "login_lists", "default", 0),
    ),
    (
        "empty-secret",
        _set(("devices", 0, "secrets", "tacacs_key"), ""),
        "too_short",
        ("devices", 0, "secrets", "tacacs_key"),
    ),
    ("schema-version", _set(("schema_version",), "0.1"), "literal_error", ("schema_version",)),
    (
        "description-nel",
        _set(("devices", 0, "interfaces", 0, "description"), "UPLINK" + chr(0x85) + "end"),
        "string_pattern_mismatch",
        ("devices", 0, "interfaces", 0, "description"),
    ),
    (
        "banner-ls",
        _set(("devices", 2, "banner_motd"), "x" + chr(0x2028) + "y"),
        "string_pattern_mismatch",
        ("devices", 2, "banner_motd"),
    ),
    (
        "unused-enabled",
        _set(("devices", 0, "interfaces", 3, "enabled"), True),
        "value_error",
        ("devices", 0, "interfaces", 3),
    ),
    (
        "missing-ip-routing",
        lambda d: d["devices"][1].pop("ip_routing"),
        "missing",
        ("devices", 1, "ip_routing"),
    ),
    ("missing-context-id", lambda d: d.pop("context_id"), "missing", ("context_id",)),
]


@pytest.mark.parametrize(("name", "mutate", "kind", "loc"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_a_wrong_context_fails_at_exactly_the_right_place(name: str, mutate, kind: str, loc: tuple):
    data = _campus()
    mutate(data)
    assert _errors(data) == [(kind, loc)]


def test_mutations_target_what_they_claim():
    """Đối chứng cho bảng trên: đúng chỗ bị đột biến có trong file gốc (interface 3 của R1 là cổng
    `unused`)."""
    data = _campus()
    assert data["devices"][0]["interfaces"][3]["mode"] == "unused"
    assert data["devices"][1]["interfaces"][4]["mode"] == "unused"
    assert "EDGE-IN" in data["devices"][0]["acls"] and "tacacs_key" in data["devices"][0]["secrets"]


# ------------------------------------------------------------ secret
def test_dumps_and_reprs_never_contain_a_secret_value():
    ctx = NetworkContext.model_validate(_campus())
    secrets = ctx.secret_values()
    assert len(secrets) == 7
    outputs = [
        ctx.model_dump_json(),
        json.dumps(ctx.model_dump(mode="json")),
        repr(ctx),
        str(ctx),
        str(ctx.model_dump()),
    ]
    assert not any(s in out for s in secrets for out in outputs)


def test_negative_control_a_plain_str_secret_field_leaks():
    """Đối chứng âm: cùng dữ liệu, trường `secrets` kiểu `str` thì dump lộ giá trị, tức test trên bắt
    đúng điều cần."""

    class PlainSecrets(StrictModel):
        secrets: dict[str, str]

    device = _example()["devices"][0]
    leaky = PlainSecrets(secrets=device["secrets"])
    assert EXAMPLE_SECRET in leaky.model_dump_json() and EXAMPLE_SECRET in repr(leaky)
    ctx = NetworkContext.model_validate(_example())
    assert EXAMPLE_SECRET not in ctx.model_dump_json() and EXAMPLE_SECRET not in repr(ctx)


def test_testcase_dump_does_not_contain_the_context_secret():
    """Case chỉ trỏ tới file context (`context_ref`, commit (c)): secret có trong file được trỏ tới,
    nhưng không có trong case. Đối chứng: file mà case trỏ tới đúng là chứa giá trị đó."""
    case = TestCase.model_validate(json.loads(TESTCASE_EXAMPLE.read_text(encoding="utf-8")))
    assert EXAMPLE_SECRET in (ROOT / case.context_ref.path).read_text(encoding="utf-8")
    assert EXAMPLE_SECRET not in case.model_dump_json() and EXAMPLE_SECRET not in repr(case)


def test_validation_error_does_not_echo_a_secret():
    """Secret đặt nhầm vào một trường lạ: lỗi `extra_forbidden` không chép giá trị."""
    data = _example()
    data["devices"][0]["enable_secret"] = EXAMPLE_SECRET
    with pytest.raises(ValidationError) as exc:
        NetworkContext.model_validate(data)
    assert EXAMPLE_SECRET not in str(exc.value) and EXAMPLE_SECRET not in repr(exc.value)

    class Echoing(NetworkContext):  # đối chứng âm: tắt cờ ở model ngoài cùng thì lỗi chép giá trị
        model_config = ConfigDict(**{**NetworkContext.model_config, "hide_input_in_errors": False})

    with pytest.raises(ValidationError) as echoed:
        Echoing.model_validate(data)
    assert EXAMPLE_SECRET in str(echoed.value)


def test_secret_values_match_the_context_store_on_the_real_file():
    assert (
        NetworkContext.model_validate(_campus()).secret_values() == ContextStore.load(CAMPUS).secret_values()
    )


def test_short_secrets_are_valid_but_not_scanned():
    data = _example()
    data["devices"][0]["secrets"] = {"short": "abc", "long": EXAMPLE_SECRET}
    ctx = NetworkContext.model_validate(data)
    assert ctx.secret_values() == {EXAMPLE_SECRET}
    assert ctx.devices[0].secrets["short"].get_secret_value() == "abc"


def test_secret_refs_are_names_not_values():
    """`secret_ref`/`key_ref` là tên khoá trong `secrets` (kiểm tham chiếu có thật là việc của invariants)."""
    ctx = NetworkContext.model_validate(_campus())
    for device in ctx.devices:
        refs = [u.secret_ref for u in device.aaa.local_users] + [t.key_ref for t in device.aaa.tacacs_servers]
        assert set(refs) <= set(device.secrets)
        assert not (set(refs) & ctx.secret_values())


def test_contract_does_not_modify_the_file():
    before = copy.deepcopy(_campus())
    NetworkContext.model_validate(before)
    assert before == _campus()
