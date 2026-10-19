"""Áp cấu hình đã duyệt của MỘT thiết bị (`cha-apply`). Mặc định dry-run: chỉ in lệnh, không kết nối.

    python -m harness_agent.apply --run-dir runs/ui/<run_id> --device SW2 \
        --inventory lab/smb-01/inventory.json
    cha-apply --run-dir <dir> --device SW2 --inventory inv.json --commit --confirm <run_id>

Commit cần `--confirm` trùng run_id (tên run-dir) của lượt đã duyệt; SSH dùng `COLLECTOR_USER` /
`COLLECTOR_PASSWORD`; placeholder `<SECRET:ref>` lấy từ `CHA_SECRET_<REF>`. Không bao giờ `write memory`.
KHÔNG dùng trong lô đo (D-064).

Mã thoát: 0 = dry-run xong hoặc áp xong và kiểm đủ; 1 = kiểm thiếu dòng (đã rollback) hoặc lỗi sau khi
đã bắt đầu gửi cấu hình; 2 = bị từ chối (điều kiện tiên quyết, xác nhận sai, thiếu secret, không kết nối
được) hoặc sai tham số.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness_agent.check.__main__ import safe_text

from .execute import ApplyReport, apply
from .plan import ApplyError, ApplyFailed, ApplyPlan
from .transport import NetmikoTransport


class _Parser(argparse.ArgumentParser):
    """Lỗi tham số thành `ApplyError` (mã 2) thay vì `sys.exit`, để `main` gọi được trong test."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ApplyError(message)


def _print_report(plan: ApplyPlan, report: ApplyReport) -> None:
    for warning in report.warnings:
        print(f"[CẢNH BÁO] {safe_text(warning)}", file=sys.stderr)
    head = "DRY-RUN (không kết nối, không gửi gì)" if report.dry_run else "COMMIT"
    print(f"== {head}: {safe_text(plan.device)} {plan.host}, run {safe_text(plan.run_id)}")
    print(f"   duyệt bởi {safe_text(plan.review.reviewer)} lúc {safe_text(plan.review.approved_at)}")
    for line in report.applied_lines:
        print(f"   | {safe_text(line)}")
    if report.required_env:
        print(f"   cần biến môi trường khi commit: {', '.join(report.required_env)}")
    if report.dry_run:
        print(f"   Áp thật: thêm --commit --confirm {safe_text(plan.run_id)}")
        return
    print(f"   backup: {report.backup_path}")
    for line in report.diff_before_after.splitlines():
        print(safe_text(line))
    if report.device_errors:
        print(f"[CẢNH BÁO] thiết bị báo {report.device_errors} dòng lỗi (%) khi nhận cấu hình")
    if report.rolled_back:
        print("[ROLLBACK] thiếu dòng sau khi áp, hoặc thiết bị báo lỗi:")
        for line in report.verify_missing:
            print(f"   - {safe_text(line)}")
        print("   đã gửi:")
        for line in report.rollback_lines:
            print(f"   | {safe_text(line)}")
        print(f"   còn {report.residual_diff_lines} dòng lệch so với .pre.cfg sau rollback")
        print("   So running-config với apply/<device>.pre.cfg và .rollback.cfg trước khi làm gì tiếp.")
    else:
        print("[OK] mọi dòng có trong running-config. Chưa lưu: kiểm rồi tự `write memory` nếu muốn giữ.")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = _Parser(prog="cha-apply", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="run-dir của lượt đã duyệt")
    ap.add_argument("--device", required=True, help="tên thiết bị (có trong inventory và trong bản duyệt)")
    ap.add_argument("--inventory", required=True, help="inventory JSON (như lab/smb-01/inventory.json)")
    ap.add_argument("--context", help="file context của lượt chạy (mặc định: đường dẫn trong case_start)")
    ap.add_argument("--commit", action="store_true", help="áp thật (mặc định dry-run)")
    ap.add_argument("--confirm", metavar="RUN_ID", help="bắt buộc với --commit: run_id của lượt đã duyệt")
    try:
        a = ap.parse_args(argv)
        if a.commit and not a.confirm:
            raise ApplyError("--commit cần --confirm <run_id>")
        if a.confirm and not a.commit:
            raise ApplyError("--confirm chỉ dùng kèm --commit")
        plan = ApplyPlan.from_run(
            Path(a.run_dir), a.device, Path(a.inventory), context=Path(a.context) if a.context else None
        )
        transport = NetmikoTransport(plan.target) if a.commit else None
        report = apply(plan, transport, commit=a.commit, confirm_token=a.confirm)
    except ApplyFailed as exc:
        print(f"[LỖI] {safe_text(str(exc))}", file=sys.stderr)
        return 1
    except ApplyError as exc:
        print(f"[TỪ CHỐI] {safe_text(str(exc))}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # file không đọc được, không phải UTF-8...: chỉ in tên lớp lỗi
        print(f"[TỪ CHỐI] lỗi đọc dữ liệu của lượt chạy: {type(exc).__name__}", file=sys.stderr)
        return 2
    _print_report(plan, report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
