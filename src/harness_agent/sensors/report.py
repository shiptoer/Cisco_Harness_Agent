"""Kiểu dữ liệu chung của sensors (iv): Finding và SensorReport.

Mỗi sensor trả về danh sách Finding. Severity:
- `fail`: cấu hình KHÔNG được gửi người duyệt (runner → verdict reject, stopped_by = sensor_<tên>).
- `warn`: ghi vào report và change package để người duyệt thấy, không chặn.
- `info`: chỉ để trace.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["fail", "warn", "info"]


@dataclass(frozen=True)
class Finding:
    sensor: str  # "syntax" | "cis" | "batfish"
    code: str  # mã ổn định, vd SYN_UNKNOWN_COMMAND, CIS_1_4_PLAINTEXT_SECRET
    severity: Severity
    device: str
    line_no: int | None  # dòng trong candidate/<device>.cfg (1-based), None nếu là kết luận cả file
    message: str
    reference: str = ""  # mục của chuẩn (vd "CIS Cisco IOS 15 Benchmark 1.4.4")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SensorReport:
    findings: list[Finding] = field(default_factory=list)
    # Sensor đã chạy, theo thứ tự: {"syntax": "ran", "batfish": "skipped", ...}
    sensors: dict[str, str] = field(default_factory=dict)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def blocking_sensor(self) -> str | None:
        """Tên sensor đầu tiên (theo thứ tự chạy) có finding `fail`; None nếu không có."""
        for name, status in self.sensors.items():
            if status == "ran" and any(f.sensor == name for f in self.failures):
                return name
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensors": dict(self.sensors),
            "blocking_sensor": self.blocking_sensor,
            "n_fail": len(self.failures),
            "n_warn": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }
