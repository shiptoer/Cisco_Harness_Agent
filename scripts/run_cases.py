"""Chạy nhiều case, so với KỲ VỌNG FIXTURE (khi chạy khô) và với NHÃN ĐÚNG của case (D-045).

    python scripts/run_cases.py --config config/fake.toml            # chạy khô, không cần API key
    python scripts/run_cases.py --config config/local.toml clean-vlan30 adv-protected-uplink
    python scripts/run_cases.py --config config/fake.toml --level B0         # baseline B0, chạy khô

Không truyền tên case thì chạy mọi file trong data/cases/ (case có nhãn) và data/fixtures/cases/ (case
không nhãn, chỉ thử đường lỗi của runner).
`--level B0..B4` chọn mức ablation (D-049, `harness_agent.levels`; mặc định B3). `--condition B0|B3` là tên
cũ, vẫn nhận nhưng có cảnh báo. Mức khác B3 ghi kết quả vào `runs-<mức>` (không phải `runs`: `run_case` xoá
thư mục của case trước khi chạy, nên hai mức dùng chung thư mục sẽ xoá kết quả của nhau).
B0 và B1 gửi nguyên văn context, kể cả giá trị secret, cho model: CHỈ chạy thật với context có secret giả.

Hai cột cuối là hai phép so KHÁC NHAU:
- `fixture`: kết quả so với khoá "expect" của kịch bản `fake_dsh` của mức đó (SCENARIO_DIRS trong
  `runner/cases.py`), bằng `expect_mismatches`. Chỉ có nghĩa khi chạy khô; với dsh thật cột này là "-".
  Nó kiểm RUNNER: kịch bản giả trả lời gì thì runner phải phân loại ra đúng thế.
- `nhãn`: verdict so với `label.expected_verdict` của case, giống nhau ở mọi mức. ĐÚNG / SAI /
  ERROR(security|infra|harness|agent) / "-" (case không có nhãn). ERROR không bao giờ khớp nhãn. Khi chạy
  khô cột này phản ánh KỊCH BẢN GIẢ, không phản ánh model. Cột này chỉ so verdict: nội dung của một lượt
  accept và attack success của một lượt adversarial do evaluator chấm.

Mã thoát: chạy khô → 1 nếu có fixture lệch. Chạy thật → 0 khi chạy xong mọi case, dù model đúng hay sai
(đọc dòng tổng của cột nhãn). 2 = lỗi cấu hình hoặc lỗi dữ liệu case, gồm file context khác `context_sha256`
ghim trong case (`CTX_HASH_MISMATCH`, D-054): kiểm cho MỌI case trước khi chạy case đầu tiên, và kiểm lại lúc
mỗi case bắt đầu; lệch ở đâu cũng dừng cả lô.

`leak_sources` trong expect là danh sách LOẠI nguồn (raw_stream, harness_blocked, run_dir_file, dsh_home,
dsh_cwd), so không kể thứ tự và số lần; vị trí cụ thể nằm trong result.json.
Cột leak: A = có nguồn ngoài change package (ý định rò rỉ); P = secret nằm trong change package
(rò rỉ thật); "-" = không.
Cột token: tổng token dsh báo cho case đó ("-" nếu dsh không báo đủ); cột giây: thời gian wall-clock.
Số liệu chi tiết theo stage nằm trong result.json (`usage`, `duration_s`).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from harness_agent.levels import Level, add_level_arguments, level_from_args
from harness_agent.runner.cases import (
    NO_LABEL,
    RIGHT,
    WRONG,
    CaseError,
    case_files,
    check_case_context,
    context_path,
    fixture_expect,
    label_outcome,
    load_case,
    scenario_path,
)
from harness_agent.runner.context_binding import ContextBindingError
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.pipeline import expect_mismatches, lock_workspace, run_case
from harness_agent.runner.usage import token_sum

TOKEN_COLUMNS = ("input_uncached", "input_cached", "cache_write", "output")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252: không in được tiếng Việt
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/local.toml")
    ap.add_argument("--out", help="thư mục kết quả; mặc định `runs` (B3) hoặc `runs-<mức>` (mức khác)")
    add_level_arguments(ap)
    ap.add_argument("--keep-dsh-home", action="store_true", help="giữ session log của dsh sau khi quét")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    try:
        cfg = LocalConfig.load(a.config)
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2
    cfg.keep_dsh_home = cfg.keep_dsh_home or a.keep_dsh_home
    level = level_from_args(ap, a)
    out = Path(a.out or ("runs" if level is Level.B3 else f"runs-{level.value.lower()}"))

    try:
        files = case_files(a.names or None)
        # Giữ khoá thư mục làm việc của dsh cho CẢ lô (D-025): một lượt chạy khác không chen được
        # vào giữa hai case. Lượt khác đang giữ khoá thì dừng ở đây, trước khi đụng tới kết quả nào.
        with lock_workspace(cfg):
            return run_all(cfg, files, out, level)
    except CaseError as exc:
        print(f"[LỖI DỮ LIỆU CASE] {exc}", file=sys.stderr)
        return 2
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2


def run_all(cfg: LocalConfig, files: list[Path], out: Path, level: Level = Level.B3) -> int:
    # Chạy khô = `fake_dsh`: chỉ khi đó kỳ vọng fixture mới có nghĩa.
    dry = not cfg.requires_api_key
    cases = [(f, load_case(f)) for f in files]  # kiểm mọi file TRƯỚC khi chạy case đầu tiên
    for f, case in cases:  # cả file context mà case ghim (D-054)
        check_case_context(f, case)
    fixture_wrong = 0
    labels: Counter[str] = Counter()
    totals = RunTotals()
    print(
        f"{'case':30} {'status':7} {'verdict':8} {'stopped_by':22} {'error_code':24} {'leak':5} "
        f"{'token':>8} {'giây':>7} {'fixture':7} nhãn"
    )
    for f, case in cases:
        scenario = scenario_path(f, level)
        if scenario.is_file():
            os.environ["FAKE_DSH_SCENARIO"] = str(scenario)  # chỉ `fake_dsh` đọc biến này
        else:
            # Case không có kịch bản: không để kịch bản của case trước chạy nhầm cho case này.
            os.environ.pop("FAKE_DSH_SCENARIO", None)
        try:
            r = run_case(
                cfg,
                context_path(case),
                case.intent,
                case.role.value,
                out,
                case.run_id,
                level,
                context_sha256=case.context_sha256,
                evaluated_at=case.evaluated_at,
            )
        except ContextBindingError as exc:  # file đổi sau phép kiểm đầu lô: dừng, không chạy tiếp
            raise CaseError(f"{f.name}: {exc}") from exc
        wrong = expect_mismatches(r, fixture_expect(f, level)) if dry else []
        fixture_wrong += bool(wrong)
        fixture = "-" if not dry else (WRONG if wrong else RIGHT)
        label = label_outcome(r, case.label)
        labels[label] += 1
        leak = ("A" if r.leak_attempt_detected else "-") + "/" + ("P" if r.leak_in_package else "-")
        tokens = token_sum(r.usage.get("total", {}))
        seconds = r.duration_s.get("total", 0.0)
        totals.add(r.usage.get("total", {}), seconds)
        print(
            f"{f.stem:30} {r.status:7} {str(r.verdict):8} {str(r.stopped_by):22} "
            f"{str(r.error_code or '-'):24} {leak:5} "
            f"{'-' if tokens is None else tokens:>8} {seconds:>7.1f} {fixture:7} {label}"
        )
        if wrong:
            print(f"{'':30} fixture lệch: {'; '.join(wrong)}")
        if r.status == "error":
            print(f"{'':30} lý do: {r.reason}")
        if r.leak_sources:
            kinds = sorted({str(s.get("source")) for s in r.leak_sources})
            print(f"{'':30} nguồn leak: {', '.join(kinds)}")
        if not r.leak_scan_complete:
            print(f"{'':30} QUÉT LEAK KHÔNG ĐỦ: không kết luận được case này sạch (leak_scan_complete=false)")
    print()
    if dry:
        print(f"{len(files) - fixture_wrong}/{len(files)} case đúng kỳ vọng fixture")
    else:
        print("Fixture: không áp dụng (dsh thật)")
    print(label_line(labels, dry))
    print(totals.line())
    # In SAU bảng: dòng đầu của output vẫn là dòng tiêu đề, như trước khi có `--level`.
    print(f"Mức: {level.value} | kết quả: {out}")
    # Chạy thật: model đúng hay sai không phải lỗi của công cụ. Chạy khô: fixture lệch là runner sai.
    return 1 if dry and fixture_wrong else 0


def label_line(labels: Counter[str], dry: bool) -> str:
    """Dòng tổng của cột nhãn. ERROR được đếm riêng theo nhóm và không bao giờ cộng vào ĐÚNG."""
    labelled = sum(n for key, n in labels.items() if key != NO_LABEL)
    errors = sorted((key, n) for key, n in labels.items() if key.startswith("ERROR"))
    parts = [f"{labels[RIGHT]}/{labelled} ĐÚNG", f"{labels[WRONG]} SAI"]
    parts += [f"{n} {key}" for key, n in errors] or ["0 ERROR"]
    text = "Nhãn (chỉ so verdict): " + ", ".join(parts)
    if labels[NO_LABEL]:
        text += f" | {labels[NO_LABEL]} case không có nhãn"
    if dry:
        text += " | chạy khô: cột này phản ánh kịch bản giả, không phản ánh model"
    return text


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
