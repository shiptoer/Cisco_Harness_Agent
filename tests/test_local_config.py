"""LocalConfig.load: đọc được config/local.toml do công cụ trên Windows ghi ra.

PowerShell 5.1 (`Set-Content -Encoding UTF8`, `Out-File -Encoding UTF8`) và Notepad ghi UTF-8 kèm BOM.
`tomllib` không nhận BOM: trước khi sửa, một file hợp lệ có BOM làm runner dừng với
"Invalid statement (at line 1, column 1)".
"""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

from harness_agent.runner.dsh import DshSetupError, LocalConfig


def _toml(tmp_path: Path) -> str:
    python = Path(sys.executable).as_posix()
    return (
        "# cấu hình cục bộ\n"
        f'[dsh]\ncommand = ["{python}", "scripts/fake_dsh.py"]\nhome = "{(tmp_path / "home").as_posix()}"\n'
        'base_patch = "tests/fixtures/dsh.base.patch.yml"\nrequires_api_key = false\n'
        f'[python]\nexe = "{python}"\n'
    )


def test_config_with_utf8_bom_loads_like_one_without(tmp_path: Path):
    plain, with_bom = tmp_path / "plain.toml", tmp_path / "bom.toml"
    plain.write_bytes(_toml(tmp_path).encode("utf-8"))
    with_bom.write_bytes(codecs.BOM_UTF8 + _toml(tmp_path).encode("utf-8"))
    assert with_bom.read_bytes()[:3] == b"\xef\xbb\xbf"  # đối chứng: file thật sự có BOM

    assert LocalConfig.load(with_bom) == LocalConfig.load(plain)
    assert LocalConfig.load(with_bom).python_exe == Path(sys.executable)


def test_utf16_config_is_a_setup_error_that_names_the_cause(tmp_path: Path):
    """PowerShell 5.1 `>` ghi UTF-16 (F8). Không đọc được thì phải là lỗi cấu hình có hướng dẫn, không
    phải traceback."""
    path = tmp_path / "utf16.toml"
    path.write_bytes(_toml(tmp_path).encode("utf-16"))
    with pytest.raises(DshSetupError, match="UTF-8"):
        LocalConfig.load(path)


def test_invalid_toml_is_a_setup_error(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text("[dsh\ncommand = ", encoding="utf-8")
    with pytest.raises(DshSetupError, match="TOML"):
        LocalConfig.load(path)
