"""Freshness binding của network context (D-047 (a), D-054).

Hai phần, bật ở hai chỗ khác nhau:

- **Binding** (mọi mức B0–B4, giống trace (v): bảo đảm phép đo hợp lệ, không phải biến ablation). Runner đọc
  file context MỘT lần lúc bắt đầu lượt chạy (`bind_context`), mọi bước sau dùng đúng bộ byte đó hoặc kiểm
  lại hash của nó: harness server so hash khi nạp (`--context-sha256`), runner so lại ngay trước khi render.
  - Hash khác hash ghim trong case (`context_sha256`) → `ContextBindingError` mã `CTX_HASH_MISMATCH`: lỗi dữ
    liệu case, KHÔNG phải kết cục; `run_cases` thoát mã 2 và dừng lô (D-017: lỗi cấu hình là exception).
  - File đổi trong lúc chạy → kết cục ERROR `CTX_CHANGED_DURING_RUN` (nhóm security, `runner/pipeline.py`).
- **Freshness** (chỉ B2+, cùng control (i), `spec.context_validation`): context `collector` quá
  `ttl_seconds` so với `evaluated_at` của case → reject `CTX_STALE`, cùng đường với vi phạm invariant.

"now" là `evaluated_at` của case, truyền qua tham số. Module này KHÔNG đọc đồng hồ máy: cùng case chạy
ngày nào cũng cho cùng kết quả.

Giới hạn (ghi ở D-054): hash chỉ phát hiện FILE đổi, không phát hiện THIẾT BỊ đổi sau lúc thu thập; và
khoảng từ lúc người duyệt đọc change package tới lúc áp lên thiết bị (TOCTOU) nằm ngoài hệ thống.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.models import ContextSource, Provenance, Violation

STALE = "CTX_STALE"
HASH_MISMATCH = "CTX_HASH_MISMATCH"
EVALUATED_AT_MISSING = "CTX_EVALUATED_AT_MISSING"
# Đường dẫn của vi phạm `CTX_STALE`, cùng dạng với `Violation.path` của invariant.
STALE_PATH = "provenance.collected_at"
# Hai lý do của `CTX_STALE`, là phần đầu của `detail` (trace `context_invalid`): người đọc trace và test phân
# biệt "dữ liệu cũ" với "mốc thời gian sai", hai lỗi có cách sửa khác nhau.
STALE_EXPIRED = "quá hạn TTL"
STALE_FUTURE = "collected_at ở tương lai so với evaluated_at"

# Trạng thái freshness ghi vào snapshot (trace, change package).
FRESH, STALE_STATUS, NO_TTL, NOT_CHECKED, INVALID = "fresh", "stale", "no_ttl", "not_checked", "invalid"

_BOM = b"\xef\xbb\xbf"


class ContextBindingError(Exception):
    """File context không dùng được cho case này: lỗi DỮ LIỆU CASE, không phải kết cục của lượt chạy.
    Người gọi dừng lô (`run_cases` thoát mã 2), không ghi kết quả nào cho case."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class BoundContext:
    """Bộ byte context của một lượt chạy và những gì đọc được từ nó, không validate.

    `raw` là None khi file không đọc được và case không ghim hash: kết cục khi đó do mức quyết, như trước
    D-054 (B0: PREFLIGHT_FAILED, B1–B4: reject CTX_UNREADABLE). `provenance` là None khi context không có
    khối đó hoặc khối đó sai contract (`provenance_invalid`); ở B1+ contract sẽ từ chối context sai."""

    raw: bytes | None
    sha256: str | None
    context_id: str | None
    provenance: Provenance | None
    provenance_invalid: bool


def sha256_of(path: Path) -> str:
    """SHA-256 của các byte hiện có của file; "unreadable" nếu không đọc được."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def bind_context(
    path: Path, expected_sha256: str | None = None, evaluated_at: datetime | None = None
) -> BoundContext:
    """Đọc file context MỘT lần cho cả lượt chạy.

    Raise `ContextBindingError`:
    - `CTX_HASH_MISMATCH`: có `expected_sha256` (hash ghim trong case) mà file khác hash đó hoặc không đọc
      được;
    - `CTX_EVALUATED_AT_MISSING`: context ghi nguồn `collector` mà không có `evaluated_at`. Kiểm ở MỌI mức,
      kể cả B0/B1 (không kiểm freshness), để cùng một case dùng được ở cả năm mức.
    `evaluated_at` không có múi giờ là lỗi của người gọi (`ValueError`)."""
    if evaluated_at is not None and evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at phải có múi giờ")
    try:
        raw: bytes | None = path.read_bytes()
    except OSError:
        raw = None
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if expected_sha256 is not None and digest != expected_sha256:
        actual = digest or "unreadable"
        raise ContextBindingError(
            HASH_MISMATCH,
            f"sha256 của {path.name} là {actual}, case ghim {expected_sha256} "
            "(file đã đổi sau khi case được viết)",
        )
    data = _json_object(raw)
    declared = data.get("provenance") if data is not None else None
    provenance, invalid = None, False
    if declared is not None:
        try:
            provenance = Provenance.model_validate(declared)
        except ValidationError:
            invalid = True
    if (
        evaluated_at is None
        and isinstance(declared, dict)
        and declared.get("source") == ContextSource.COLLECTOR
    ):
        raise ContextBindingError(
            EVALUATED_AT_MISSING,
            f"{path.name} là context collector: case phải có evaluated_at để kiểm freshness",
        )
    context_id = data.get("context_id") if data is not None else None
    return BoundContext(
        raw=raw,
        sha256=digest,
        context_id=context_id if isinstance(context_id, str) else None,
        provenance=provenance,
        provenance_invalid=invalid,
    )


def _json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw.removeprefix(_BOM).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def freshness(provenance: Provenance | None, evaluated_at: datetime | None) -> tuple[str, list[Violation]]:
    """(trạng thái, vi phạm) của một context so với `evaluated_at`. Hàm thuần: không đọc đồng hồ máy.

    - Không có provenance, hoặc provenance synthetic → `no_ttl`, không vi phạm: không có gì để so.
    - Collector: tuổi = `evaluated_at - collected_at`. Còn hạn khi 0 ≤ tuổi ≤ `ttl_seconds`. Quá hạn, hoặc
      `collected_at` nằm SAU `evaluated_at` (dữ liệu "từ tương lai": sai mốc, không tin được) → `CTX_STALE`.
    Collector mà thiếu `evaluated_at` là lỗi của người gọi (`bind_context` đã chặn): raise `ValueError`."""
    if provenance is None or provenance.source is ContextSource.SYNTHETIC:
        return NO_TTL, []
    if evaluated_at is None:
        raise ValueError("context collector cần evaluated_at")
    collected, ttl = provenance.collected_at, provenance.ttl_seconds
    if collected is None or ttl is None:  # contract đã chặn; giữ để không bao giờ coi là còn hạn
        raise ValueError("provenance collector thiếu collected_at hoặc ttl_seconds")
    age = evaluated_at - collected  # so đúng tới micro giây, không làm tròn
    if age < timedelta(0):
        detail = (
            f"{STALE_FUTURE}: collected_at {_utc(collected)} sau evaluated_at {_utc(evaluated_at)} "
            f"{-age.total_seconds():g} giây (mốc thu thập hoặc mốc đánh giá sai)"
        )
    elif age > timedelta(seconds=ttl):
        detail = (
            f"{STALE_EXPIRED}: tuổi {age.total_seconds():g} giây > ttl {ttl} giây "
            f"(thu thập {_utc(collected)}, đánh giá {_utc(evaluated_at)})"
        )
    else:
        return FRESH, []
    return STALE_STATUS, [Violation(code=STALE, device="-", path=STALE_PATH, detail=detail)]


def snapshot(bound: BoundContext, evaluated_at: datetime | None, checked: bool) -> dict[str, Any]:
    """Những gì người duyệt và người đọc trace cần biết về context của lượt chạy. KHÔNG có đường dẫn tuyệt
    đối (phụ thuộc máy, làm vỡ golden). `checked` = mức này có kiểm freshness (B2+)."""
    p = bound.provenance
    if bound.provenance_invalid:
        status = INVALID
    elif not checked:
        status = NOT_CHECKED
    else:
        status = freshness(p, evaluated_at)[0]
    return {
        "context_id": bound.context_id,
        "sha256": bound.sha256,
        "provenance": None if p is None else "declared",
        "source": ContextSource.SYNTHETIC.value if p is None else p.source.value,
        "collected_at": None if p is None or p.collected_at is None else _utc(p.collected_at),
        "ttl_seconds": None if p is None else p.ttl_seconds,
        "evaluated_at": None if evaluated_at is None else _utc(evaluated_at),
        "freshness": status,
    }


def package_lines(snap: dict[str, Any], level: str) -> list[str]:
    """Các dòng về context trong change package. Xác định từ snapshot: cùng case thì cùng byte."""
    lines = [f"- Context: `{snap['context_id']}`, sha256 `{snap['sha256']}`"]
    if snap["source"] == ContextSource.COLLECTOR.value:
        lines.append(
            f"- Nguồn context: collector, thu thập {snap['collected_at']}, TTL {snap['ttl_seconds']} giây"
        )
    else:
        origin = "không có provenance" if snap["provenance"] is None else "provenance synthetic"
        lines.append(f"- Nguồn context: synthetic ({origin})")
    status = snap["freshness"]
    if snap["source"] != ContextSource.COLLECTOR.value:
        lines.append("- Freshness: không kiểm TTL")
    elif status == NOT_CHECKED:
        lines.append(f"- Freshness: KHÔNG kiểm ở mức {level} (freshness có từ B2)")
    elif status == FRESH:
        lines.append(f"- Freshness: còn hạn tại evaluated_at {snap['evaluated_at']}")
    else:
        lines.append(f"- Freshness: {status}")
    return lines


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
