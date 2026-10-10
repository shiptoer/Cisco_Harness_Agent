"""Chạy một case qua pipeline hai giai đoạn.

Ví dụ (PowerShell, đứng ở thư mục gốc repo, venv đã kích hoạt):

    python -m harness_agent.runner `
        --context data/contexts/campus-small-01.json `
        --role network_admin `
        --intent "Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 vào VLAN 30"

Hoặc chạy từ file case JSON: python -m harness_agent.runner --case data/cases/clean-vlan30.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness_agent.harness.roles import ROLE_NAMES
from harness_agent.levels import LEVEL_NAMES

from .dsh import DshSetupError, LocalConfig
from .pipeline import run_case


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252: không in được tiếng Việt
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/local.toml")
    ap.add_argument("--case", help="file case JSON có intent, role, context (và run_id tuỳ chọn)")
    ap.add_argument("--context", help="đường dẫn network context JSON")
    ap.add_argument("--role", choices=ROLE_NAMES)
    ap.add_argument("--intent")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--run-id")
    ap.add_argument("--level", default="B4", choices=LEVEL_NAMES)
    ap.add_argument("--confirmed-intent")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument(
        "--keep-dsh-home",
        action="store_true",
        help="giữ session log của dsh trong <run-dir>/.dsh_home sau khi quét (mặc định: xoá)",
    )
    a = ap.parse_args(argv)

    if a.case:
        case = json.loads(Path(a.case).read_text(encoding="utf-8"))
        context, role, intent = case["context"], case["role"], case["intent"]
        run_id = a.run_id or case.get("run_id")
        if case.get("fake_scenario"):
            # Chỉ scripts/fake_dsh.py đọc biến này; dsh thật bỏ qua.
            os.environ["FAKE_DSH_SCENARIO"] = str(Path(case["fake_scenario"]).resolve())
    else:
        if not (a.context and a.role and a.intent):
            ap.error("cần --case, hoặc đủ --context --role --intent")
        context, role, intent, run_id = a.context, a.role, a.intent, a.run_id

    try:
        cfg = LocalConfig.load(a.config)
        cfg.keep_dsh_home = cfg.keep_dsh_home or a.keep_dsh_home
        result = run_case(
            cfg,
            Path(context),
            intent,
            role,
            Path(a.out),
            run_id,
            a.level,
            confirmed_intent_path=Path(a.confirmed_intent) if a.confirmed_intent else None,
            prepare_only=a.prepare_only,
        )
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.status in {"ok", "pending_review"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
