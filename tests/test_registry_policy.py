"""Khoá các thuộc tính của tool registry mà harness dựa vào (task 3.3).

Registry là nguồn duy nhất về tool: tên, tham số, min_role, blast radius. Đổi các thuộc tính
dưới đây phải là thay đổi có chủ đích, kèm sửa test này.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from registry_samples import EXPECTED_TOOLS, VALID_ARGS

from harness_agent.tools.registry.base import Role
from harness_agent.tools.registry.tools import TOOL_REGISTRY


def test_registry_has_exactly_the_expected_tools():
    """Thêm hay bớt tool phải sửa test này: mỗi tool mới cần luật gate và template riêng."""
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS
    assert set(VALID_ARGS) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_args_models_forbid_extra_and_are_frozen(tool: str):
    model = TOOL_REGISTRY[tool].args_model
    args = model.model_validate(VALID_ARGS[tool])
    with pytest.raises(ValidationError):
        model.model_validate({**VALID_ARGS[tool], "privilege": 15})
    # frozen (D-001): object đã qua gate không bị sửa trước khi render.
    with pytest.raises(ValidationError):
        args.device = "R9"  # type: ignore[misc]


def test_every_tool_needs_more_than_viewer():
    """Đối chứng cho RBAC: không tool nào có min_role bằng 0 (mức của viewer)."""
    assert all(spec.min_role >= Role.noc_operator for spec in TOOL_REGISTRY.values())


def test_create_vlan_is_not_available_to_lowest_operator_level():
    assert TOOL_REGISTRY["create_vlan"].min_role > Role.noc_operator
    assert TOOL_REGISTRY["set_access_vlan"].min_role == Role.noc_operator
