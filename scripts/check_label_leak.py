"""Kiểm tra rò nhãn trong một hay nhiều run-dir (D-025): model có nhìn thấy tên case hay nhãn đáp án không.

    python scripts/check_label_leak.py runs/clean-vlan30
    python scripts/check_label_leak.py runs/*                       # mọi run-dir của một lô
    python scripts/check_label_leak.py runs/x --case data/cases/adv-protected-uplink.json

Đọc `*.prompt.txt`, phần model thấy được của `*.patch.yml`, và session log của dsh trong `.dsh_home` (có khi
lượt chạy dùng `--keep-dsh-home`). Tìm: `run_id` (tên run-dir), tên file case, và các chuỗi `adv-`, `clean-`,
`ambiguous-`, `fault-`, `expect`, `stopped_by`, `fake_scenario`, và các khoá của nhãn đúng (D-045):
`expected_verdict`, `expected_tool_calls`, `success_if_any`, `fixtures/cases`, `scenarios_b0`.
Chi tiết và giới hạn:
`src/harness_agent/runner/label_leak.py`.

Mã thoát: 0 = không thấy nhãn ở chỗ nào model nhìn thấy; 1 = có; 2 = không có gì để kiểm.
Chỉ in vị trí và vài chục ký tự quanh chỗ khớp.
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from harness_agent.runner.label_leak import Report, check_run_dir


def describe(report: Report) -> list[str]:
    lines = [
        f"== {report.run_dir}",
        f"   đã đọc: {report.prompts} prompt, {report.patches} patch, {report.session_logs} session log"
        + (f" (trong đó {report.fake_session_logs} do fake_dsh viết)" if report.fake_session_logs else ""),
    ]
    if report.real_session_logs == 0:
        why = (
            "session log ở đây do fake_dsh viết: chỉ mô phỏng hai câu có đường dẫn thư mục làm việc"
            if report.fake_session_logs
            else "không có session log (chạy lại với --keep-dsh-home để giữ)"
        )
        lines.append(
            f"   CHƯA KIỂM ĐƯỢC: system prompt và những gì dsh thật tự chèn vào prompt — {why}. "
            "Phần này chỉ kiểm được trên run-dir của lượt chạy thật."
        )
    for name in report.unreadable:
        lines.append(f"   KHÔNG ĐỌC ĐƯỢC: {name} (không biết trong đó có nhãn hay không)")
    for hit in report.hits:
        tag = "RÒ NHÃN" if hit.model_input else "ghi nhận (model tự viết ra, không tính là rò)"
        lines.append(f"   {tag}: {hit.label} | {hit.file} | {hit.where} | …{hit.snippet}…")
    lines.append(f"   KẾT QUẢ: {'CÓ RÒ NHÃN' if report.leaks else 'không thấy nhãn'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "run_dirs", nargs="+", type=Path, help="thư mục kết quả của một case, vd runs/clean-vlan30"
    )
    ap.add_argument("--case", type=Path, help="file case của run-dir (chỉ dùng khi kiểm đúng một run-dir)")
    a = ap.parse_args(argv)
    if a.case and len(a.run_dirs) != 1:
        ap.error("--case chỉ dùng với đúng một run-dir")

    # PowerShell không tự khai triển `runs\*` cho chương trình ngoài, nên khai triển ở đây.
    dirs: list[Path] = []
    for given in a.run_dirs:
        matches = sorted(glob.glob(str(given))) if glob.has_magic(str(given)) else [str(given)]
        dirs += [Path(m) for m in matches if Path(m).is_dir()]
    reports = [check_run_dir(d.resolve(), a.case) for d in dirs]
    for report in reports:
        print("\n".join(describe(report)))
    checked = [r for r in reports if r.prompts or r.patches or r.session_logs]
    leaking = [r for r in reports if r.leaks]
    unreal = [r for r in checked if r.real_session_logs == 0]
    print(
        f"\n{len(checked)} run-dir đã kiểm, {len(leaking)} có rò nhãn"
        + (f"; {len(unreal)} run-dir chưa kiểm được phần dsh thật chèn vào prompt" if unreal else "")
    )
    if leaking:
        return 1
    return 0 if checked else 2


if __name__ == "__main__":
    raise SystemExit(main())
