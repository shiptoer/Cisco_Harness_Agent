"""Sinh benchmark v1 (bộ dataset chuẩn chấm độ chính xác đầu ra, D-065) từ seed, tất định.

    python scripts/gen_benchmark.py                                  # seed 42, per_tool 4 → data/benchmark/v1
    python scripts/gen_benchmark.py --seed 7 --per-tool 2 --out runs-bm/seed7

Mặc định dùng hai context `data/contexts/campus-small-01.json` và
`data/contexts/smb-01-collected-ref-l2.json`.
Cùng tham số → cùng từng byte (sha256 của mọi file trong manifest.json; `tests/test_benchmark.py` ghim
`data/benchmark/v1` vào output của lệnh mặc định). Thư mục đích phải rỗng hoặc là một benchmark cũ (bị ghi
đè).

Context dẫn xuất smb-01 (platform `iosvl2` cho năm switch IOL L2, D-065), sinh lại từ snapshot SSH gốc:
    python scripts/gen_benchmark.py --derive     # ghi data/contexts/smb-01-collected-ref-l2.json rồi thoát

Sau đó (chạy khô bằng oracle scenario, không cần API key):
    python scripts/run_corpus.py --config config/fake.toml --corpus data/benchmark/v1 --level B4 --out runs/bm
    python scripts/evaluate.py --benchmark data/benchmark/v1 --runs runs/bm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness_agent.corpus.benchmark import (
    DEFAULT_CONTEXTS,
    DEFAULT_PER_TOOL,
    DEFAULT_SEED,
    SMB01_L2,
    SMB01_REF,
    BenchmarkError,
    derive_l2_context,
    gen_benchmark,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument(
        "--per-tool", type=int, default=DEFAULT_PER_TOOL, help="số case mỗi tool trên mỗi context"
    )
    ap.add_argument("--context", nargs="+", default=list(DEFAULT_CONTEXTS))
    ap.add_argument("--out", default="data/benchmark/v1")
    ap.add_argument("--derive", action="store_true", help=f"chỉ ghi {SMB01_L2} từ {SMB01_REF}")
    a = ap.parse_args()
    if a.derive:
        data = derive_l2_context(Path(SMB01_REF).read_bytes())
        Path(SMB01_L2).write_bytes(data)
        print(f"đã ghi {SMB01_L2} ({len(data)} byte) từ {SMB01_REF}")
        return 0
    if a.per_tool < 1:
        print("--per-tool phải >= 1", file=sys.stderr)
        return 2
    try:
        manifest = gen_benchmark(a.seed, [Path(p) for p in a.context], a.per_tool, Path(a.out))
    except (BenchmarkError, ValueError, OSError) as exc:
        print(f"[LỖI] {exc}", file=sys.stderr)
        return 2
    c = manifest["counts"]
    print(
        f"đã ghi {c['cases']} case vào {a.out}: {c['single_op']} một thao tác + {c['multi_op']} đa thao tác; "
        f"dev {c['dev']}, test {c['test']}; {c['families']} family, {c['templates']} template"
    )
    print(f"theo context: {c['by_context']}")
    print(f"seed={a.seed}, per_tool={a.per_tool}, version={manifest['benchmark_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
