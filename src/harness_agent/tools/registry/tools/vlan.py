"""nlcfg.registry.tools.vlan — create_vlan"""

from pydantic import BaseModel, ConfigDict, Field

from ..base import BlastRadius, Role, ToolSpec

DEVICE_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
VLAN_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


class CreateVlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    # strict (D-053): LLM gửi "30", 30.0 hay true thì G2 từ chối, không ép kiểu.
    vlan_id: int = Field(ge=1, le=4094, strict=True)
    name: str = Field(pattern=VLAN_NAME_PATTERN)


def _blast(a: CreateVlanArgs) -> BlastRadius:
    return BlastRadius(devices=frozenset({a.device}), vlans=frozenset({(a.device, a.vlan_id)}))


CREATE_VLAN = ToolSpec(
    name="create_vlan",
    args_model=CreateVlanArgs,
    # Tạo VLAN ảnh hưởng cả switch, không chỉ một cổng: không cấp cho bậc thấp nhất (least privilege).
    min_role=Role.netops_engineer,
    template="vlan_create.j2",
    blast=_blast,
    op_family="vlan",
    description=(
        "Create a VLAN on a Cisco IOS device. Atomic — stages the change, "
        "does not execute it. Args: device, vlan_id (1-4094), name."
    ),
)

TOOLS = [CREATE_VLAN]
