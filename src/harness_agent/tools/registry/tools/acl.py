"""nlcfg.registry.tools.acl — add_acl_entry, apply_acl"""

import ipaddress
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..base import BlastRadius, Role, ToolSpec

DEVICE_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
INTERFACE_PATTERN = r"^(GigabitEthernet|FastEthernet|Ethernet|Vlan)\d+(/\d+){0,2}$"
ACL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
ENDPOINT_PATTERN = r"^(any|host \d{1,3}(\.\d{1,3}){3}|\d{1,3}(\.\d{1,3}){3}/\d{1,2})$"
PORT_PATTERN = r"^(eq|gt|lt|neq) \d{1,5}$"


class Action(StrEnum):
    permit = "permit"
    deny = "deny"


class Protocol(StrEnum):
    tcp = "tcp"
    udp = "udp"
    ip = "ip"
    icmp = "icmp"


class Direction(StrEnum):
    inbound = "in"
    outbound = "out"


def _validate_endpoint(v: str) -> str:
    if v == "any":
        return v
    if v.startswith("host "):
        ipaddress.IPv4Address(v.removeprefix("host "))
        return v
    ipaddress.IPv4Network(v, strict=True)
    return v


# ---------------------------------------------------------------------------
# add_acl_entry
# ---------------------------------------------------------------------------
class AddAclEntryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    acl_name: str = Field(pattern=ACL_NAME_PATTERN)
    seq: int = Field(ge=1, le=2147483647)
    action: Action
    protocol: Protocol
    src: str = Field(pattern=ENDPOINT_PATTERN)
    dst: str = Field(pattern=ENDPOINT_PATTERN)
    dst_port: str | None = Field(default=None, pattern=PORT_PATTERN)

    @field_validator("src", "dst")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return _validate_endpoint(v)

    @model_validator(mode="after")
    def _check_port(self):
        # IOS chỉ nhận toán tử cổng với tcp/udp; cổng trong 0–65535 (review 01/10: `ip ... eq 99999` lọt qua).
        if self.dst_port is not None:
            if self.protocol not in (Protocol.tcp, Protocol.udp):
                raise ValueError("dst_port chỉ dùng với protocol tcp hoặc udp")
            if not 0 <= int(self.dst_port.split()[1]) <= 65535:
                raise ValueError("dst_port ngoài 0–65535")
        return self


ADD_ACL_ENTRY = ToolSpec(
    name="add_acl_entry",
    args_model=AddAclEntryArgs,
    min_role=Role.netops_engineer,
    template="acl_add_entry.j2",
    blast=lambda a: BlastRadius(devices=frozenset({a.device})),
    op_family="acl",
    description=(
        "Add one entry to a named extended ACL (created if it does not exist). "
        "Atomic — stages the change. Does not apply the ACL to any interface."
    ),
)


# ---------------------------------------------------------------------------
# apply_acl
# ---------------------------------------------------------------------------
class ApplyAclArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    acl_name: str = Field(pattern=ACL_NAME_PATTERN)
    direction: Direction


APPLY_ACL = ToolSpec(
    name="apply_acl",
    args_model=ApplyAclArgs,
    min_role=Role.security_admin,  # áp ACL vào interface là thay đổi rủi ro cao hơn thêm 1 dòng ACL
    template="acl_apply.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}), interfaces=frozenset({(a.device, a.interface)})
    ),
    op_family="acl",
    description="Apply a named ACL to an interface in a given direction. Atomic — stages the change.",
)

TOOLS = [ADD_ACL_ENTRY, APPLY_ACL]
