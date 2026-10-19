"""Áp cấu hình ĐÃ DUYỆT xuống MỘT thiết bị qua SSH: tuỳ chọn, mặc định dry-run, ngoài lô đo (D-064).

Đây là đường DUY NHẤT trong repo gửi cấu hình xuống thiết bị, và không bao giờ chạy tự động: runner,
evaluator, script chạy lô và UI không import module này (`tests/test_apply.py` kiểm). Commit cần
`--commit --confirm <run_id>`; mọi bước ghi vào `trace.jsonl` của lượt chạy với actor `apply` (nối chuỗi,
không ký, D-060).

- `ApplyPlan.from_run`: điều kiện tiên quyết (bản duyệt, sha256, chuỗi trace, verdict, leak, inventory).
- `apply`: dry-run hoặc commit với backup, kiểm sau khi áp, rollback best-effort.
- `Transport` / `NetmikoTransport`: kênh SSH (Netmiko import trễ, extras `[demo]`).

Thiết kế và giới hạn: `docs/design/apply-controlled.md`.
"""

from __future__ import annotations

from .execute import ApplyReport, apply, cfg_blocks, config_lines, rollback_lines, running_blocks, verify
from .plan import ApplyError, ApplyFailed, ApplyPlan, Review
from .transport import NetmikoTransport, Transport, TransportError

__all__ = [
    "ApplyError",
    "ApplyFailed",
    "ApplyPlan",
    "ApplyReport",
    "NetmikoTransport",
    "Review",
    "Transport",
    "TransportError",
    "apply",
    "cfg_blocks",
    "config_lines",
    "rollback_lines",
    "running_blocks",
    "verify",
]
