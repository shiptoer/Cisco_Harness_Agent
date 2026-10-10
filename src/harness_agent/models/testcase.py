"""TestCase: một mẫu trong corpus. Nhãn (expected_*) do bộ sinh deterministic tạo, KHÔNG do LLM.

Case không nhúng network context: nó trỏ tới file context bằng `ContextRef` (đường dẫn + SHA-256), nạp bằng
`load_context`. Secret của context vì vậy chỉ nằm trong file context, không bị chép vào corpus
(contract v0.2).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from .common import Role, StrictModel, Verdict
from .context import NetworkContext
from .operations import ToolCall


class Subset(StrEnum):
    CLEAN = "clean"
    CORRUPTED = "corrupted"
    ADVERSARIAL = "adversarial"
    AMBIGUOUS = "ambiguous"


class ConfigType(StrEnum):
    VLAN = "vlan"
    INTERFACE = "interface"
    STATIC_ROUTE = "static_route"
    ACL = "acl"  # sau Review 2
    AAA = "aaa"  # sau Review 2


class Actor(StrEnum):
    """Nguồn không tin cậy theo threat model."""

    A1 = "A1"  # operator đặc quyền thấp
    A2 = "A2"  # người sửa được free-text trong inventory
    N = "N"  # lỗi không tấn công


class Technique(StrEnum):
    DIRECT_INJECTION = "direct_injection"
    INDIRECT_INJECTION = "indirect_injection"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    WRONG_DEVICE = "wrong_device"
    OUT_OF_SCOPE = "out_of_scope"


class ContextRef(StrictModel):
    """Tham chiếu tới file network context mà một case dùng: đường dẫn tương đối (posix) tính từ gốc
    repo, và SHA-256 của đúng các byte của file. Case KHÔNG nhúng context (điều chỉnh 1 của contract
    v0.2): secret chỉ nằm trong file context, và nhiều case dùng chung một file mà không chép lại."""

    path: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _relative_inside_repo(cls, value: str) -> str:
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("path phải là đường dẫn tương đối, không có '.', '..' hay '//'")
        return value


class ContextRefError(ValueError):
    """File context không khớp tham chiếu (sha256 lệch, nằm ngoài gốc, không đọc được). Dừng, không
    đoán tiếp."""


def load_context(ref: ContextRef, root: Path) -> NetworkContext:
    """Nạp file context mà `ref` trỏ tới, tính từ thư mục `root`, sau khi kiểm SHA-256 của đúng các
    byte đọc được.

    Lệch hash thì raise `ContextRefError`: case được sinh với một context khác file hiện có. Hash
    tính trên byte, nên file phải có xuống dòng LF trên mọi máy (`.gitattributes`: `* text=auto
    eol=lf`).
    """
    base = root.resolve()
    path = (base / ref.path).resolve()
    if base not in path.parents:
        raise ContextRefError(f"context_ref.path nằm ngoài {base.name}: {ref.path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContextRefError(f"không đọc được context {ref.path}: {type(exc).__name__}") from None
    digest = hashlib.sha256(raw).hexdigest()
    if digest != ref.sha256:
        raise ContextRefError(
            f"sha256 của {ref.path} không khớp context_ref (file đã đổi sau khi case được sinh)"
        )
    return NetworkContext.model_validate_json(raw)


class AttackLabel(StrictModel):
    actor: Actor
    technique: Technique
    # Tool call nào, nếu xuất hiện trong output, thì tính là tấn công THÀNH CÔNG.
    success_if_any: list[ToolCall] = Field(default_factory=list)


class TestCase(StrictModel):
    __test__ = False  # để pytest không nhầm đây là class test

    case_id: str = Field(pattern=r"^(clean|corrupted|adversarial|ambiguous)-[a-z_]+-\d{4}$")
    subset: Subset
    config_type: ConfigType
    seed: int
    generator_version: str
    intent: str = Field(min_length=1, max_length=2000)
    # Intent gốc từ template, trước khi paraphrase. Giữ lại để kiểm tra paraphrase không đổi nghĩa.
    intent_template: str | None = None
    role: Role
    context_ref: ContextRef
    expected_verdict: Verdict
    expected_tool_calls: list[ToolCall] = Field(default_factory=list)
    attack: AttackLabel | None = None
    corruption: str | None = Field(default=None, pattern=r"^CTX_[A-Z_]+$")

    @model_validator(mode="after")
    def _labels_consistent(self) -> TestCase:
        if not self.case_id.startswith(self.subset.value + "-"):
            raise ValueError("case_id phải bắt đầu bằng tên subset")
        if self.expected_verdict is Verdict.ACCEPT and not self.expected_tool_calls:
            raise ValueError("verdict accept phải có expected_tool_calls")
        if self.expected_verdict is not Verdict.ACCEPT and self.expected_tool_calls:
            raise ValueError("verdict reject/clarify không được có expected_tool_calls")
        if (self.subset is Subset.ADVERSARIAL) != (self.attack is not None):
            raise ValueError("chỉ case adversarial mới có (và bắt buộc có) nhãn attack")
        return self
