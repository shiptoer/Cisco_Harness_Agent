"""Thu thập network context của một lab. CHỈ ĐỌC: sáu lệnh `show` trong allowlist, không lệnh nào khác.

    # Offline, từ output đã lưu (không cần thiết bị, không cần mật khẩu). Mốc thu thập lấy từ tên thư mục
    # (<mốc UTC> do --save-raw đặt, vd 20261013T083915Z) hoặc từ --collected-at; thiếu cả hai là lỗi:
    python -m harness_agent.collector --from-dir <thư mục> [--collected-at 2026-10-13T08:39:15Z]

    # Dựng output giả từ cấu hình nền rồi chạy offline trên đó (fixture, để thử đường đi):
    python -m harness_agent.collector --synth-from lab/smb-01/baseline-configs

    # SSH tới thiết bị trong inventory. User và mật khẩu CHỈ lấy từ biến môi trường:
    #   $env:COLLECTOR_USER = "..."; mật khẩu nhập bằng Read-Host -AsSecureString vào COLLECTOR_PASSWORD
    python -m harness_agent.collector --ssh [--save-raw [<gốc>]]
    # --save-raw: output thô (đã xoá secret) vào <gốc>/<context_id>-raw/<mốc UTC>/, mặc định
    # <gốc> = runs-real; mỗi lượt một thư mục, cùng mốc với file context, không ghi đè lượt trước

Freshness (D-054): context từ --ssh và --from-dir mang `provenance` {source: collector, collected_at,
ttl_seconds}; `--ttl-seconds` mặc định 86400. Với --ssh, mốc lấy TRƯỚC khi đọc thiết bị: tuổi tính từ dữ liệu
cũ nhất của lượt. Context từ --synth-from mang `provenance` {source: synthetic}.

Kết quả: `<out>/<context_id>-<thời gian UTC>.json` và `.meta.json`. Sau khi ghi, collector chạy contract và
invariants trên file; có vi phạm thì in ra và thoát mã 3, KHÔNG sửa dữ liệu. Mã 2 = lỗi cấu hình hoặc lỗi
khi lấy dữ liệu.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from harness_agent.models.context import MAX_TTL_SECONDS

from .build import DEFAULT_TTL_SECONDS, build_context, collector_provenance, load_policy, write_context
from .fetch import (
    STAMP_FORMAT,
    CollectorError,
    fetch_ssh,
    load_inventory,
    new_raw_dir,
    read_saved,
    save_outputs,
)
from .synth import synthesize, write_raw


def _now() -> datetime:
    return datetime.now(UTC)


def _ttl(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("phải là số nguyên giây") from None
    if not 1 <= value <= MAX_TTL_SECONDS:
        raise argparse.ArgumentTypeError(f"phải trong 1..{MAX_TTL_SECONDS}")
    return value


def _from_dir_stamp(folder: Path, collected_at: str | None) -> datetime:
    """Mốc thu thập của output đã lưu: `--collected-at`, hoặc tên thư mục `<mốc UTC>` do `--save-raw` đặt.
    KHÔNG dùng giờ xử lý: chạy lại trên output cũ sẽ cho một context trông như mới (D-054)."""
    if collected_at is not None:
        try:
            moment = datetime.fromisoformat(collected_at)
        except ValueError:
            raise CollectorError(f"--collected-at không phải ISO 8601: {collected_at!r}") from None
        if moment.tzinfo is None:
            raise CollectorError("--collected-at phải có múi giờ (vd ...Z)")
        return moment.astimezone(UTC)
    try:
        return datetime.strptime(folder.name, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise CollectorError(
            f"không biết mốc thu thập của {folder.as_posix()}: tên thư mục không phải <mốc UTC> "
            "(vd 20261013T083915Z); truyền --collected-at"
        ) from None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252: không in được tiếng Việt
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="lab/smb-01/inventory.json")
    ap.add_argument("--policy", default="data/contexts/smb-01.policy.json")
    ap.add_argument("--out", default="data/contexts/collected")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--ssh", action="store_true", help="SSH tới mọi thiết bị trong inventory")
    source.add_argument("--from-dir", help="đọc output đã lưu: <thư mục>/<hostname>/<lệnh>.txt")
    source.add_argument("--synth-from", help="dựng output giả từ thư mục cấu hình nền, rồi chạy offline")
    ap.add_argument(
        "--save-raw",
        nargs="?",
        const="runs-real",
        metavar="GỐC",
        help="(với --ssh) lưu output vào <GỐC>/<context_id>-raw/<mốc UTC>/ để chạy offline sau, mặc định "
        "GỐC = runs-real; secret bị xoá trước khi ghi",
    )
    ap.add_argument(
        "--ttl-seconds",
        type=_ttl,
        default=DEFAULT_TTL_SECONDS,
        help=f"TTL của context thu thập, giây (mặc định {DEFAULT_TTL_SECONDS}); runner kiểm ở B2+ (D-054)",
    )
    ap.add_argument("--collected-at", help="(với --from-dir) mốc thu thập, ISO 8601 có múi giờ")
    a = ap.parse_args(argv)
    if a.collected_at is not None and not a.from_dir:
        ap.error("--collected-at chỉ dùng với --from-dir")

    inventory_path, policy_path = Path(a.inventory), Path(a.policy)
    try:
        inventory = load_inventory(inventory_path)
        policy = load_policy(policy_path)
        if a.ssh:
            # Một mốc cho cả thư mục raw và file context của lượt này, lấy TRƯỚC khi đọc thiết bị (D-054).
            now = _now()
            outputs, label = fetch_ssh(inventory), "ssh"
        elif a.from_dir:
            outputs, label = read_saved(Path(a.from_dir), inventory), f"offline:{Path(a.from_dir).as_posix()}"
            now = _from_dir_stamp(Path(a.from_dir), a.collected_at)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                write_raw(Path(tmp), synthesize(Path(a.synth_from), inventory))
                outputs = read_saved(Path(tmp), inventory)
            label = f"synthetic:{Path(a.synth_from).as_posix()}"
            now = _now()  # chỉ đặt tên file; context synthetic không có mốc thu thập
        provenance = {"source": "synthetic"} if a.synth_from else collector_provenance(now, a.ttl_seconds)
        raw_dir = None
        if a.ssh and a.save_raw:
            raw_dir = new_raw_dir(Path(a.save_raw), inventory.context_id, now)
            removed = save_outputs(raw_dir, outputs)
            print(f"đã lưu output vào {raw_dir.as_posix()} ({removed} dòng secret đã xoá trước khi ghi)")
        context, notes = build_context(inventory, outputs, policy)
        if raw_dir is not None:
            notes["raw_dir"] = raw_dir.as_posix()
        context_path, meta_path, violations = write_context(
            Path(a.out),
            context,
            notes,
            label,
            {"inventory": inventory_path, "policy": policy_path},
            now,
            provenance=provenance,
        )
    except CollectorError as exc:
        print(f"[LỖI COLLECTOR] {exc}", file=sys.stderr)
        return 2

    devices = context["devices"]
    print(f"nguồn: {label}")
    print("provenance: " + ", ".join(f"{k}={v}" for k, v in provenance.items()))
    print(f"context: {context_path}")
    print(f"meta:    {meta_path}")
    print(
        f"{len(devices)} thiết bị, {sum(len(d['interfaces']) for d in devices)} interface, "
        f"{sum(len(d['vlans']) for d in devices)} VLAN, "
        f"{sum(len(d['static_routes']) for d in devices)} static route, "
        f"{sum(len(d['acls']) for d in devices)} ACL, {len(context['links'])} link"
    )
    for gap in notes["contract_gaps"]:
        print(f"  contract chưa mô tả: {gap['what']} ({gap['count']})")
        for device, names in gap.get("names", {}).items():
            print(f"    {device}: {', '.join(names)}")
    if violations:
        print(f"CONTRACT / INVARIANTS: {len(violations)} vi phạm (collector không sửa dữ liệu):")
        for v in violations:
            print(f"  {v['code']} {v['device']} {v['path']}: {v['detail']}")
        return 3
    print("contract + invariants: 0 vi phạm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
