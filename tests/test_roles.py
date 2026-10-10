"""Role: một bảng ánh xạ duy nhất từ tên bên ngoài sang mức quyền của registry (task 3.3, D-010)."""

from __future__ import annotations

import pytest

from harness_agent.harness import roles
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import Role as RoleName
from harness_agent.tools.registry.base import Role as RoleLevel
from harness_agent.tools.registry.tools import TOOL_REGISTRY


def test_mapping_covers_exactly_the_contract_roles():
    assert set(roles.CLI_ROLE_LEVEL) == {r.value for r in RoleName}
    assert roles.role_level("viewer") == RoleLevel.viewer == 0
    assert roles.role_level("access_operator") == RoleLevel.noc_operator
    # D-010 (nhóm chốt 06/10/2026): network_admin xuống bậc 2; security_admin là bậc cao nhất.
    assert roles.role_level("network_admin") == RoleLevel.netops_engineer
    assert roles.role_level("security_admin") == RoleLevel.security_admin == roles.TOP_LEVEL


@pytest.mark.parametrize(
    "bad",
    ["admin", "root", "", "NETWORK_ADMIN", "SECURITY_ADMIN", "netops_engineer", "noc_operator", None, 3],
)
def test_unknown_role_raises_instead_of_defaulting(bad):
    """Fail-closed: tên lạ (kể cả tên thành viên enum của registry) không được nhận một mức nào."""
    with pytest.raises(ValueError, match="role không xác định"):
        roles.role_level(bad)


def test_viewer_is_below_every_tool():
    assert all(roles.role_level("viewer") < spec.min_role for spec in TOOL_REGISTRY.values())


def test_security_admin_reaches_every_tool():
    assert all(roles.role_level("security_admin") >= spec.min_role for spec in TOOL_REGISTRY.values())


def test_network_admin_misses_exactly_the_level_3_tools():
    missed = {
        name for name, spec in TOOL_REGISTRY.items() if roles.role_level("network_admin") < spec.min_role
    }
    assert missed == {"apply_acl", "set_aaa_login"}


def test_unknown_role_fails_g3(tmp_path):
    from pathlib import Path

    store = ContextStore.load(Path(__file__).resolve().parents[1] / "data/contexts/campus-small-01.json")
    call = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
    d = evaluate(call, "root", store, approved=[], staged=[])
    g3 = next(s for s in d.steps if s.step == "G3_rbac")
    assert not d.allowed and not g3.ok and "không xác định" in g3.reason
