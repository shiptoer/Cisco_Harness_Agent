"""Chạy một case ở một mức ablation: `--level B0..B4` (mặc định B3; D-049). B0 là baseline (D-044).

Ví dụ (PowerShell, đứng ở thư mục gốc repo, venv đã kích hoạt):

    python -m harness_agent.runner `
        --context data/contexts/campus-small-01.json `
        --role network_admin `
        --intent "Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 vào VLAN 30"

Hoặc chạy từ file case JSON: python -m harness_agent.runner --case data/cases/clean-vlan30.json

B0 và B1 gửi nguyên văn context, kể cả giá trị secret, cho model: chỉ chạy thật với context có secret giả.

Freshness binding (D-054): `--context-sha256` ghim hash của file context (lệch thì dừng, mã 2, không chạy gì);
`--evaluated-at` (ISO 8601 có múi giờ, vd 2026-10-13T12:00:00Z) là "now" để kiểm TTL của context collector ở
B2+. Runner không đọc đồng hồ máy: context collector mà thiếu `--evaluated-at` là lỗi (mã 2). Với `--case`,
hai giá trị lấy từ khoá `context_sha256`, `evaluated_at` của file case.

`--confirmed-intent` và `--prepare-only` tắt mặc định và KHÔNG dùng trong lô đo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from harness_agent.harness.roles import ROLE_NAMES
from harness_agent.levels import add_level_arguments, level_from_args

from .cases import scenario_path
from .context_binding import ContextBindingError
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
    ap.add_argument("--context-sha256", help="SHA-256 ghim của file context (D-054)")
    ap.add_argument("--evaluated-at", help="'now' của lượt chạy, ISO 8601 có múi giờ (D-054)")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--run-id")
    add_level_arguments(ap)
    ap.add_argument(
        "--confirmed-intent",
        help="file ý định đã xác nhận (B1–B4); tắt mặc định, KHÔNG dùng trong lô đo",
    )
    ap.add_argument(
        "--prepare-only",
        action="store_true",
        help="dừng ở pending_review sau CBA (B2–B4); tắt mặc định, KHÔNG dùng trong lô đo",
    )
    ap.add_argument(
        "--keep-dsh-home",
        action="store_true",
        help="giữ session log của dsh trong <run-dir>/.dsh_home sau khi quét (mặc định: xoá)",
    )
    a = ap.parse_args(argv)
    level = level_from_args(ap, a)

    if a.case:
        case = json.loads(Path(a.case).read_text(encoding="utf-8"))
        context, role, intent = case["context"], case["role"], case["intent"]
        run_id = a.run_id or case.get("run_id")
        pinned, evaluated = case.get("context_sha256"), case.get("evaluated_at")
        # Kịch bản `fake_dsh` tìm theo tên file case (D-045).
        scenario = scenario_path(Path(a.case), level)
        if scenario.is_file():
            # Chỉ scripts/fake_dsh.py đọc biến này; dsh thật bỏ qua.
            os.environ.setdefault("FAKE_DSH_SCENARIO", str(scenario))
    else:
        if not (a.context and a.role and a.intent):
            ap.error("cần --case, hoặc đủ --context --role --intent")
        context, role, intent, run_id = a.context, a.role, a.intent, a.run_id
        pinned, evaluated = a.context_sha256, a.evaluated_at
    evaluated_at = _aware(ap, evaluated) if evaluated is not None else None

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
            level,
            confirmed_intent_path=Path(a.confirmed_intent) if a.confirmed_intent else None,
            prepare_only=a.prepare_only,
            context_sha256=pinned,
            evaluated_at=evaluated_at,
        )
    except DshSetupError as exc:
        print(f"[LỖI CẤU HÌNH] {exc}", file=sys.stderr)
        return 2
    except ContextBindingError as exc:
        print(f"[LỖI DỮ LIỆU CASE] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.status in {"ok", "pending_review"} else 1


def _aware(ap: argparse.ArgumentParser, text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        ap.error(f"evaluated_at không phải ISO 8601: {text!r}")
    if moment.tzinfo is None:
        ap.error("evaluated_at phải có múi giờ (vd ...Z)")
    return moment


if __name__ == "__main__":
    raise SystemExit(main())
