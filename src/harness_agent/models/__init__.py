"""Contract dùng chung của cả 4 luồng. Sửa file trong thư mục này phải qua issue + DECISIONS.md."""

from .change_request import ChangeRequest
from .common import DeviceRole, InterfaceMode, Platform, Role, Verdict
from .context import (
    SCHEMA_VERSION,
    Aaa,
    AaaLocalUser,
    AclEntry,
    Device,
    Interface,
    Link,
    NetworkContext,
    Policy,
    StaticRoute,
    TacacsServer,
    Vlan,
)
from .invariants import INVARIANT_CODES, Violation, check_invariants
from .operations import TOOL_NAMES, ToolCall, parse_tool_call
from .testcase import (
    Actor,
    AttackLabel,
    ConfigType,
    ContextRef,
    ContextRefError,
    Subset,
    Technique,
    TestCase,
    load_context,
)

__all__ = [
    "INVARIANT_CODES",
    "SCHEMA_VERSION",
    "TOOL_NAMES",
    "Aaa",
    "AaaLocalUser",
    "AclEntry",
    "Actor",
    "AttackLabel",
    "ChangeRequest",
    "ConfigType",
    "ContextRef",
    "ContextRefError",
    "Device",
    "DeviceRole",
    "Interface",
    "InterfaceMode",
    "Link",
    "NetworkContext",
    "Platform",
    "Policy",
    "Role",
    "StaticRoute",
    "Subset",
    "TacacsServer",
    "Technique",
    "TestCase",
    "ToolCall",
    "Verdict",
    "Violation",
    "Vlan",
    "check_invariants",
    "load_context",
    "parse_tool_call",
]
