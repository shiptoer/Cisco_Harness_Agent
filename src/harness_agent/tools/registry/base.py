"""
nlcfg.registry.base
====================
Các kiểu dữ liệu lõi dùng chung cho toàn bộ tool: BlastRadius, Role (thứ bậc
quyền), ToolSpec (mô tả một tool nguyên tử), StagedOp/ChangeSet (staged
change set — gọi tool KHÔNG render CLI ngay, chỉ đưa thao tác vào hàng đợi),
và KeyRef (tham chiếu secret, không bao giờ chứa giá trị thật).

File này KHÔNG phụ thuộc gói `mcp` — để logic lõi không vỡ khi SDK MCP đổi API.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Role hierarchy — role của người gọi do harness cấp (theo phiên đăng nhập),
# KHÔNG BAO GIỜ đọc từ văn bản do model sinh ra.
# ---------------------------------------------------------------------------
class Role(IntEnum):
    viewer = 0  # chỉ đọc: thấp hơn min_role của mọi tool
    noc_operator = 1
    netops_engineer = 2
    security_admin = 3

    @classmethod
    def parse(cls, value: str | Role) -> Role:
        if isinstance(value, Role):
            return value
        try:
            return cls[value]
        except KeyError as exc:
            raise ValueError(f"Role không hợp lệ: {value!r}") from exc


# ---------------------------------------------------------------------------
# KeyRef — LLM và tool KHÔNG BAO GIỜ thấy giá trị secret thật.
# ---------------------------------------------------------------------------
# Dùng fullmatch, không dùng match với "^...$": "$" của Python khớp trước một "\n" ở cuối chuỗi.
KEY_REF_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class KeyRef(BaseModel):
    """Tham chiếu tới một secret lưu trong vault/secret-manager bên ngoài.
    Chỉ lưu id tham chiếu — không lưu giá trị. Render ra placeholder cho
    người duyệt điền tay, hoặc để pipeline CI/CD bơm vào lúc deploy."""

    ref: str

    def __init__(self, ref: str, **kwargs):
        if not KEY_REF_PATTERN.fullmatch(ref):
            raise ValueError(f"key_ref không hợp lệ: {ref!r}")
        super().__init__(ref=ref, **kwargs)

    def placeholder(self) -> str:
        return f"${{VAULT:{self.ref}}}"


# ---------------------------------------------------------------------------
# BlastRadius — đo "vùng ảnh hưởng" của một thao tác, để gate chặn thay đổi
# vượt quá ngưỡng cho phép trong MỘT change set.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BlastRadius:
    devices: frozenset[str] = frozenset()
    interfaces: frozenset[tuple[str, str]] = frozenset()  # (device, interface)
    vlans: frozenset[tuple[str, int]] = frozenset()  # (device, vlan_id)

    def merge(self, other: BlastRadius) -> BlastRadius:
        return BlastRadius(
            devices=self.devices | other.devices,
            interfaces=self.interfaces | other.interfaces,
            vlans=self.vlans | other.vlans,
        )

    @staticmethod
    def empty() -> BlastRadius:
        return BlastRadius()


# ---------------------------------------------------------------------------
# ToolSpec — mô tả tĩnh một tool nguyên tử.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    min_role: Role
    template: str  # tên file .j2 trong nlcfg/templates/
    blast: Callable[[BaseModel], BlastRadius]
    op_family: str  # dùng để nhóm log/metrics: "vlan" | "interface" | "route" | "acl" | "aaa"
    description: str  # mô tả cho model — trở thành `description` của MCP tool


# ---------------------------------------------------------------------------
# StagedOp / ChangeSet — gọi tool = thêm StagedOp vào ChangeSet.
# CLI CHỈ được render sau khi to_change_set() qua gate (xem gate.py).
# ---------------------------------------------------------------------------
@dataclass
class StagedOp:
    op_id: str
    tool_name: str
    args: BaseModel
    blast: BlastRadius

    @property
    def spec(self) -> ToolSpec:
        from .tools import TOOL_REGISTRY  # import trễ để tránh vòng lặp import

        return TOOL_REGISTRY[self.tool_name]


@dataclass
class ChangeSet:
    """Một phiên staging — tương ứng một session dsh. KHÔNG lưu trạng thái
    giữa các process khác nhau; nếu cần bền vững, serialize ra file/DB riêng."""

    session_id: str
    caller_role: Role
    ops: list[StagedOp] = field(default_factory=list)

    def stage(self, tool_name: str, args: BaseModel, blast: BlastRadius) -> StagedOp:
        op = StagedOp(op_id=str(uuid.uuid4())[:8], tool_name=tool_name, args=args, blast=blast)
        self.ops.append(op)
        return op

    def discard(self, op_id: str) -> bool:
        before = len(self.ops)
        self.ops = [o for o in self.ops if o.op_id != op_id]
        return len(self.ops) < before

    def clear(self) -> None:
        self.ops.clear()

    def aggregate_blast(self) -> BlastRadius:
        total = BlastRadius.empty()
        for op in self.ops:
            total = total.merge(op.blast)
        return total

    def is_empty(self) -> bool:
        return len(self.ops) == 0
