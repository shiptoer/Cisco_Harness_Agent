"""Chạy nhiều case và so với kết quả kỳ vọng.

    python scripts/run_cases.py --config config/fake.toml            # chạy khô, không cần API key
    python scripts/run_cases.py --config config/local.toml clean-vlan30 adv-protected-uplink

Không truyền tên case thì chạy mọi file trong data/cases/.
Mỗi case có khoá "expect"; so khớp các trường có trong expect (status, verdict, stopped_by,
error_code, leak_attempt_detected, leak_in_package, leak_sources) bằng `expect_mismatches`.
`leak_sources` trong expect là danh sách LOẠI nguồn (raw_stream, harness_blocked, run_dir_file, dsh_home,
dsh_cwd), so không kể thứ tự và số lần; vị trí cụ thể nằm trong result.json.
Cột leak: A = có nguồn ngoài change package (ý định rò rỉ); P = secret nằm trong change package
(rò rỉ thật); "-" = không.
Cột token: tổng token dsh báo cho case đó ("-" nếu dsh không báo đủ); cột giây: thời gian wall-clock.
Dòng cuối in tổng của cả lô. Số liệu chi tiết theo stage nằm trong result.json (`usage`, `duration_s`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness_agent.levels import LEVEL_NAMES
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.pipeline import expect_mismatches, lock_workspace, run_case
from harness_agent.runner.usage import token_sum

CASES = Path("data/cases")
TOKEN_COLUMNS = ("input_uncached", "input_cached", "cache_write", "output")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252: không in được tiếng Việt
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/local.toml")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--level", choices=LEVEL_NAMES, default="B4")
    ap.add_argument("--expect-key", default="expect")
    ap.add_argument("--keep-dsh-home", action="store_true", help="giữ session log của dsh sau khi quét")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    try:
        cfg = LocalConfig.load(a.config)
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2
    cfg.keep_dsh_home = cfg.keep_dsh_home or a.keep_dsh_home

    files = [CASES / f"{n}.json" for n in a.names] if a.names else sorted(CASES.glob("*.json"))
    try:
        # Giữ khoá thư mục làm việc của dsh cho CẢ lô (D-025): một lượt chạy khác không chen được
        # vào giữa hai case. Lượt khác đang giữ khoá thì dừng ở đây, trước khi đụng tới kết quả nào.
        with lock_workspace(cfg):
            return run_all(cfg, files, Path(a.out), a.level, a.expect_key)
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2


def run_all(
    cfg: LocalConfig, files: list[Path], out: Path, level: str = "B4", expect_key: str = "expect"
) -> int:
    failed = 0
    totals = RunTotals()
    print(
        f"{'case':30} {'status':7} {'verdict':8} {'stopped_by':22} {'error_code':24} {'leak':5} "
        f"{'token':>8} {'giây':>7} kỳ vọng"
    )
    for f in files:
        case = json.loads(f.read_text(encoding="utf-8"))
        if case.get("fake_scenario"):
            os.environ["FAKE_DSH_SCENARIO"] = str(Path(case["fake_scenario"]).resolve())
        r = run_case(
            cfg,
            Path(case["context"]),
            case["intent"],
            case["role"],
            out,
            case.get("run_id", f.stem) + ("" if level == "B4" else f".{level}"),
            level,
        )
        expect = case.get("expect_levels", {}).get(level) if level != "B4" else case.get(expect_key)
        wrong = ["Không có kỳ vọng cho mức này"] if expect is None else expect_mismatches(r, expect)
        failed += bool(wrong)
        leak = ("A" if r.leak_attempt_detected else "-") + "/" + ("P" if r.leak_in_package else "-")
        tokens = token_sum(r.usage.get("total", {}))
        seconds = r.duration_s.get("total", 0.0)
        totals.add(r.usage.get("total", {}), seconds)
        print(
            f"{f.stem:30} {r.status:7} {str(r.verdict):8} {str(r.stopped_by):22} "
            f"{str(r.error_code or '-'):24} {leak:5} "
            f"{'-' if tokens is None else tokens:>8} {seconds:>7.1f} "
            f"{'SAI  ' + '; '.join(wrong) if wrong else 'ĐÚNG'}"
        )
        if r.status == "error":
            print(f"{'':30} lý do: {r.reason}")
        if r.leak_sources:
            kinds = sorted({str(s.get("source")) for s in r.leak_sources})
            print(f"{'':30} nguồn leak: {', '.join(kinds)}")
        if not r.leak_scan_complete:
            print(f"{'':30} QUÉT LEAK KHÔNG ĐỦ: không kết luận được case này sạch (leak_scan_complete=false)")
    print(f"\n{len(files) - failed}/{len(files)} case đúng kỳ vọng")
    print(totals.line())
    return 1 if failed else 0


class RunTotals:
    """Tổng token, request và thời gian của cả lô. Chỉ cộng những gì dsh đã báo: case không có đủ số liệu
    token thì được đếm riêng, không bị coi là 0."""

    def __init__(self) -> None:
        self.sums = dict.fromkeys(TOKEN_COLUMNS, 0)
        self.requests = 0
        self.seconds = 0.0
        self.unreported = 0

    def add(self, usage: dict, seconds: float) -> None:
        self.seconds += seconds
        self.requests += usage.get("requests") or 0
        if token_sum(usage) is None:
            self.unreported += 1
            return
        for name in TOKEN_COLUMNS:
            self.sums[name] += usage[name]

    def line(self) -> str:
        s = self.sums
        text = (
            f"Tổng: {sum(s.values())} token (input không cache {s['input_uncached']}, "
            f"từ cache {s['input_cached']}, ghi cache {s['cache_write']}, output {s['output']}) "
            f"| {self.requests} request | {self.seconds:.1f} giây"
        )
        if self.unreported:
            text += f" | {self.unreported} case không có số liệu token (dsh không báo), không tính vào tổng"
        return text


if __name__ == "__main__":
    raise SystemExit(main())
