"""Chấm kết quả chạy corpus theo nhãn (evaluator độc lập, D-006) và xuất bảng so sánh các mức.

    python scripts/evaluate.py --runs runs/corpus                    # mọi mức có trong runs/corpus/<level>/
    python scripts/evaluate.py --runs runs/corpus --out reports/eval

Đọc: data/corpus/<case>.json (nhãn) + runs/corpus/<level>/<run_id>/{result.json, candidate/*.cfg}.
Ghi: <out>/cases_<level>.jsonl (từng case), <out>/summary.json, <out>/summary.md (bảng cho Ch4).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness_agent.eval import evaluate_case, load_result, markdown_table, summarise
from harness_agent.models import TestCase


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default="data/corpus")
    ap.add_argument("--runs", default="runs/corpus")
    ap.add_argument("--out", default="reports/eval")
    a = ap.parse_args()

    corpus = Path(a.corpus)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    cases = {
        cid: TestCase.model_validate_json((corpus / m["file"]).read_text(encoding="utf-8"))
        for cid, m in manifest["cases"].items()
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for level_dir in sorted(p for p in Path(a.runs).iterdir() if p.is_dir() and (p / "index.json").exists()):
        index = json.loads((level_dir / "index.json").read_text(encoding="utf-8"))
        evals = []
        skipped = 0
        for meta in index.values():
            case = cases.get(meta["case_id"])
            if case is None:
                skipped += 1  # case không có trong corpus hiện tại (corpus đã đổi?)
                continue
            result = load_result(Path(meta["run_dir"]))
            if result is None:
                # Không có result.json (runner chết giữa chừng): tính là ERROR, không bỏ khỏi mẫu số.
                result = {"status": "error", "error_code": "MISSING_RESULT", "level": level_dir.name}
            evals.append(evaluate_case(case, result, Path(meta["run_dir"]), context_root=corpus))
        level = level_dir.name
        with (out / f"cases_{level}.jsonl").open("w", encoding="utf-8") as f:
            for e in evals:
                f.write(json.dumps(e.as_dict(), ensure_ascii=False) + "\n")
        summaries[level] = summarise(evals)
        print(f"{level}: {len(evals)} lượt chấm, {skipped} bỏ qua (case không có trong corpus)")
        for subset, entry in summaries[level]["subsets"].items():
            print(f"   {subset:12} n={entry['n']:3} {entry['outcomes']}")
    if not summaries:
        print("không có mức nào để chấm (cần runs/<level>/index.json từ scripts/run_corpus.py)")
        return 1
    (out / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    table = markdown_table(summaries)
    (out / "summary.md").write_text("# Kết quả ablation theo mức\n\n" + table, encoding="utf-8")
    print("\n" + table)
    print(f"đã ghi {out / 'summary.json'} và {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
