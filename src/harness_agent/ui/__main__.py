"""Khởi động giao diện vận hành (D-057): duyệt change package, KHÔNG deploy, không LLM agent đứng ngoài.

    # Chạy khô (fake_dsh, chọn kịch bản theo tên; kết quả do kịch bản, không do intent):
    cha-ui --config config/fake.toml --context data/contexts/campus-small-01.json `
        --role network_admin --level B3 --reviewer "Duẩn"
    # Chạy thật: key chỉ lấy từ biến môi trường DEEPSEEK_API_KEY của terminal này (Read-Host -AsSecureString)
    cha-ui --config config/local.toml --context <file> --role <role> --level B4
    --reviewer <tên>

Role, mức, context, cấu hình và người duyệt cố định lúc khởi động; trang không đổi được chúng. Server chỉ bind
127.0.0.1 và in một đường dẫn đăng nhập có token ngẫu nhiên; mở đúng đường dẫn đó trong trình duyệt của máy
này.
Kết quả ghi vào `--out` (mặc định `runs-ui`, nằm trong .gitignore). Không dùng trong lô đo.

`cha-ui`, `python -m harness_agent.ui` và lệnh cũ `python scripts/ui_server.py` (wrapper) là cùng một hàm
`main`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness_agent.harness.roles import ROLE_NAMES
from harness_agent.levels import Level
from harness_agent.ui.app import UiApp, UiError, UiSettings
from harness_agent.ui.web import Auth, make_server


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(
        prog="cha-ui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--config", required=True, help="config/fake.toml (chạy khô) hoặc config/local.toml (thật)"
    )
    ap.add_argument("--context", required=True, help="file network context JSON")
    ap.add_argument("--role", required=True, choices=ROLE_NAMES)
    ap.add_argument("--level", required=True, choices=("B3", "B4"))
    ap.add_argument("--reviewer", required=True, help="tên người duyệt, ghi vào bản duyệt trong trace")
    ap.add_argument("--out", default="runs-ui")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    try:
        app = UiApp(
            UiSettings(
                config=Path(a.config),
                context=Path(a.context),
                role=a.role,
                level=Level(a.level),
                reviewer=a.reviewer,
                out=Path(a.out),
            )
        )
    except (UiError, OSError) as exc:
        print(f"[LỖI] {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    auth = Auth()
    try:
        server = make_server(app, auth, a.port)
    except OSError as exc:
        print(
            f"[LỖI] không mở được cổng {a.port} trên 127.0.0.1 ({exc.strerror or exc}). "
            "Cổng đang bị một server khác giữ: đóng nó hoặc chạy lại với --port <cổng khác>.",
            file=sys.stderr,
        )
        return 2
    port = server.server_address[1]
    blocker = app.run_blocker()
    if blocker:
        print(f"[CẢNH BÁO] {blocker}", file=sys.stderr, flush=True)
    mode = "fake (kịch bản giả)" if app.fake else "thật (gọi model)"
    print(
        f"Giao diện vận hành: {mode}, role {a.role}, mức {a.level}, context {app.context.context_id}",
        flush=True,
    )
    print(f"Mở trong trình duyệt của máy này: http://127.0.0.1:{port}/login?token={auth.token}", flush=True)
    print("Không chia sẻ đường dẫn này. Ctrl+C để dừng.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
