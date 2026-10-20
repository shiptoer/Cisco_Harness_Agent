"""Limit changes only where collector observations reveal missing semantics."""

from __future__ import annotations

from typing import Any


def observation_problem(device: dict[str, Any], tool: str, args: dict[str, Any]) -> str | None:
    obs = device.get("collected_security") or {}
    gaps = set(obs.get("unsupported", []))
    if not obs or tool == "set_interface_description":
        return None
    blocked = False
    if args.get("interface") in obs.get("vrf_interfaces", {}):
        blocked = True  # registry has no VRF parameter or VRF-aware state transitions
    if tool in {"add_static_route", "remove_static_route", "set_interface_ip"}:
        blocked |= bool(
            gaps
            & {
                "vrf_forwarding",
                "vrf_route",
                "vrf_definition",
                "route_option",
                "route_exit_interface",
                "secondary_address",
            }
        )
    if tool == "set_aaa_login":
        blocked |= any(g.startswith(("aaa_", "tacacs_", "vty_")) for g in gaps)
    if tool in {"add_acl_entry", "apply_acl"}:
        blocked |= any(g.startswith("acl_") and g != "acl_log" for g in gaps)
        acl = args.get("acl_name")
        blocked |= any(line.split()[0:2] == ["access-class", acl] for line in obs.get("vty", []))
    # New gap categories default to blocking until their affected domain is reviewed.
    known = {
        "banner_login",
        "route_name",
        "interface_name",
        "vrf_forwarding",
        "vrf_route",
        "vrf_definition",
        "route_option",
        "route_exit_interface",
        "secondary_address",
    }
    blocked |= any(g not in known and not g.startswith(("aaa_", "tacacs_", "vty_", "acl_")) for g in gaps)
    return "collector context has unsupported semantics for this operation" if blocked else None
