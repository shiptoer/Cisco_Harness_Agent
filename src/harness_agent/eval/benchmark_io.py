"""Nạp benchmark để chấm (D-065), tách khỏi generator `corpus.benchmark` (D-006).

Generator import gate và grounding (để chỉ sinh gold qua được chúng); evaluator thì không được import control
đang được đánh giá. Vì vậy phép nạp + kiểm sha256 dùng khi chấm nằm ở đây, chỉ phụ thuộc contract `models`.
`corpus.benchmark.load_benchmark` là cùng hàm này (import lại để giữ chỗ gọi cũ).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness_agent.models import TestCase, load_context

Manifest = dict[str, Any]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_benchmark(root: Path) -> tuple[Manifest, dict[str, TestCase], Path]:
    """Đọc manifest + TestCase, kiểm sha256 của MỌI file (case, kịch bản, gold, context) so với manifest.
    Lệch → ValueError (dữ liệu đã đổi sau khi sinh: không chấm trên gold không còn là gold)."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if "benchmark_version" not in manifest:
        raise ValueError(f"{root} không phải benchmark (manifest thiếu benchmark_version)")

    def check(rel: str, expected: str) -> None:
        if _sha((root / rel).read_bytes()) != expected:
            raise ValueError(f"sha256 của {rel} không khớp manifest")

    for entry in manifest["contexts"].values():
        check(entry["file"], entry["sha256"])
    cases: dict[str, TestCase] = {}
    for cid, m in manifest["cases"].items():
        check(m["file"], m["sha256"])
        check(m["fake_scenario"], m["scenario_sha256"])
        for g in m["gold"].values():
            check(g["file"], g["sha256"])
        case = TestCase.model_validate_json((root / m["file"]).read_bytes())
        if case.case_id != cid:
            raise ValueError(f"case_id trong {m['file']} không khớp manifest")
        load_context(case.context_ref, root)  # kiểm đường dẫn + hash của context
        cases[cid] = case
    return manifest, cases, root
