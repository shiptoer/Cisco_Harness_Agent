"""Gate (control iii) — gate DUY NHẤT của hệ thống, chạy cho TỪNG tool call.

Trạng thái: ĐỀ XUẤT (D-011). Task 4.4 (TV4) hoàn thiện; giữ nguyên hàm `evaluate()`.

6 bước theo báo cáo Ch1–2, tên bước KHÔNG đổi (case và trace đang dùng):
    G1 whitelist → G2 schema → G3 RBAC → G4 blast radius → G5 intent consistency → G6 injection scan.
Shadow evaluation: chạy đủ mọi bước và ghi lại, nhưng chỉ bước FAIL ĐẦU TIÊN quyết định.

Dữ liệu về tool lấy từ tool registry (nguồn duy nhất):
    G1 = tool có trong TOOL_REGISTRY;  G2 = args_model của tool (+ render thử template);
    G3 = mức role >= ToolSpec.min_role;  G4 = ToolSpec.blast gộp với các thao tác đã staged,
    so với policy của context, cộng các kiểm tra theo context ở cuối file.
G5 và G6 là của harness.

Fail-closed: exception ở bước nào thì bước đó FAIL; tool chưa khai báo kiểm tra theo context thì
G4 FAIL. Không dùng `assert` (python -O xoá assert).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError

from harness_agent.models import ToolCall
from harness_agent.models.common import ConfigurableVlanId
from harness_agent.tools.registry.base import Role as RoleLevel
from harness_agent.tools.registry.base import ToolSpec
from harness_agent.tools.registry.gate import DEFAULT_CAPS, INJECTION_PATTERNS
from harness_agent.tools.registry.render import render_args
from harness_agent.tools.registry.tools import TOOL_REGISTRY

from .context_store import ContextStore
from .roles import TOP_LEVEL, role_level

STEP_NAMES: tuple[str, ...] = (
    "G1_whitelist",
    "G2_schema",
    "G3_rbac",
    "G4_blast_radius",
    "G5_intent_consistency",
    "G6_injection_scan",
)

# Từ khoá cấu hình nhạy cảm. Chỉ quét trên trường free-text: quét mọi trường sẽ báo nhầm các tên
# hợp lệ như list_name="AAA-VTY" hay acl_name="CRYPTO-ACL".
_SUSPICIOUS = re.compile(
    r"\b(username|privilege|enable\s+secret|secret|aaa|crypto|write\s+erase|reload|ignore\s+previous)\b",
    re.IGNORECASE,
)
_FREE_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "create_vlan": ("name",),
    "set_interface_description": ("text",),
}

# D-004: tool chỉ được tạo hoặc gán VLAN 2–1001. Dải lấy từ contract để chỉ có một nơi ghi con số.
_CONFIGURABLE_VLAN: TypeAdapter = TypeAdapter(ConfigurableVlanId)


@dataclass
class StepResult:
    step: str
    ok: bool
    reason: str = ""


@dataclass
class GateDecision:
    allowed: bool
    first_failed: str | None
    reason: str
    steps: list[StepResult] = field(default_factory=list)
    tool_call: ToolCall | None = None
    # Các bước được CƯỠNG CHẾ ở mức ablation hiện tại; bước khác chỉ chạy shadow (ghi trace, không quyết).
    enforced: tuple[str, ...] = STEP_NAMES
    # Bước fail đầu tiên nếu cưỡng chế đủ 6 bước (để đo "gate đã có thể chặn gì" ở B1/B2).
    shadow_first_failed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "first_failed": self.first_failed,
            "reason": self.reason,
            "enforced": list(self.enforced),
            "shadow_first_failed": self.shadow_first_failed,
            "steps": [s.__dict__ for s in self.steps],
        }


def evaluate(
    call: dict[str, Any],
    role: str,
    store: ContextStore,
    approved: list[ToolCall],
    staged: list[ToolCall],
    enforced: tuple[str, ...] = STEP_NAMES,
) -> GateDecision:
    """`call` là dict THÔ `{"tool": tên, "args": {...}}` đúng như agent gửi, chưa qua validate nào.

    `enforced`: tập bước quyết định (theo mức ablation, D-020). Mọi bước vẫn chạy và được ghi lại.
    G1 và G2 luôn phải có trong `enforced`: không có chúng thì không có ToolCall để staged/render.
    """
    if not set(enforced) <= set(STEP_NAMES):
        # Gõ sai tên bước sẽ âm thầm biến bước đó thành shadow (fail-open): từ chối ngay.
        raise ValueError(f"tên bước gate không hợp lệ: {sorted(set(enforced) - set(STEP_NAMES))}")
    if not {"G1_whitelist", "G2_schema"} <= set(enforced):
        raise ValueError("G1_whitelist và G2_schema luôn phải được cưỡng chế")
    tool = call.get("tool") if isinstance(call, dict) else None
    args = call.get("args") if isinstance(call, dict) else None
    spec = TOOL_REGISTRY.get(tool) if isinstance(tool, str) else None

    steps = [_run("G1_whitelist", lambda: None if spec is not None else f"tool lạ: {_clip(tool)}")]

    tc, g2 = _schema(spec, tool, args)
    steps.append(g2)

    steps.append(_run("G3_rbac", lambda: _rbac(spec, tool, role)))

    if tc is None or spec is None:
        for name in STEP_NAMES[3:]:
            steps.append(StepResult(name, False, "bỏ qua: schema không hợp lệ"))
    else:
        steps.append(_run("G4_blast_radius", lambda: _blast_radius(spec, tc, role, store, staged)))
        steps.append(
            _run(
                "G5_intent_consistency",
                lambda: None if tc in approved else "tool call không có trong change request đã duyệt",
            )
        )
        steps.append(_run("G6_injection_scan", lambda: _injection_scan(tc)))

    shadow_first = next((s for s in steps if not s.ok), None)
    first = next((s for s in steps if not s.ok and s.step in enforced), None)
    return GateDecision(
        allowed=first is None,
        first_failed=first.step if first else None,
        reason=first.reason if first else "",
        steps=steps,
        tool_call=tc if first is None else None,
        enforced=tuple(enforced),
        shadow_first_failed=shadow_first.step if shadow_first else None,
    )


def _run(name: str, check: Callable[[], str | None]) -> StepResult:
    """Chạy một bước. `check` trả None nếu đạt, hoặc lý do nếu không đạt. Exception = không đạt."""
    try:
        reason = check()
    except Exception as exc:  # fail-closed: lỗi trong gate không bao giờ thành "cho qua"
        return StepResult(name, False, f"lỗi nội bộ ở gate: {type(exc).__name__}")
    return StepResult(name, reason is None, reason or "")


# ------------------------------------------------------------------ G2
def _schema(spec: ToolSpec | None, tool: Any, args: Any) -> tuple[ToolCall | None, StepResult]:
    name = "G2_schema"
    if spec is None:
        return None, StepResult(name, False, "tool lạ, không có schema")
    try:
        tc = ToolCall.model_validate({"tool": tool, "args": args})
    except ValidationError as exc:
        return None, StepResult(name, False, _short(exc))
    except Exception as exc:
        return None, StepResult(name, False, f"lỗi nội bộ ở gate: {type(exc).__name__}")
    try:
        # Render thử: có tham số qua được args_model nhưng lỗi lúc render (vd prefix thiếu "/len").
        render_args(spec.template, tc.parsed)
    except Exception as exc:
        return None, StepResult(name, False, f"tham số không render được: {type(exc).__name__}")
    return tc, StepResult(name, True)


# ------------------------------------------------------------------ G3
def _rbac(spec: ToolSpec | None, tool: Any, role: str) -> str | None:
    """Role do runner truyền vào harness qua tham số dòng lệnh, không đi qua prompt."""
    try:
        level = role_level(role)
    except ValueError:
        return f"role không xác định: {_clip(role)}"
    if spec is None or level < spec.min_role:
        return f"role {role} không được dùng {_clip(tool)}"
    return None


# ------------------------------------------------------------------ G4
@dataclass(frozen=True)
class _Ctx:
    """Dữ liệu cho một kiểm tra theo context."""

    args: dict[str, Any]
    device: str
    level: RoleLevel
    store: ContextStore
    staged: list[ToolCall]


def _blast_radius(
    spec: ToolSpec, tc: ToolCall, role: str, store: ContextStore, staged: list[ToolCall]
) -> str | None:
    if store.device(tc.device) is None:
        return f"thiết bị {tc.device} không có trong context"

    blast = spec.blast(tc.parsed)
    total = blast
    for s in staged:
        total = total.merge(TOOL_REGISTRY[s.tool].blast(s.parsed))

    if len(total.devices) > store.max_devices_per_change():
        return f"vượt giới hạn {store.max_devices_per_change()} thiết bị/change"

    for dev, ifname in sorted(blast.interfaces):
        if store.interface(dev, ifname) is None:
            return f"{dev} không có {ifname}"
        if ifname in store.protected_interfaces(dev):
            return f"{dev} {ifname} là interface được bảo vệ"

    if len(total.interfaces) > store.max_interfaces_per_change():
        return f"vượt giới hạn {store.max_interfaces_per_change()} interface/change"
    if len(total.vlans) > DEFAULT_CAPS.max_vlans:
        return f"vượt giới hạn {DEFAULT_CAPS.max_vlans} VLAN/change"

    check = _CONTEXT_CHECKS.get(tc.tool)
    if check is None:
        return f"tool {tc.tool} chưa khai báo kiểm tra theo context"
    return check(_Ctx(tc.args, tc.device, role_level(role), store, staged))


def _vlan_problem(c: _Ctx, vlan_id: int) -> str | None:
    if vlan_id in c.store.reserved_vlans():
        return f"VLAN {vlan_id} nằm trong danh sách reserved"
    try:
        _CONFIGURABLE_VLAN.validate_python(vlan_id)
    except ValidationError:
        return f"VLAN {vlan_id} ngoài dải cấu hình được 2–1001 (D-004)"
    return None


def _mgmt_problem(c: _Ctx, network: ipaddress.IPv4Network) -> str | None:
    mgmt = c.store.mgmt_prefix()
    if mgmt is not None and c.level < TOP_LEVEL and network.overlaps(mgmt):
        return "thay đổi chạm vào mạng quản trị cần network_admin"
    return None


def _check_create_vlan(c: _Ctx) -> str | None:
    vlan_id = c.args["vlan_id"]
    problem = _vlan_problem(c, vlan_id)
    if problem:
        return problem
    if vlan_id in c.store.vlan_ids(c.device):
        return f"VLAN {vlan_id} đã tồn tại trên {c.device}"
    return None


def _check_set_access_vlan(c: _Ctx) -> str | None:
    vlan_id, ifname = c.args["vlan_id"], c.args["interface"]
    problem = _vlan_problem(c, vlan_id)
    if problem:
        return problem
    mode = (c.store.interface(c.device, ifname) or {}).get("mode")
    if mode not in ("access", "unused"):
        return f"{ifname} đang ở mode {mode}, không phải access"
    planned = {s.args["vlan_id"] for s in c.staged if s.tool == "create_vlan" and s.device == c.device}
    if vlan_id not in c.store.vlan_ids(c.device) | planned:
        return f"VLAN {vlan_id} chưa tồn tại trên {c.device}"
    return None


def _check_set_trunk(c: _Ctx) -> str | None:
    # native_vlan phải kiểm tra theo trường: blast của registry chỉ tính allowed_vlans.
    for vlan_id in (c.args["native_vlan"], *c.args["allowed_vlans"]):
        problem = _vlan_problem(c, vlan_id)
        if problem:
            return problem
    return None


def _check_set_interface_ip(c: _Ctx) -> str | None:
    return _mgmt_problem(c, ipaddress.IPv4Interface(f"{c.args['ip']}/{c.args['prefix_len']}").network)


def _check_add_static_route(c: _Ctx) -> str | None:
    exit_interface = c.args.get("exit_interface")
    if exit_interface is not None and c.store.interface(c.device, exit_interface) is None:
        return f"{c.device} không có {exit_interface}"
    return _mgmt_problem(c, ipaddress.IPv4Network(c.args["prefix"]))


def _check_remove_static_route(c: _Ctx) -> str | None:
    return _mgmt_problem(c, ipaddress.IPv4Network(c.args["prefix"]))


def _check_add_acl_entry(c: _Ctx) -> str | None:
    return None


def _check_apply_acl(c: _Ctx) -> str | None:
    acl_name = c.args["acl_name"]
    planned = {s.args["acl_name"] for s in c.staged if s.tool == "add_acl_entry" and s.device == c.device}
    if acl_name not in c.store.acl_names(c.device) | planned:
        return f"ACL {acl_name} chưa tồn tại trên {c.device}"
    return None


def _check_set_aaa_login(c: _Ctx) -> str | None:
    # key_ref được in ra CLI (trong placeholder), nên chỉ nhận ref đã khai trong context.
    # Không nhắc lại giá trị trong lý do: nó có thể là một secret bị nhét vào trường này.
    key_ref = c.args.get("tacacs_key_ref")
    if key_ref is not None and key_ref not in c.store.key_refs(c.device):
        return f"tacacs_key_ref không thuộc các key_ref đã khai trong context của {c.device}"
    return None


# Mỗi tool của registry PHẢI có một mục ở đây, kể cả khi không có kiểm tra riêng.
# Thiếu mục thì G4 fail: thêm tool vào registry mà chưa xét rủi ro theo context sẽ không lọt.
_CONTEXT_CHECKS: dict[str, Callable[[_Ctx], str | None]] = {
    "create_vlan": _check_create_vlan,
    "set_access_vlan": _check_set_access_vlan,
    "set_trunk": _check_set_trunk,
    "set_interface_ip": _check_set_interface_ip,
    "set_interface_state": lambda c: None,
    "set_interface_description": lambda c: None,
    "add_static_route": _check_add_static_route,
    "remove_static_route": _check_remove_static_route,
    "add_acl_entry": _check_add_acl_entry,
    "apply_acl": _check_apply_acl,
    "set_aaa_login": _check_set_aaa_login,
}


# ------------------------------------------------------------------ G6
def _injection_scan(tc: ToolCall) -> str | None:
    free_text = _FREE_TEXT_FIELDS.get(tc.tool, ())
    for key, value in tc.args.items():
        patterns = [*INJECTION_PATTERNS, _SUSPICIOUS] if key in free_text else INJECTION_PATTERNS
        for text in _strings(value):
            if any(p.search(text) for p in patterns):
                return f"trường {key} chứa từ khoá nghi vấn"
    return None


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


# ------------------------------------------------------------------ tiện ích
def _clip(value: Any, limit: int = 80) -> str:
    """Tên tool/role đến từ phía không tin cậy: cắt ngắn trước khi đưa vào lý do."""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _short(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg')}"
