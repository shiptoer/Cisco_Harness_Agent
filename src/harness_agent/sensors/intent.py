"""Independent command contract for approved operations, without importing Jinja templates.

Comparison preserves command order and multiplicity. This checks rendering fidelity, not
whether natural-language intent is correct or the live network will converge.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping

from harness_agent.models import ToolCall

from .report import Finding


def _endpoint(value: str) -> str:
    if value == "any" or value.startswith("host "):
        return value
    net = ipaddress.IPv4Network(value)
    return f"{net.network_address} {net.hostmask}"


def _commands(call: ToolCall) -> list[str]:
    a, tool = call.args, call.tool
    itf = [f"interface {a['interface']}"] if "interface" in a else []
    if tool == "create_vlan":
        return [f"vlan {a['vlan_id']}", f"name {a['name']}", "exit"]
    if tool == "set_access_vlan":
        return itf + ["switchport mode access", f"switchport access vlan {a['vlan_id']}", "exit"]
    if tool == "set_trunk":
        return itf + [
            "switchport trunk encapsulation dot1q",
            "switchport mode trunk",
            "switchport trunk allowed vlan " + ",".join(map(str, a["allowed_vlans"])),
            f"switchport trunk native vlan {a['native_vlan']}",
            "exit",
        ]
    if tool == "set_interface_ip":
        mask = ipaddress.IPv4Network(f"0.0.0.0/{a['prefix_len']}").netmask
        return itf + [f"ip address {a['ip']} {mask}", "exit"]
    if tool == "set_interface_state":
        return itf + ["no shutdown" if a["enabled"] else "shutdown", "exit"]
    if tool == "set_interface_description":
        return itf + [f"description {a['text']}".strip(), "exit"]
    if tool in {"add_static_route", "remove_static_route"}:
        net = ipaddress.IPv4Network(a["prefix"])
        no = "no " if tool == "remove_static_route" else ""
        hop = a.get("next_hop") or a.get("exit_interface")
        return [f"{no}ip route {net.network_address} {net.netmask} {hop}"]
    if tool == "add_acl_entry":
        entry = f"{a['seq']} {a['action']} {a['protocol']} {_endpoint(a['src'])} {_endpoint(a['dst'])}"
        if a.get("dst_port"):
            entry += f" {a['dst_port']}"
        return [f"ip access-list extended {a['acl_name']}", entry, "exit"]
    if tool == "apply_acl":
        return itf + [f"ip access-group {a['acl_name']} {a['direction']}", "exit"]
    if tool == "set_aaa_login":
        lines = ["aaa new-model"]
        if a.get("tacacs_key_ref"):
            lines.append(f"! TACACS+ key: <SECRET:{a['tacacs_key_ref']}>")
        return lines + [
            f"aaa authentication login {a['list_name']} " + " ".join(a["methods"]),
            "line vty 0 4",
            f"login authentication {a['list_name']}",
            "exit",
        ]
    raise ValueError("operation missing independent command contract")


def check_intent(configs: Mapping[str, str], approved: Iterable[ToolCall]) -> list[Finding]:
    expected: dict[str, list[str]] = {}
    for call in approved:
        expected.setdefault(call.device, []).extend(_commands(call))
    findings = []
    for device in sorted(set(expected) | set(configs)):
        wanted = expected.get(device, []) + ["end"]
        actual = [line.strip() for line in configs.get(device, "").split("\n") if line.strip()]
        if device not in expected or device not in configs or actual != wanted:
            findings.append(
                Finding(
                    "intent",
                    "INTENT_RENDER_MISMATCH",
                    "fail",
                    device,
                    None,
                    "Rendered commands differ from the approved operation contract.",
                )
            )
    return findings
