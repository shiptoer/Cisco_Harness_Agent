"""Kiểu dùng chung cho toàn bộ contract.

Nguyên tắc bảo mật áp dụng cho MỌI model trong package này:
- extra="forbid": trường lạ (ví dụ LLM tự bịa thêm "privilege": 15) bị reject, không bị bỏ qua âm thầm.
- frozen=True: object đã validate thì không bị sửa sau đó, tránh TOCTOU giữa gate và render.
- Chuỗi đi vào CLI (tên VLAN, description) bị cấm ký tự điều khiển, đặc biệt là xuống dòng,
  vì một ký tự "\n" trong description đủ để chèn thêm lệnh vào cấu hình đã render.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    """Base cho mọi model của contract."""

    # hide_input_in_errors: lỗi validate không chép giá trị đầu vào vào thông báo lỗi. Đầu vào có thể là
    # secret của context hoặc output của LLM chứa secret; thông báo lỗi đi vào trace, rejects.jsonl, log.
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=False, hide_input_in_errors=True
    )


# ---------- Enum ----------


class Role(StrEnum):
    """Role của người gửi intent. Được runner truyền thẳng vào harness, KHÔNG đi qua prompt."""

    VIEWER = "viewer"
    ACCESS_OPERATOR = "access_operator"
    NETWORK_ADMIN = "network_admin"
    SECURITY_ADMIN = "security_admin"  # thay đổi chạm mạng quản trị hoặc AAA (D-010, 06/10/2026)


class Verdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    CLARIFY = "clarify"


class DeviceRole(StrEnum):
    """Vai trò thiết bị: đúng các giá trị của file context thật (D-042; nhóm chốt ngày 08/10/2026)."""

    ROUTER = "router"
    DISTRIBUTION_SWITCH = "distribution_switch"
    ACCESS_SWITCH = "access_switch"


class Platform(StrEnum):
    IOS = "ios"
    IOSV = "iosv"  # router image trên EVE-NG
    IOSVL2 = "iosvl2"  # switch image trên EVE-NG (cần cho VLAN/switchport)


class InterfaceMode(StrEnum):
    ACCESS = "access"
    TRUNK = "trunk"
    ROUTED = "routed"
    UNUSED = "unused"  # cổng không dùng: tắt, có thể nằm trong VLAN parking


# ---------- Chuỗi có ràng buộc ----------

Hostname = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,62}$")]

# Tên đầy đủ kiểu Cisco. Viết tắt (Gi0/1) phải được chuẩn hoá trước khi vào contract.
InterfaceName = Annotated[
    str,
    StringConstraints(pattern=r"^(GigabitEthernet|FastEthernet|Ethernet|Loopback|Vlan)\d+(/\d+){0,2}$"),
]

# Tên VLAN: chỉ chữ, số, _ và -. Không khoảng trắng, không ký tự điều khiển.
VlanName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]

# Free text (description, banner): cho phép khoảng trắng và dấu câu nhưng cấm mọi ký tự điều khiển.
# Lưu ý: nội dung free text vẫn là UNTRUSTED dù đã qua ràng buộc này (xem control ii).
# Cấm: C0 (\x00-\x1f), DEL và C1 (\x7f-\x9f, gồm NEL \x85), LINE/PARAGRAPH SEPARATOR (\u2028, \u2029).
# Python `str.splitlines()` tách dòng ở cả \x85, \u2028, \u2029 (D-003, bổ sung 06/10/2026).
FREE_TEXT_PATTERN = r"^[^\x00-\x1f\x7f-\x9f\u2028\u2029]{0,240}$"
FreeText = Annotated[str, StringConstraints(pattern=FREE_TEXT_PATTERN)]

# VLAN hợp lệ để cấu hình qua tool: 2–1001 (loại VLAN 1 và dải extended/reserved 1002–4094).
ConfigurableVlanId = Annotated[int, Field(ge=2, le=1001)]
# VLAN có thể xuất hiện trong trạng thái mạng (đọc được), rộng hơn dải cấu hình được. strict (D-053, ĐỀ XUẤT):
# chỉ nhận số nguyên, không ép "30", 30.0 hay true (true từng thành VLAN 1).
AnyVlanId = Annotated[int, Field(ge=1, le=4094, strict=True)]
