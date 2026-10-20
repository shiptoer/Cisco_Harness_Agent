"""Sensors (control iv): kiểm tra cấu hình ĐÃ render, trước khi thành change package.

Thứ tự theo báo cáo: syntax → Batfish → CIS. Mỗi sensor là một hàm thuần `check_*(device, text, ...)`
trả về `list[Finding]`; `run_sensors` gộp thành `SensorReport` và ghi rõ sensor nào đã chạy, sensor nào
bị bỏ qua (Batfish chưa có, task 3.6). Sensor bị bỏ qua không bao giờ được tính là "đạt".

Sensors độc lập với gate và template (không import registry): đây là lớp phòng thủ sau cùng, dùng được
cả để đo (trong ablation B4) lẫn để bắt lỗi template.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from . import batfish, cis, syntax
from .report import Finding, SensorReport

__all__ = ["Finding", "SensorReport", "run_sensors"]


def run_sensors(
    configs: Mapping[str, str],
    platforms: Mapping[str, str] | None = None,
    secret_values: Iterable[str] = (),
    enabled: Iterable[str] = ("syntax", "batfish", "cis"),
) -> SensorReport:
    """`configs`: {hostname: CLI đã render}. `platforms`: {hostname: "ios"|"iosv"|"iosvl2"} nếu biết."""
    report = SensorReport()
    secrets = list(secret_values)
    plat = dict(platforms or {})
    for name in enabled:
        if name == "syntax":
            for dev, text in configs.items():
                report.findings += syntax.check_syntax(dev, text, plat.get(dev))
            report.sensors["syntax"] = "ran"
        elif name == "batfish":
            report.sensors["batfish"] = "ran" if batfish.is_available() else "skipped"
            if batfish.is_available():  # pragma: no cover - task 3.6
                for dev, text in configs.items():
                    report.findings += batfish.check_batfish(dev, text)
        elif name == "cis":
            for dev, text in configs.items():
                report.findings += cis.check_cis(dev, text, secrets)
            report.sensors["cis"] = "ran"
        else:
            raise ValueError(f"sensor không xác định: {name!r}")
    if os.environ.get("CHA_REQUIRE_BATFISH") == "1" and report.sensors.get("batfish") != "ran":
        report.findings.append(
            Finding(
                "batfish",
                "BF_REQUIRED_UNAVAILABLE",
                "fail",
                "-",
                None,
                "Batfish is required but no operational backend is available.",
            )
        )
    return report
