"""scripts/check_setup.py: thông báo khi file chạy dsh trong `[dsh] command` không tồn tại.

dsh 0.2.0-rc.1 khai `"bin": {"dsh": "lib/bin.js"}`. Ghi nhầm `.../dsh/bin.js` là lỗi đã gặp; thông báo
phải chỉ vào trường "bin" của package.json thay vì để người dùng đoán tên file.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("check_setup", ROOT / "scripts" / "check_setup.py")
check_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_setup)


def _package(tmp_path: Path, manifest: dict) -> Path:
    pkg = tmp_path / "node_modules" / "@deepseek-ai" / "dsh"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "lib" / "bin.js").write_text("// dsh", encoding="utf-8")
    (pkg / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pkg


def test_wrong_js_path_reports_the_declared_bin(tmp_path: Path):
    pkg = _package(tmp_path, {"name": "@deepseek-ai/dsh", "bin": {"dsh": "lib/bin.js"}})
    wrong = pkg / "bin.js"

    message = check_setup.missing_file_message(str(wrong))

    assert str(wrong) in message
    assert '"bin": {"dsh": "lib/bin.js"}' in message
    assert str(pkg / "lib" / "bin.js") in message


def test_bin_declared_as_a_string(tmp_path: Path):
    pkg = _package(tmp_path, {"name": "x", "bin": "lib/bin.js"})
    message = check_setup.missing_file_message(str(pkg / "cli.js"))
    assert str(pkg / "lib" / "bin.js") in message


def test_no_package_json_points_to_the_bin_field_instead_of_guessing(tmp_path: Path):
    message = check_setup.missing_file_message(str(tmp_path / "nowhere" / "bin.js"))
    assert '"bin"' in message and "package.json" in message and "ConvertFrom-Json" in message
    assert "lib/bin.js" not in message  # không đoán tên file khi không đọc được package.json


def test_package_json_without_bin_gives_the_generic_hint(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "spike"}), encoding="utf-8")
    message = check_setup.missing_file_message(str(tmp_path / "bin.js"))
    assert "ConvertFrom-Json" in message


def test_non_js_file_keeps_the_plain_message(tmp_path: Path):
    missing = tmp_path / "node.exe"
    assert check_setup.missing_file_message(str(missing)) == f"không tồn tại: {missing}"
