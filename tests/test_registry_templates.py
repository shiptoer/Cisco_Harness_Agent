"""Test cho 11 template Jinja2 của tool registry (task 3.2).

Golden test so CLI render ra với chuỗi kỳ vọng đầy đủ, từng ký tự.
Cú pháp mới đúng về lý thuyết; cần xác nhận trên EVE-NG IOSv/IOSvL2 ở task 3.5.
Mỗi test bảo mật có đối chứng âm: chứng minh phép kiểm tra bắt được lỗi khi control bị gỡ.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harness_agent.tools.registry import base as base_mod
from harness_agent.tools.registry import render
from harness_agent.tools.registry.render import render_args
from harness_agent.tools.registry.tools import TOOL_REGISTRY

# (tool, args, CLI kỳ vọng). Mỗi tool ít nhất một case; nhánh if/else có thêm case.
GOLDEN: list[tuple[str, dict, str]] = [
    (
        "create_vlan",
        {"device": "SW1", "vlan_id": 10, "name": "USERS"},
        "vlan 10\n name USERS",
    ),
    (
        "set_access_vlan",
        {"device": "SW1", "interface": "GigabitEthernet0/1", "vlan_id": 10},
        "interface GigabitEthernet0/1\n switchport mode access\n switchport access vlan 10",
    ),
    (
        "set_trunk",
        {
            "device": "SW1",
            "interface": "GigabitEthernet0/2",
            "allowed_vlans": [30, 10, 20, 10],
            "native_vlan": 99,
        },
        "interface GigabitEthernet0/2\n"
        " switchport trunk encapsulation dot1q\n"
        " switchport mode trunk\n"
        " switchport trunk allowed vlan 10,20,30\n"
        " switchport trunk native vlan 99",
    ),
    (
        "set_interface_ip",
        {"device": "R1", "interface": "GigabitEthernet0/1", "ip": "192.168.1.1", "prefix_len": 24},
        "interface GigabitEthernet0/1\n ip address 192.168.1.1 255.255.255.0",
    ),
    (
        "set_interface_state",
        {"device": "R1", "interface": "GigabitEthernet0/1", "enabled": True},
        "interface GigabitEthernet0/1\n no shutdown",
    ),
    (
        "set_interface_state",
        {"device": "R1", "interface": "GigabitEthernet0/1", "enabled": False},
        "interface GigabitEthernet0/1\n shutdown",
    ),
    (
        "set_interface_description",
        {"device": "R1", "interface": "GigabitEthernet0/1", "text": "Uplink to CORE-01 (Gi0/1)"},
        "interface GigabitEthernet0/1\n description Uplink to CORE-01 (Gi0/1)",
    ),
    (
        "add_static_route",
        {"device": "R1", "prefix": "192.168.10.0/24", "next_hop": "10.0.0.2"},
        "ip route 192.168.10.0 255.255.255.0 10.0.0.2",
    ),
    (
        "add_static_route",
        {"device": "R1", "prefix": "0.0.0.0/0", "exit_interface": "GigabitEthernet0/0"},
        "ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0",
    ),
    (
        "remove_static_route",
        {"device": "R1", "prefix": "10.1.0.0/16", "next_hop": "10.0.0.2"},
        "no ip route 10.1.0.0 255.255.0.0 10.0.0.2",
    ),
    (
        "add_acl_entry",
        {
            "device": "R1",
            "acl_name": "WEB-IN",
            "seq": 10,
            "action": "permit",
            "protocol": "tcp",
            "src": "10.0.0.0/24",
            "dst": "host 192.168.1.10",
            "dst_port": "eq 443",
        },
        "ip access-list extended WEB-IN\n 10 permit tcp 10.0.0.0 0.0.0.255 host 192.168.1.10 eq 443",
    ),
    (
        "add_acl_entry",
        {
            "device": "R1",
            "acl_name": "WEB-IN",
            "seq": 20,
            "action": "deny",
            "protocol": "ip",
            "src": "any",
            "dst": "172.16.0.0/12",
        },
        "ip access-list extended WEB-IN\n 20 deny ip any 172.16.0.0 0.15.255.255",
    ),
    (
        # dst_port đã chứa toán tử ("gt 1023"): template render nguyên văn, không thêm "eq".
        "add_acl_entry",
        {
            "device": "R1",
            "acl_name": "DNS-OUT",
            "seq": 30,
            "action": "permit",
            "protocol": "udp",
            "src": "host 10.0.0.53",
            "dst": "any",
            "dst_port": "gt 1023",
        },
        "ip access-list extended DNS-OUT\n 30 permit udp host 10.0.0.53 any gt 1023",
    ),
    (
        "apply_acl",
        {"device": "R1", "interface": "Vlan10", "acl_name": "WEB-IN", "direction": "in"},
        "interface Vlan10\n ip access-group WEB-IN in",
    ),
    (
        "set_aaa_login",
        {
            "device": "R1",
            "list_name": "VTY-AUTH",
            "methods": ["group tacacs+", "local"],
            "tacacs_key_ref": "tac-key.01",
        },
        "aaa new-model\n"
        "! TACACS+ key: <SECRET:tac-key.01>\n"
        "aaa authentication login VTY-AUTH group tacacs+ local\n"
        "line vty 0 4\n"
        " login authentication VTY-AUTH",
    ),
    (
        "set_aaa_login",
        {"device": "R1", "list_name": "default", "methods": ["local"]},
        "aaa new-model\naaa authentication login default local\nline vty 0 4\n login authentication default",
    ),
]


def _render(tool: str, raw: dict) -> str:
    spec = TOOL_REGISTRY[tool]
    return render_args(spec.template, spec.args_model.model_validate(raw))


def _args(tool: str) -> dict:
    """Args hợp lệ đầu tiên của tool trong GOLDEN."""
    return next(dict(raw) for t, raw, _ in GOLDEN if t == tool)


# ---------------------------------------------------------------------------
# Golden
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("tool", "raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_cli(tool: str, raw: dict, expected: str):
    assert _render(tool, raw) == expected


def test_every_tool_has_golden_case():
    assert {t for t, _, _ in GOLDEN} == set(TOOL_REGISTRY)


@pytest.mark.parametrize("native", [None, "missing"])
def test_trunk_native_vlan_is_required(native):
    """native_vlan bắt buộc (không nhận None): template luôn render dòng native vlan, không cần {% if %}."""
    raw = _args("set_trunk")
    if native == "missing":
        del raw["native_vlan"]
    else:
        raw["native_vlan"] = native
    with pytest.raises(ValidationError):
        TOOL_REGISTRY["set_trunk"].args_model.model_validate(raw)


# ---------------------------------------------------------------------------
# Đủ 11 template, render được, đúng định dạng
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", sorted(TOOL_REGISTRY))
def test_template_exists_and_renders(tool: str):
    spec = TOOL_REGISTRY[tool]
    assert (render._TEMPLATES_DIR / spec.template).is_file()
    assert _render(tool, _args(tool))  # không TemplateNotFound, không UndefinedError


def test_exactly_eleven_templates():
    files = {p.name for p in render._TEMPLATES_DIR.glob("*.j2")}
    assert files == {spec.template for spec in TOOL_REGISTRY.values()}
    assert len(files) == 11


@pytest.mark.parametrize("tool", sorted(TOOL_REGISTRY))
def test_template_file_format(tool: str):
    raw = (render._TEMPLATES_DIR / TOOL_REGISTRY[tool].template).read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    assert all(line == line.rstrip() for line in raw.decode("utf-8").split("\n"))


@pytest.mark.parametrize(("tool", "raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_rendered_cli_format(tool: str, raw: dict, expected: str):
    lines = _render(tool, raw).split("\n")
    assert all(line == line.rstrip() and line for line in lines)
    assert "end" not in (line.strip() for line in lines)


# ---------------------------------------------------------------------------
# Enum: luôn render .value
# ---------------------------------------------------------------------------
ENUM_REPRS = ("Action.", "Protocol.", "Direction.", "AuthMethod.")


@pytest.mark.parametrize(("tool", "raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_no_enum_repr_in_cli(tool: str, raw: dict, expected: str):
    cli = _render(tool, raw)
    assert not any(r in cli for r in ENUM_REPRS)


def test_enum_repr_negative_control():
    """Đối chứng âm: bỏ .value thì phép kiểm tra ở trên bắt được."""
    args = TOOL_REGISTRY["add_acl_entry"].args_model.model_validate(_args("add_acl_entry"))
    cli = render._env.from_string("{{ a.action }} {{ a.protocol }}").render(a=args)
    assert cli == "permit tcp"  # P StrEnum renders as its wire value even without .value
    assert any(r in "Action.permit Protocol.tcp" for r in ENUM_REPRS)  # negative fixture


# ---------------------------------------------------------------------------
# AAA: không bao giờ render secret thật, chỉ <SECRET:ref>
# ---------------------------------------------------------------------------
def _key_lines_are_placeholders(cli: str) -> bool:
    return all("<SECRET:" in line for line in cli.split("\n") if "key" in line.lower())


def test_aaa_renders_only_secret_placeholder():
    cli = _render("set_aaa_login", _args("set_aaa_login"))
    assert "<SECRET:tac-key.01>" in cli
    assert "${VAULT" not in cli
    assert _key_lines_are_placeholders(cli)


def test_aaa_key_placeholder_negative_control():
    """Đối chứng âm: template đưa ref/giá trị ra thẳng một dòng key thì phép kiểm tra bắt được."""
    assert not _key_lines_are_placeholders("tacacs server T\n key S3cr3t-Value")


@pytest.mark.parametrize("field", ["tacacs_key", "password", "secret"])
def test_aaa_rejects_raw_secret_field(field: str):
    raw = {**_args("set_aaa_login"), field: "S3cr3t-Value"}
    with pytest.raises(ValidationError):
        TOOL_REGISTRY["set_aaa_login"].args_model.model_validate(raw)


# ---------------------------------------------------------------------------
# Injection: CR/LF trong chuỗi đi vào CLI phải bị args_model từ chối
# ---------------------------------------------------------------------------
BAD_SUFFIXES = ["\n", "\r", "\r\nshutdown", "\nend"]
# Mọi field chuỗi đi vào CLI: (tool, field, giá trị hợp lệ, override để args còn lại vẫn hợp lệ).
TEXT_FIELDS = [
    ("create_vlan", "name", "USERS", {}),
    ("create_vlan", "device", "SW1", {}),
    ("set_access_vlan", "interface", "GigabitEthernet0/1", {}),
    ("set_interface_ip", "ip", "192.168.1.1", {}),
    ("set_interface_description", "text", "Uplink", {}),
    ("add_static_route", "prefix", "192.168.10.0/24", {}),
    ("add_static_route", "next_hop", "10.0.0.2", {}),
    ("add_static_route", "exit_interface", "GigabitEthernet0/0", {"next_hop": None}),
    ("remove_static_route", "prefix", "10.1.0.0/16", {}),
    ("remove_static_route", "next_hop", "10.0.0.2", {}),
    ("add_acl_entry", "acl_name", "WEB-IN", {}),
    ("add_acl_entry", "src", "10.0.0.0/24", {}),
    ("add_acl_entry", "dst", "host 192.168.1.10", {}),
    ("add_acl_entry", "dst_port", "eq 443", {}),
    ("apply_acl", "interface", "Vlan10", {}),
    ("apply_acl", "acl_name", "WEB-IN", {}),
    ("set_aaa_login", "list_name", "VTY-AUTH", {}),
    ("set_aaa_login", "tacacs_key_ref", "tac-key.01", {}),
]


@pytest.mark.parametrize("suffix", BAD_SUFFIXES, ids=repr)
@pytest.mark.parametrize(
    ("tool", "field", "base", "override"), TEXT_FIELDS, ids=[f"{t[0]}.{t[1]}" for t in TEXT_FIELDS]
)
def test_crlf_in_text_field_rejected(tool: str, field: str, base: str, override: dict, suffix: str):
    raw = {**_args(tool), **override, field: base}
    TOOL_REGISTRY[tool].args_model.model_validate(raw)  # giá trị gốc phải hợp lệ
    with pytest.raises(ValidationError):
        TOOL_REGISTRY[tool].args_model.model_validate({**raw, field: base + suffix})


def test_keyref_trailing_newline_negative_control(monkeypatch):
    """Đối chứng âm: quay lại kiểm tra cũ (re.match với "^...$") thì "\\n" cuối chuỗi lọt vào CLI."""
    old = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
    monkeypatch.setattr(base_mod, "KEY_REF_PATTERN", SimpleNamespace(fullmatch=old.match))
    raw = {**_args("set_aaa_login"), "tacacs_key_ref": "tac-key.01\n"}
    args = TOOL_REGISTRY["set_aaa_login"].args_model.model_validate(raw)
    assert "<SECRET:tac-key.01\n>" in render_args("aaa_login.j2", args)


def test_crlf_negative_control():
    """Đối chứng âm: bỏ qua validation (model_construct) thì CR/LF chèn được lệnh vào CLI."""
    model = TOOL_REGISTRY["set_interface_description"].args_model
    args = model.model_construct(device="R1", interface="GigabitEthernet0/1", text="x\nshutdown")
    cli = render_args("interface_description.j2", args)
    assert "shutdown" in cli.split("\n")
