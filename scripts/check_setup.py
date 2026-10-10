"""Kiểm tra cài đặt trước khi chạy thật: python scripts/check_setup.py --config config/local.toml

Không gọi LLM. Kiểm tra: config đọc được, node/dsh tồn tại, dsh chạy được `--help`,
base patch hợp lệ (đủ 20 row đã tắt, đúng 1 khối MCP client), harness server khởi động được
bằng đúng python mà dsh sẽ dùng, và context mẫu hợp lệ.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from harness_agent.harness.context_store import ContextError, ContextStore
from harness_agent.runner.dsh import REPO_ROOT, DshSetupError, LocalConfig, build_patch, preflight
from harness_agent.runner.model import composed_model
from harness_agent.runner.pipeline import lock_workspace


def workspace_status(cfg: LocalConfig) -> str:
    """Tạo (nếu chưa có) và thử khoá thư mục làm việc của dsh (D-025). Lượt chạy khác đang giữ khoá, hoặc
    thư mục không rỗng mà không do runner tạo, thì báo lỗi ở đây thay vì giữa lô."""
    if cfg.dsh_workspace is None:
        return "thư mục tạm riêng cho từng case (chỉ dùng cho chạy khô)"
    with lock_workspace(cfg):
        return f"{cfg.dsh_workspace} (khoá đang rảnh; dsh gửi đường dẫn này cho model, giống nhau ở mọi case)"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/local.toml")
    ap.add_argument("--context", default="data/contexts/campus-small-01.json")
    a = ap.parse_args()
    ok = True

    def check(name: str, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"[OK]   {name}{': ' + detail if detail else ''}")
        except Exception as exc:  # noqa: BLE001 — muốn in mọi lỗi cho người dùng
            ok = False
            print(f"[LỖI]  {name}: {exc}")

    cfg_holder: dict[str, LocalConfig] = {}

    def load_cfg():
        cfg_holder["c"] = LocalConfig.load(a.config)
        return a.config

    check("đọc config", load_cfg)
    if "c" not in cfg_holder:
        return 2
    cfg = cfg_holder["c"]
    check("python cho harness", lambda: str(cfg.python_exe))
    check("thư mục làm việc của dsh", lambda: workspace_status(cfg))
    for part in cfg.dsh_command:
        if "/" in part or "\\" in part:
            check(
                f"tồn tại {Path(part).name}",
                lambda p=part: str(p) if Path(p).exists() else _raise(missing_file_message(p)),
            )

    def dsh_help():
        proc = subprocess.run([*cfg.dsh_command, "--help"], capture_output=True, timeout=60, check=False)  # noqa: S603
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
        return "dsh --help chạy được"

    check("gọi dsh", dsh_help)
    check("context mẫu", lambda: f"{len(ContextStore.load(a.context).hostnames)} thiết bị")
    check("model đã ghim (config/model.toml)", lambda: cfg.model().describe())
    with tempfile.TemporaryDirectory() as tmp:
        check(
            "base patch",
            lambda: build_patch(cfg, "cba", Path(a.context).resolve(), "network_admin", Path(tmp)).name,
        )

        def dsh_model():
            # Hỏi chính dsh xem sau khi áp patch nó sẽ dùng model nào. `--dump-config` chỉ in cấu hình đã
            # ghép rồi thoát: không khởi động agent, không gọi API. DSH_HOME tạm để không đụng ~/.dsh.
            patch = build_patch(cfg, "cba", Path(a.context).resolve(), "network_admin", Path(tmp))
            env = os.environ.copy()
            env.update(DSH_HOME=str(Path(tmp) / "dsh_home"), DSH_TELEMETRY_DISABLED="1")
            cmd = [*cfg.dsh_command, "--profile", "headless", "--patch", str(patch), "--dump-config"]
            proc = subprocess.run(cmd, capture_output=True, env=env, timeout=120, check=False)  # noqa: S603
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
            seen = composed_model(proc.stdout.decode("utf-8", "replace"))
            want = cfg.model().patch_row()["config"]
            if seen != want:
                raise RuntimeError(f"dsh sẽ dùng {seen}, khác config/model.toml {want}")
            return f"{seen['provider']}/{seen['model']}, reasoningEffort={seen['reasoningEffort']}"

        if cfg.requires_api_key:  # chỉ với dsh thật; fake_dsh không có --dump-config
            check("model dsh sẽ dùng (dsh --dump-config)", dsh_model)
        check(
            "harness preflight stage cba",
            lambda: ", ".join(preflight(cfg, "cba", Path(a.context).resolve(), "network_admin", Path(tmp))),
        )
    if cfg.requires_api_key:
        check(
            "API key trong terminal",
            lambda: "có" if os.environ.get("DEEPSEEK_API_KEY") else _raise("chưa có"),
        )
    print("\nSẴN SÀNG CHẠY THẬT" if ok else "\nCÒN LỖI — sửa các dòng [LỖI] trước khi chạy thật")
    print(f"(repo: {REPO_ROOT})")
    return 0 if ok else 1


def _raise(msg: str):
    raise FileNotFoundError(msg)


_JS_SUFFIXES = {".js", ".mjs", ".cjs"}
_READ_BIN = "(Get-Content <thư mục gói dsh>\\package.json -Raw | ConvertFrom-Json).bin"


def missing_file_message(path: str) -> str:
    """Thông báo khi một file trong `[dsh] command` không tồn tại.

    Với file JS của dsh: KHÔNG đoán tên file. File chạy là giá trị trường "bin" trong package.json của
    gói (dsh 0.2.0-rc.1 khai `lib/bin.js`, không phải `bin.js`). Nếu tìm thấy package.json có khai "bin"
    ở một thư mục cha của đường dẫn sai thì in luôn giá trị đọc được.
    """
    target = Path(path)
    if target.suffix.lower() not in _JS_SUFFIXES:
        return f"không tồn tại: {path}"
    for parent in target.parents:
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8")).get("bin")
        except (OSError, ValueError, AttributeError):
            break
        if declared:
            files = list(declared.values()) if isinstance(declared, dict) else [declared]
            expected = ", ".join(str(parent / str(f)) for f in files)
            return (
                f'không tồn tại: {path}. {manifest} khai "bin": {json.dumps(declared)} '
                f"→ file chạy là: {expected}. Sửa [dsh] command trong config."
            )
        break
    return (
        f'không tồn tại: {path}. Đừng đoán tên file: đọc trường "bin" trong package.json của gói dsh '
        f"— {_READ_BIN} — rồi sửa [dsh] command trong config."
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DshSetupError, ContextError) as exc:
        print(f"[LỖI] {exc}")
        raise SystemExit(2) from exc
