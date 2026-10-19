"""nlcfg.registry.tools.route — add_static_route, remove_static_route

`prefix` phải là CIDR có "/len" (`A.B.C.D/len`, host bit tắt). Trước đây "10.0.0.0" qua được args_model
(IPv4Network coi là /32) rồi mới lỗi lúc render (`split("/")[1]`); harness phải render thử ở G2 để chặn.
Nay chặn ngay trong args_model (mục 7.3 của docs/design/registry-mcp-integration.md). Render thử ở G2
vẫn giữ làm lớp phòng thủ sau cùng.
"""

import ipaddress

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..base import BlastRadius, Role, ToolSpec
from ..ip_utils import prefix_to_netmask, validate_ipv4

DEVICE_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
INTERFACE_PATTERN = r"^(GigabitEthernet|FastEthernet|Ethernet)\d+(/\d+){1,2}$"
CIDR_PATTERN = r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$"


def _validate_cidr(v: str) -> str:
    # Pattern đã ép có "/len"; IPv4Network kiểm tra octet, độ dài prefix và host bit (strict).
    ipaddress.IPv4Network(v, strict=True)
    return v


def _network(prefix: str) -> str:
    return str(ipaddress.IPv4Network(prefix, strict=True).network_address)


def _netmask(prefix: str) -> str:
    return prefix_to_netmask(int(prefix.split("/")[1]))


# ---------------------------------------------------------------------------
# add_static_route — đúng một trong hai: next_hop HOẶC exit_interface
# ---------------------------------------------------------------------------
class AddStaticRouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    prefix: str = Field(pattern=CIDR_PATTERN)  # CIDR, vd "192.168.10.0/24"
    next_hop: str | None = None
    exit_interface: str | None = Field(default=None, pattern=INTERFACE_PATTERN)

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        return _validate_cidr(v)

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if bool(self.next_hop) == bool(self.exit_interface):
            raise ValueError("Cần cung cấp đúng một trong hai: next_hop hoặc exit_interface")
        if self.next_hop:
            validate_ipv4(self.next_hop)
        return self

    @property
    def network(self) -> str:
        return _network(self.prefix)

    @property
    def netmask(self) -> str:
        return _netmask(self.prefix)


ADD_STATIC_ROUTE = ToolSpec(
    name="add_static_route",
    args_model=AddStaticRouteArgs,
    min_role=Role.netops_engineer,
    template="route_add.j2",
    blast=lambda a: BlastRadius(devices=frozenset({a.device})),
    op_family="route",
    description=(
        "Add a static route on a device. prefix is CIDR (A.B.C.D/len). Exactly one of next_hop or "
        "exit_interface must be given. Atomic — stages the change."
    ),
)


# ---------------------------------------------------------------------------
# remove_static_route
# ---------------------------------------------------------------------------
class RemoveStaticRouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    prefix: str = Field(pattern=CIDR_PATTERN)
    next_hop: str

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        return _validate_cidr(v)

    @field_validator("next_hop")
    @classmethod
    def _check_next_hop(cls, v: str) -> str:
        return validate_ipv4(v)

    @property
    def network(self) -> str:
        return _network(self.prefix)

    @property
    def netmask(self) -> str:
        return _netmask(self.prefix)


REMOVE_STATIC_ROUTE = ToolSpec(
    name="remove_static_route",
    args_model=RemoveStaticRouteArgs,
    min_role=Role.netops_engineer,
    template="route_remove.j2",
    blast=lambda a: BlastRadius(devices=frozenset({a.device})),
    op_family="route",
    description=(
        "Remove a static route on a device. prefix is CIDR (A.B.C.D/len). Atomic — stages the change."
    ),
)

TOOLS = [ADD_STATIC_ROUTE, REMOVE_STATIC_ROUTE]
