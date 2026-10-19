"""nlcfg.registry.tools.interface — set_access_vlan, set_trunk, set_interface_ip,
set_interface_state, set_interface_description.

Lưu ý: bản nháp gốc trong tài liệu có regex interface hơi lỗi
(`r"^(GigabitEthernet|Ethernet)\\d+(/\\d+) {1,2}$"` — khoảng trắng thừa trước
`{1,2}` khiến nó không compile đúng ý). Đã sửa lại bên dưới thành
`(/\\d+){1,2}` để khớp cả `GigabitEthernet0/1` lẫn `GigabitEthernet0/0/1`.
"""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from ..base import BlastRadius, Role, ToolSpec
from ..ip_utils import validate_ipv4

DEVICE_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
INTERFACE_PATTERN = r"^(GigabitEthernet|FastEthernet|Ethernet)\d+(/\d+){1,2}$"
SAFE_TEXT = r"^[A-Za-z0-9 _.,:()/-]{0,80}$"  # không newline, không ký tự điều khiển


# ---------------------------------------------------------------------------
# set_access_vlan
# ---------------------------------------------------------------------------
class SetAccessVlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    # strict (D-053): LLM gửi "30", 30.0 hay true thì G2 từ chối, không ép kiểu.
    vlan_id: int = Field(ge=1, le=4094, strict=True)


SET_ACCESS_VLAN = ToolSpec(
    name="set_access_vlan",
    args_model=SetAccessVlanArgs,
    min_role=Role.noc_operator,
    template="interface_access_vlan.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}), interfaces=frozenset({(a.device, a.interface)})
    ),
    op_family="interface",
    description="Set an interface to access mode on the given VLAN. Atomic — stages the change.",
)


# ---------------------------------------------------------------------------
# set_trunk
# ---------------------------------------------------------------------------
class SetTrunkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    # strict (D-053): từng phần tử và native_vlan chỉ nhận số nguyên.
    allowed_vlans: list[StrictInt] = Field(min_length=1, max_length=64)
    native_vlan: int = Field(ge=1, le=4094, strict=True)

    @field_validator("allowed_vlans")
    @classmethod
    def _check_vlan_range(cls, v: list[int]) -> list[int]:
        for vid in v:
            if not (1 <= vid <= 4094):
                raise ValueError(f"VLAN id ngoài phạm vi hợp lệ: {vid}")
        return sorted(set(v))


SET_TRUNK = ToolSpec(
    name="set_trunk",
    args_model=SetTrunkArgs,
    min_role=Role.netops_engineer,
    template="interface_trunk.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}),
        interfaces=frozenset({(a.device, a.interface)}),
        vlans=frozenset((a.device, v) for v in a.allowed_vlans),
    ),
    op_family="interface",
    description=(
        "Configure an interface as an 802.1Q trunk with allowed VLANs and native VLAN. "
        "Atomic — stages the change."
    ),
)


# ---------------------------------------------------------------------------
# set_interface_ip
# ---------------------------------------------------------------------------
class SetInterfaceIpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    ip: str
    prefix_len: int = Field(ge=1, le=32)

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str) -> str:
        return validate_ipv4(v)


SET_INTERFACE_IP = ToolSpec(
    name="set_interface_ip",
    args_model=SetInterfaceIpArgs,
    min_role=Role.netops_engineer,
    template="interface_ip.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}), interfaces=frozenset({(a.device, a.interface)})
    ),
    op_family="interface",
    description="Assign an IPv4 address to an interface. Atomic — stages the change.",
)


# ---------------------------------------------------------------------------
# set_interface_state
# ---------------------------------------------------------------------------
class SetInterfaceStateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    enabled: bool


SET_INTERFACE_STATE = ToolSpec(
    name="set_interface_state",
    args_model=SetInterfaceStateArgs,
    min_role=Role.noc_operator,
    template="interface_state.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}), interfaces=frozenset({(a.device, a.interface)})
    ),
    op_family="interface",
    description="Enable (no shutdown) or disable (shutdown) an interface. Atomic — stages the change.",
)


# ---------------------------------------------------------------------------
# set_interface_description
# ---------------------------------------------------------------------------
class SetDescriptionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    interface: str = Field(pattern=INTERFACE_PATTERN)
    text: str = Field(pattern=SAFE_TEXT)


SET_DESCRIPTION = ToolSpec(
    name="set_interface_description",
    args_model=SetDescriptionArgs,
    min_role=Role.noc_operator,
    template="interface_description.j2",
    blast=lambda a: BlastRadius(
        devices=frozenset({a.device}), interfaces=frozenset({(a.device, a.interface)})
    ),
    op_family="interface",
    description="Set an interface's description text. Atomic — stages the change.",
)


TOOLS = [SET_ACCESS_VLAN, SET_TRUNK, SET_INTERFACE_IP, SET_INTERFACE_STATE, SET_DESCRIPTION]
