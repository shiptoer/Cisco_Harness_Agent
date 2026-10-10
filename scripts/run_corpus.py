"""Chạy cả corpus (data/corpus) qua pipeline ở một mức ablation.

    python scripts/run_corpus.py --config config/fake.toml --level B4              # chạy khô, oracle scenario
    python scripts/run_corpus.py --config config/fake.toml --level B0 B1 B2 B3 B4  # mọi mức, một lệnh
    python scripts/run_corpus.py --config config/local.toml --level B4 --subset clean --limit 5
    python scripts/run_corpus.py --config config/local.toml --level B4 --repeat 3  # lặp k lần (model thật)

Kết quả: <out>/<level>/<case_id>[.rN]/ (run-dir chuẩn của runner) + <out>/<level>/index.json.
Sau đó: python scripts/evaluate.py --runs runs/corpus
Với dsh thật, mỗi case là một (hoặc hai) lượt gọi API: dùng --limit/--subset khi thử.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from harness_agent.levels import DEFAULT_LEVEL, LEVEL_NAMES
from harness_agent.models import TestCase, load_context
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.pipeline import run_case


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/local.toml")
    ap.add_argument("--corpus", default="data/corpus")
    ap.add_argument("--out", default="runs/corpus")
    ap.add_argument("--level", nargs="+", default=[DEFAULT_LEVEL.value], choices=LEVEL_NAMES)
    ap.add_argument("--subset", nargs="*", default=None, help="chỉ chạy các subset này")
    ap.add_argument("--limit", type=int, default=None, help="số case tối đa mỗi mức")
    ap.add_argument(
        "--repeat", type=int, default=1, help="chạy lặp k lần mỗi case (model thật không tất định)"
    )
    a = ap.parse_args()
    try:
        cfg = LocalConfig.load(a.config)
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2

    corpus = Path(a.corpus)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    cases = []
    for meta in manifest["cases"].values():
        case = TestCase.model_validate_json((corpus / meta["file"]).read_text(encoding="utf-8"))
        if a.subset and case.subset.value not in a.subset:
            continue
        cases.append((case, meta))
    if a.limit:
        cases = cases[: a.limit]

    for level in a.level:
        out = Path(a.out) / level
        ctx_dir = out / "_contexts"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        index: dict[str, dict] = {}
        t0 = time.time()
        print(f"== mức {level}: {len(cases)} case × {a.repeat} lần → {out}")
        for case, meta in cases:
            load_context(case.context_ref, corpus)  # verify path and hash before invoking runner
            ctx_path = (corpus / case.context_ref.path).resolve()
            if meta.get("fake_scenario"):
                # Chỉ fake_dsh đọc biến này; dsh thật bỏ qua.
                os.environ["FAKE_DSH_SCENARIO"] = str((corpus / meta["fake_scenario"]).resolve())
            for k in range(a.repeat):
                run_id = case.case_id if a.repeat == 1 else f"{case.case_id}.r{k + 1}"
                r = run_case(cfg, ctx_path, case.intent, case.role.value, out, run_id, level)
                index[run_id] = {
                    "case_id": case.case_id,
                    "run_dir": r.run_dir,
                    "status": r.status,
                    "verdict": r.verdict,
                }
                tag = r.verdict if r.status == "ok" else f"ERROR:{r.error_code}"
                print(f"  {run_id:36} {tag:24} {r.stopped_by or ''}")
        (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   xong {len(index)} lượt trong {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
