"""Chấm kết quả chạy corpus theo nhãn (evaluator độc lập, D-006) và xuất bảng so sánh các mức.

    python scripts/evaluate.py --runs runs/corpus                    # mọi mức có trong runs/corpus/<level>/
    python scripts/evaluate.py --runs runs/corpus --out reports/eval
    python scripts/evaluate.py --runs runs/corpus --aggregate pass_k # luật gom lặp khác (mặc định majority)

Đọc: data/corpus/v2/<case>.json (nhãn) + runs/corpus/<level>/<run_id>/{result.json, candidate/*.cfg}.
Ghi: <out>/cases_<level>.jsonl (từng LƯỢT chạy), <out>/summary.json, <out>/summary.md (bảng cho Ch4).

Lặp k lần (`run_corpus --repeat k`): các lượt của một case được gom thành một (D-037, D-063) TRƯỚC khi tính tỉ
lệ, nên n là số case. Mọi tỉ lệ in kèm khoảng Wilson 95%; cột "Safe rate pass^k" luôn tính theo pass^k.
`run_dir` trong index.json là tương đối so với thư mục mức (POSIX); thiếu thì thử `<level>/<run_id>` và
cảnh báo.

Benchmark độ chính xác (D-065), chấm ở hai mức thao tác và CLI so với gold:
    python scripts/evaluate.py --benchmark data/benchmark/v1 --runs runs/bm   # ghi reports/benchmark/
Ghi: <out>/cases_<level>.jsonl, <out>/summary.json, <out>/summary.md. Bảng: accuracy ops strict/lenient,
CLI exact/lenient (kèm Wilson 95%) cho tổng, theo split, topology, tool; thêm pass^k khi có lặp.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness_agent.eval import aggregate_by_case, evaluate_case, load_result, markdown_table, summarise
from harness_agent.eval.benchmark_io import (
    load_benchmark,  # không qua corpus.benchmark (gate, grounding): D-006
)
from harness_agent.eval.evaluate import (
    benchmark_markdown_table,
    evaluate_benchmark_case,
    projection_mechanism,
    summarise_benchmark,
)
from harness_agent.models import TestCase


def resolve_run_dir(level_dir: Path, run_id: str, recorded: str | None) -> Path:
    """Đường dẫn run-dir của một lượt: `run_dir` trong index.json (tương đối so với thư mục mức, POSIX; bản cũ
    ghi tuyệt đối), rồi `level_dir / run_id` nếu đường dẫn ghi lại không tồn tại (thư mục kết quả đã chuyển
    máy). Không thấy ở đâu → cảnh báo; evaluate tính lượt đó là ERROR `MISSING_RESULT`."""
    candidates = []
    if recorded:
        p = Path(recorded)
        candidates.append(p if p.is_absolute() else level_dir / p)
    candidates.append(level_dir / run_id)
    for c in candidates:
        if c.is_dir():
            if c != candidates[0]:
                print(f"[CẢNH BÁO] {run_id}: không thấy {recorded!r}, dùng {c}", file=sys.stderr)
            return c
    print(f"[CẢNH BÁO] {run_id}: không thấy run-dir ({recorded!r} hay {level_dir / run_id})", file=sys.stderr)
    return candidates[-1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default="data/corpus/v2")
    ap.add_argument("--runs", default="runs/corpus")
    ap.add_argument("--out", default=None, help="mặc định reports/eval (corpus) hoặc reports/benchmark")
    ap.add_argument("--benchmark", default=None, help="thư mục benchmark (vd data/benchmark/v1): chấm gold")
    ap.add_argument(
        "--aggregate", choices=["majority", "pass_k"], default="majority", help="luật gom lượt lặp"
    )
    a = ap.parse_args()
    if a.benchmark:
        return run_benchmark(Path(a.benchmark), Path(a.runs), Path(a.out or "reports/benchmark"))
    a.out = a.out or "reports/eval"

    corpus = Path(a.corpus)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    cases = {
        cid: TestCase.model_validate_json((corpus / m["file"]).read_text(encoding="utf-8"))
        for cid, m in manifest["cases"].items()
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summaries = {}
    by_level = {}
    for level_dir in sorted(p for p in Path(a.runs).iterdir() if p.is_dir() and (p / "index.json").exists()):
        index = json.loads((level_dir / "index.json").read_text(encoding="utf-8"))
        evals = []
        skipped = 0
        for run_id, meta in index.items():
            case = cases.get(meta["case_id"])
            if case is None:
                skipped += 1  # case không có trong corpus hiện tại (corpus đã đổi?)
                continue
            run_dir = resolve_run_dir(level_dir, run_id, meta.get("run_dir"))
            result = load_result(run_dir)
            if result is None:
                # Không có result.json (runner chết giữa chừng): tính là ERROR, không bỏ khỏi mẫu số.
                result = {"status": "error", "error_code": "MISSING_RESULT", "level": level_dir.name}
            evals.append(evaluate_case(case, result, run_dir, context_root=corpus))
        level = level_dir.name
        with (out / f"cases_{level}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for e in evals:
                f.write(json.dumps(e.as_dict(), ensure_ascii=False) + "\n")
        # Gom lượt lặp theo case TRƯỚC khi tính tỉ lệ (D-037, D-063): n là số case, không phải số lượt.
        summaries[level] = summarise(aggregate_by_case(evals, a.aggregate))
        passk = summarise(aggregate_by_case(evals, "pass_k"))
        summaries[level]["aggregate"] = a.aggregate
        summaries[level]["pass_k"] = {"safe_rate": passk["safe_rate"], "ci": passk["ci"]["safe_rate"]}
        by_level[level] = aggregate_by_case(evals, a.aggregate)
        s = summaries[level]
        print(
            f"{level}: {len(evals)} lượt chấm = {s['n']} case × tối đa {s['repeats']} lần, "
            f"{skipped} bỏ qua (case không có trong corpus); gom theo {a.aggregate}"
        )
        for subset, entry in s["subsets"].items():
            rates = ", ".join(
                f"{k}={v:.1%} [{entry['ci'][k][0]:.1%}, {entry['ci'][k][1]:.1%}]"
                for k, v in entry.items()
                if k in entry.get("ci", {}) and v is not None and k != "error_rate"
            )
            print(f"   {subset:12} n={entry['n']:3} {entry['outcomes']}")
            if rates:
                print(f"   {'':12} {rates}")
    if not summaries:
        print("không có mức nào để chấm (cần runs/<level>/index.json từ scripts/run_corpus.py)")
        return 1
    mechanism = None
    if "B1" in by_level and "B2" in by_level:
        # Cơ chế của control (ii): thứ có trong input model ở B1 thì ở B2 không còn (corpus v2).
        mechanism = projection_mechanism(by_level["B1"], by_level["B2"])
    report = {**summaries, "projection_mechanism_B1_B2": mechanism} if mechanism else summaries
    (out / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    table = markdown_table(summaries)
    if mechanism:
        secret, payload = mechanism["input_secret"], mechanism["input_payload"]
        table += (
            f"\nCơ chế (ii), B1 → B2: {mechanism['mechanism_cases']}/{mechanism['cases']} case có input "
            f"lộ ở B1 và kín ở B2; secret {secret['b1_exposed']} lộ ở B1 → {secret['b2_contained']} kín "
            f"ở B2; free-text {payload['b1_exposed']} lộ ở B1 → {payload['b2_contained']} kín ở B2\n"
        )
    (out / "summary.md").write_text("# Kết quả ablation theo mức\n\n" + table, encoding="utf-8", newline="\n")
    print("\n" + table)
    print(f"đã ghi {out / 'summary.json'} và {out / 'summary.md'}")
    return 0


def _level_dirs(runs: Path) -> list[Path]:
    if not runs.is_dir():
        return []
    return sorted(p for p in runs.iterdir() if p.is_dir() and (p / "index.json").exists())


def run_benchmark(bench: Path, runs: Path, out: Path) -> int:
    """Chấm các lượt chạy benchmark (`run_corpus.py --corpus <bench>`) theo gold hai mức (D-065).

    Lượt không có result.json là ERROR `MISSING_RESULT` (không bỏ khỏi mẫu số). Case có trong benchmark mà
    không có lượt nào (vd chạy `--limit`) không được chấm: số case chấm được in ra cùng tổng số case."""
    try:
        manifest, cases, root = load_benchmark(bench)  # kiểm sha256 mọi file trước khi chấm
    except (OSError, ValueError) as exc:
        print(f"[LỖI] benchmark {bench}: {exc}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict] = {}
    tables: list[str] = []
    for level_dir in _level_dirs(runs):
        index = json.loads((level_dir / "index.json").read_text(encoding="utf-8"))
        evals = []
        skipped = 0
        for run_id, meta in index.items():
            case = cases.get(meta["case_id"])
            if case is None:
                skipped += 1  # lượt của case không có trong benchmark này
                continue
            run_dir = resolve_run_dir(level_dir, run_id, meta.get("run_dir"))
            result = load_result(run_dir)
            if result is None:
                result = {"status": "error", "error_code": "MISSING_RESULT", "level": level_dir.name}
            m = manifest["cases"][case.case_id]
            evals.append(
                evaluate_benchmark_case(
                    case,
                    result | {"level": level_dir.name},
                    run_dir,
                    root / "gold" / case.case_id,
                    root,
                    split=m["split"],
                )
            )
        level = level_dir.name
        with (out / f"cases_{level}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for e in evals:
                f.write(json.dumps(e.as_dict(), ensure_ascii=False) + "\n")
        if not evals:
            print(f"{level}: không có lượt nào của benchmark này ({skipped} lượt của case khác)")
            continue
        s = summarise_benchmark(evals)
        s["benchmark_cases"] = len(cases)
        summaries[level] = s
        print(
            f"{level}: {s['runs']} lượt = {s['n']}/{len(cases)} case × tối đa {s['repeats']} lần, "
            f"{skipped} bỏ qua; kết cục {s['overall']['outcomes']}"
        )
        tables.append(f"## {level}\n\n" + benchmark_markdown_table(s))
    if not summaries:
        print("không có mức nào để chấm (cần runs/<level>/index.json từ scripts/run_corpus.py)")
        return 1
    report = {
        "benchmark": {
            "version": manifest["benchmark_version"],
            "seed": manifest["seed"],
            "cases": len(cases),
            "oracle_note": "chạy bằng fake_dsh (oracle) không phải số đo model",
        },
        "levels": summaries,
    }
    (out / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    md = "# Benchmark độ chính xác đầu ra\n\n" + "\n".join(tables)
    (out / "summary.md").write_text(md, encoding="utf-8", newline="\n")
    print("\n" + "\n".join(tables))
    print(f"đã ghi {out / 'summary.json'} và {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
