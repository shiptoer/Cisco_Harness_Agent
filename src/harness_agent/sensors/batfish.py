"""Sensor Batfish (iv-2) — CHƯA CÓ (task 3.6, spike Batfish).

Batfish cần một service riêng (container `batfish/allinone`) và gói `pybatfish`; cả hai chưa được
pin trong `pyproject.toml` (thêm dependency phải hỏi nhóm). Để pipeline B4 không fail-open một cách
âm thầm, sensor này khai báo rõ là `skipped` trong SensorReport; evaluator và báo cáo phải ghi
"Batfish: chưa chạy" chứ không được coi là đã kiểm.

Khi hiện thực: `check_batfish(configs: dict[str, str]) -> list[Finding]` nạp snapshot gồm running-config
của từng thiết bị (từ context) + delta của change package, chạy các câu hỏi `parseWarning`,
`undefinedReferences`, `unusedStructures`, `routes` (next-hop reachable) và trả Finding với mã `BF_*`.
"""

from __future__ import annotations

from .report import Finding

SENSOR = "batfish"


def is_available() -> bool:
    return False


def check_batfish(device: str, text: str) -> list[Finding]:  # pragma: no cover - chưa hiện thực
    raise NotImplementedError("task 3.6 — spike Batfish (TV3)")
