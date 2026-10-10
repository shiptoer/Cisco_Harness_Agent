"""nlcfg.registry.tools — gom toàn bộ ToolSpec thành một registry duy nhất."""

from ..base import ToolSpec
from . import aaa, acl, interface, route, vlan

_ALL_TOOLS: list[ToolSpec] = [
    *vlan.TOOLS,
    *interface.TOOLS,
    *route.TOOLS,
    *acl.TOOLS,
    *aaa.TOOLS,
]

TOOL_REGISTRY: dict[str, ToolSpec] = {t.name: t for t in _ALL_TOOLS}

if len(TOOL_REGISTRY) != len(_ALL_TOOLS):
    raise RuntimeError("Trùng tên tool trong registry!")
if len(TOOL_REGISTRY) != 11:
    raise RuntimeError(f"Kỳ vọng đúng 11 tool v1, hiện có {len(TOOL_REGISTRY)}")
