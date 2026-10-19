"""Dữ liệu mẫu dùng chung cho các test của tool registry và gate (không phải file test).

Mỗi bộ tham số trong VALID_ARGS hợp lệ theo args_model VÀ qua được gate với role security_admin (role
cao nhất, D-010) trên context `data/contexts/campus-small-01.json` khi đứng một mình.
"""

from __future__ import annotations

EXPECTED_TOOLS = {
    "create_vlan",
    "set_access_vlan",
    "set_trunk",
    "set_interface_ip",
    "set_interface_state",
    "set_interface_description",
    "add_static_route",
    "remove_static_route",
    "add_acl_entry",
    "apply_acl",
    "set_aaa_login",
}

VALID_ARGS: dict[str, dict] = {
    "create_vlan": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"},
    "set_access_vlan": {"device": "SW2", "interface": "GigabitEthernet1/1", "vlan_id": 20},
    "set_trunk": {
        "device": "SW2",
        "interface": "GigabitEthernet1/2",
        "allowed_vlans": [10, 20],
        "native_vlan": 999,
    },
    "set_interface_ip": {
        "device": "R1",
        "interface": "GigabitEthernet0/2",
        "ip": "10.0.13.1",
        "prefix_len": 30,
    },
    "set_interface_state": {"device": "R1", "interface": "GigabitEthernet0/2", "enabled": True},
    "set_interface_description": {"device": "SW2", "interface": "GigabitEthernet0/2", "text": "HR-PC-02"},
    "add_static_route": {"device": "R1", "prefix": "10.20.0.0/16", "next_hop": "10.0.12.2"},
    "remove_static_route": {"device": "R1", "prefix": "10.10.0.0/16", "next_hop": "10.0.12.2"},
    "add_acl_entry": {
        "device": "SW1",
        "acl_name": "SERVERS-PROTECT",
        "seq": 60,
        "action": "permit",
        "protocol": "tcp",
        "src": "10.10.20.0/24",
        "dst": "host 10.10.50.20",
        "dst_port": "eq 22",
    },
    "apply_acl": {"device": "SW1", "interface": "Vlan20", "acl_name": "SERVERS-PROTECT", "direction": "in"},
    "set_aaa_login": {
        "device": "R1",
        "list_name": "VTY-AUTH",
        "methods": ["group tacacs+", "local"],
        "tacacs_key_ref": "tacacs_key",
    },
}


def call(tool: str, **overrides) -> dict:
    """Tool call thô `{tool, args}` từ bộ tham số mẫu, ghi đè một số trường."""
    return {"tool": tool, "args": {**VALID_ARGS.get(tool, {}), **overrides}}
