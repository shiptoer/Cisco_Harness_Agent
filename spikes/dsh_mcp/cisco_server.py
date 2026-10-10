"""Stdio MCP server for the dsh spike: one read-only tool, `get_device(hostname)`.

The model only ever sees a whitelisted projection of a Device. `project_device` builds the output
from the allowed fields instead of copying the Device and deleting sensitive ones, so a field added
to the contract later stays hidden until it is added here. Excluded on purpose:
- `local_users` (holds `secret_hash`), `mgmt_ip`, `vlans`, `static_routes`, `allowed_vlans`, `ipv4`;
- `banner` and interface `description`: UNTRUSTED free text (indirect prompt injection, attack A2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from harness_agent.models import Device, NetworkContext

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_PATH = REPO_ROOT / "examples" / "valid" / "network_context.json"


class UnknownDeviceError(LookupError):
    """No device with the requested hostname exists in the context."""


def load_context(path: Path = CONTEXT_PATH) -> NetworkContext:
    return NetworkContext.model_validate_json(path.read_text(encoding="utf-8"))


def project_device(device: Device) -> dict[str, Any]:
    """Return only hostname, platform, role and interfaces (name, mode, access_vlan, enabled)."""
    return {
        "hostname": device.hostname,
        "platform": device.platform.value,
        "role": device.role.value,
        "interfaces": [
            {
                "name": i.name,
                "mode": i.mode.value,
                "access_vlan": i.access_vlan,
                "enabled": i.enabled,
            }
            for i in device.interfaces
        ],
    }


def lookup_device(context: NetworkContext, hostname: str) -> dict[str, Any]:
    device = context.get_device(hostname)
    if device is None:
        # repr() + cap: the hostname comes from the model, keep the echo bounded and inert.
        raise UnknownDeviceError(f"unknown hostname {hostname[:64]!r}: no such device in the context")
    return project_device(device)


CONTEXT = load_context()
server = MCPServer(name="cisco")


@server.tool()
def get_device(hostname: str) -> dict[str, Any]:
    """Read one device from the network context: hostname, platform, role and its interfaces
    (name, mode, access_vlan, enabled). Hostnames are exact and case-sensitive, e.g. SW-ACC-01."""
    try:
        return lookup_device(CONTEXT, hostname)
    except UnknownDeviceError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    server.run(transport="stdio")
