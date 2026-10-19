"""Control (i) ở runner: kiểm file network context bằng contract rồi bằng invariants, TRƯỚC mọi lời gọi
LLM.

Một hàm riêng, để PR ablation sau này tắt được bước này cho các mức không có control (i) (B0, B1) mà
không phải sửa `run_case`. Kết cục khi có vi phạm do `run_case` quyết định (reject,
`stopped_by=context_validation`, D-017).

- Lỗi contract (kiểu, enum, trường lạ...): mỗi lỗi một `Violation` mã `CTX_SCHEMA`, `path` theo `loc`
  của pydantic, `detail` là loại lỗi + thông báo đã lọc (không chép giá trị đầu vào,
  `safe_error_message`).
- Contract hợp lệ: `check_invariants` (D-005; 16 mã, `docs/design/invariants-plan.md`).

Trạng thái: ĐỀ XUẤT (D-043).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from harness_agent.models import NetworkContext, Violation, check_invariants
from harness_agent.models.operations import safe_error_message

from .context_binding import freshness

_BOM = b"\xef\xbb\xbf"


def context_violations(path: Path, *, invariants: bool = True) -> list[Violation]:
    """Mọi vi phạm của file context ở `path`: lỗi đọc file, lỗi contract, hoặc vi phạm invariant.
    Rỗng = hợp lệ. Không kiểm freshness (collector dùng hàm này ngay sau khi thu thập)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [Violation(code="CTX_UNREADABLE", device="-", path="", detail=type(exc).__name__)]
    return context_violations_from_bytes(raw, invariants=invariants)


def context_violations_from_bytes(
    raw: bytes | None,
    *,
    invariants: bool = True,
    freshness_at: datetime | None = None,
    check_freshness: bool = False,
) -> list[Violation]:
    """Như `context_violations`, trên ĐÚNG bộ byte runner đã ràng buộc cho lượt chạy (D-054): không đọc file
    lần nữa. `raw` None = file không đọc được.

    `check_freshness` (control (i) ở B2+): context hợp lệ contract thì thêm vi phạm `CTX_STALE` khi context
    `collector` quá TTL so với `freshness_at` (= `evaluated_at` của case, không phải đồng hồ máy)."""
    if raw is None:
        return [Violation(code="CTX_UNREADABLE", device="-", path="", detail="OSError")]
    try:
        # Bỏ BOM như ContextStore (đọc bằng utf-8-sig): file context tạo trên Windows có thể có BOM.
        ctx = NetworkContext.model_validate_json(raw.removeprefix(_BOM))
    except ValidationError as exc:
        return [
            Violation(
                code="CTX_SCHEMA",
                device="-",
                path=format_loc(err.get("loc", ())),
                detail=f"[{err.get('type')}] {safe_error_message(err)}",
            )
            for err in exc.errors(include_input=False)
        ]
    found = check_invariants(ctx) if invariants else []
    if check_freshness:
        found += freshness(ctx.provenance, freshness_at)[1]
    return found


def format_loc(loc: tuple) -> str:
    """`loc` của pydantic → đường dẫn cùng dạng với `Violation.path`:
    ("devices", 1, "role") → "devices[1].role"."""
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out
