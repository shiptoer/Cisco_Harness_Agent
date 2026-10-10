"""
nlcfg.registry.gate
=====================
Hằng số policy của registry mà gate dùng lại: ngưỡng blast-radius mặc định và
danh sách pattern injection.

File này KHÔNG còn chứa gate. Gate duy nhất của hệ thống là
`harness_agent.harness.gate.evaluate` (6 bước G1–G6, chạy cho TỪNG tool call và
ghi đủ từng bước vào trace). `run_gate` cũ chạy trên cả change set lúc submit,
trả về danh sách lý do không có tên bước, và không nơi nào gọi nó; giữ lại sẽ
thành một gate thứ hai với luật khác.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 1. Cấu hình ngưỡng blast-radius mặc định — chỉnh theo lab thực tế.
# Harness lấy ngưỡng thiết bị/interface từ policy của context; chỉ dùng
# max_vlans ở đây vì context chưa có trường tương ứng.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BlastCaps:
    max_devices: int = 3
    max_interfaces: int = 10
    max_vlans: int = 10


DEFAULT_CAPS = BlastCaps()

# ---------------------------------------------------------------------------
# 2. Từ khoá injection cơ bản — hạt giống lấy cảm hứng từ các pattern trong
# Liu et al. (benchmark tấn công prompt injection) và Greshake et al.
# (indirect injection). Đây KHÔNG phải danh sách đầy đủ — mở rộng dần khi
# quan sát thêm case trong corpus adversarial (Chương 7 của playbook).
# Rule-based, không học máy: là một lớp phòng thủ bổ sung, không phải giải
# pháp triệt để cho prompt injection.
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore (all )?(previous|prior|above) instructions",
        r"disregard (all )?(previous|prior|above)",
        r"you are now",
        r"act as (a|an) ",
        r"system prompt",
        r"override (the )?(rule|policy|role)",
        r"bypass (the )?(gate|check|approval)",
        r"\bsudo\b",
        r"as an ai\b",
    ]
]
