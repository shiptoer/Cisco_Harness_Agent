"""Role: ánh xạ DUY NHẤT từ tên role bên ngoài sang mức quyền của tool registry.

- Tên bên ngoài (chuỗi của `--role`, nhãn `role` trong corpus): `harness_agent.models.Role`.
- Mức quyền (so sánh với `ToolSpec.min_role`): `harness_agent.tools.registry.base.Role`.

Role do runner truyền thẳng vào harness qua tham số dòng lệnh, KHÔNG bao giờ đi qua prompt.
Chuỗi lạ thì raise: không có mức mặc định (fail-closed). Không dùng `assert` (D-007).

Bảng 4 role theo D-010 (nhóm chốt 06/10/2026): viewer(0), access_operator → noc_operator(1),
network_admin → netops_engineer(2), security_admin → security_admin(3). Thay đổi chạm mạng quản trị hoặc AAA
(`apply_acl`, `set_aaa_login`, route/IP chạm `mgmt_prefix`) cần security_admin.
"""

from __future__ import annotations

from harness_agent.models import Role as RoleName
from harness_agent.tools.registry.base import Role as RoleLevel

CLI_ROLE_LEVEL: dict[str, RoleLevel] = {
    RoleName.VIEWER.value: RoleLevel.viewer,
    RoleName.ACCESS_OPERATOR.value: RoleLevel.noc_operator,
    RoleName.NETWORK_ADMIN.value: RoleLevel.netops_engineer,
    RoleName.SECURITY_ADMIN.value: RoleLevel.security_admin,
}

if set(CLI_ROLE_LEVEL) != {r.value for r in RoleName}:
    raise RuntimeError("bảng CLI_ROLE_LEVEL phải có đúng các role của contract (models.Role)")

# Dùng cho `choices` của argparse ở harness server và runner.
ROLE_NAMES: tuple[str, ...] = tuple(sorted(CLI_ROLE_LEVEL))

# Mức cao nhất: cần cho thay đổi chạm vào mạng quản trị.
TOP_LEVEL: RoleLevel = max(RoleLevel)


def role_level(name: str) -> RoleLevel:
    """Tên role bên ngoài → mức quyền. Tên lạ thì raise ValueError."""
    if not isinstance(name, str) or name not in CLI_ROLE_LEVEL:
        raise ValueError(f"role không xác định: {name!r}")
    return CLI_ROLE_LEVEL[name]
