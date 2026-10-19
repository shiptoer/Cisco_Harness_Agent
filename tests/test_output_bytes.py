"""File kết quả của một lượt chạy giống nhau từng byte trên mọi hệ điều hành (D-022).

Trước đây các file này được ghi ở chế độ text mặc định: trên Windows mỗi `\\n` thành `\\r\\n`, trên Linux
thì không. Ở lượt chạy thật đầu tiên (01/10/2026, Windows) `candidate/SW2.cfg` dài 112 byte, trong đó 6 byte
là CR; cùng nội dung ghi bằng LF là 106 byte. Hash và mọi phép so theo byte của change package vì thế phụ
thuộc máy chạy.

Hai lớp kiểm:
1. Mọi file do runner và harness ghi: UTF-8 không BOM, chỉ xuống dòng bằng LF.
2. Các file có nội dung xác định (không chứa thời gian, đường dẫn hay số ngẫu nhiên) khớp sha256 với bản
   golden trong `tests/golden/dry_run_sha256.json`. Bản golden sinh trên Windows; CI chạy Linux, nên test
   này xanh trên CI nghĩa là hai hệ điều hành cho ra đúng cùng byte.

Sinh lại golden sau khi cố ý đổi output (template, định dạng change package, kịch bản chạy khô):

    .venv\\Scripts\\python.exe tests\\test_output_bytes.py
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from harness_agent.levels import Level
from harness_agent.runner.cases import case_files, fixture_expect, scenario_path
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import run_case

ROOT = Path(__file__).resolve().parents[1]
CASES = case_files()  # 14 case có nhãn + 2 case fixture (`fault-*`)
GOLDEN_FILE = ROOT / "tests" / "golden" / "dry_run_sha256.json"

# File có nội dung xác định: cùng case thì cùng byte ở mọi lần chạy, mọi máy.
GOLDEN_NAMES = (
    "change_package.md",
    "change_request.json",
    "staged_ops.jsonl",
    "rejects.jsonl",
    "finalized.json",
    "TERMINATED",
)
# Bản sao nguyên byte của stdout/stderr của dsh: runner không quyết định nội dung của chúng.
RAW_CAPTURE_SUFFIXES = (".ndjson", ".stderr.txt")
DSH_OWNED_DIRS = (".dsh_home",)


def _fake_cfg(tmp: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp / "unused_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def run_all_cases(out_root: Path) -> dict[str, Path]:
    """Chạy khô cả bộ case một lần; trả về run-dir của từng case."""
    previous = os.environ.get("FAKE_DSH_SCENARIO")
    runs: dict[str, Path] = {}
    try:
        for case_file in CASES:
            case = json.loads(case_file.read_text(encoding="utf-8"))
            os.environ["FAKE_DSH_SCENARIO"] = str(scenario_path(case_file, Level.B3))
            result = run_case(
                _fake_cfg(out_root),
                ROOT / case["context"],
                case["intent"],
                case["role"],
                out_root,
                case_file.stem,
            )
            runs[case_file.stem] = Path(result.run_dir)
    finally:
        if previous is None:
            os.environ.pop("FAKE_DSH_SCENARIO", None)
        else:
            os.environ["FAKE_DSH_SCENARIO"] = previous
    return runs


def written_by_us(run_dir: Path) -> list[Path]:
    """File do runner hoặc harness ghi: mọi file trong run-dir, trừ thư mục của dsh và output thô của dsh."""
    files = []
    for path in sorted(run_dir.rglob("*")):
        rel = path.relative_to(run_dir)
        if not path.is_file() or rel.parts[0] in DSH_OWNED_DIRS or path.name.endswith(RAW_CAPTURE_SUFFIXES):
            continue
        files.append(path)
    return files


def deterministic_hashes(runs: dict[str, Path]) -> dict[str, dict[str, str]]:
    hashes: dict[str, dict[str, str]] = {}
    for name, run_dir in runs.items():
        files = [run_dir / n for n in GOLDEN_NAMES] + sorted((run_dir / "candidate").glob("*.cfg"))
        hashes[name] = {
            p.relative_to(run_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
            if p.is_file()
        }
    return hashes


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, Path]:
    return run_all_cases(tmp_path_factory.mktemp("bytes"))


@pytest.mark.slow
def test_every_file_we_write_is_utf8_without_bom_and_lf_only(runs: dict[str, Path]):
    checked = 0
    for name, run_dir in runs.items():
        for path in written_by_us(run_dir):
            raw = path.read_bytes()
            where = f"{name}/{path.relative_to(run_dir).as_posix()}"
            assert b"\r" not in raw, f"{where}: có ký tự CR (file được ghi ở chế độ text của hệ điều hành)"
            assert not raw.startswith(codecs.BOM_UTF8), f"{where}: có BOM"
            raw.decode("utf-8")  # phải là UTF-8 hợp lệ
            checked += 1
    # Mỗi case có ít nhất result.json, trace.jsonl, một prompt và một patch.
    assert checked >= 4 * len(runs)


@pytest.mark.slow
def test_expected_files_are_covered(runs: dict[str, Path]):
    """Đối chứng cho test trên: danh sách file được kiểm đúng là thứ một case accept sinh ra."""
    names = {p.relative_to(runs["clean-vlan30"]).as_posix() for p in written_by_us(runs["clean-vlan30"])}
    assert names == {
        "candidate/SW2.cfg",
        "cba.patch.yml",
        "cba.prompt.txt",
        "change_package.md",
        "change_request.json",
        "finalized.json",
        "result.json",
        "sensors.json",
        "staged_ops.jsonl",
        "trace.jsonl",
        "tsa.patch.yml",
        "tsa.prompt.txt",
    }


@pytest.mark.slow
def test_deterministic_outputs_match_the_golden_hashes(runs: dict[str, Path]):
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    actual = deterministic_hashes(runs)
    assert set(actual) == set(golden), "bộ case đã đổi: sinh lại golden (xem docstring của file)"
    for name in sorted(golden):
        assert actual[name] == golden[name], f"{name}: output khác golden"


def test_golden_covers_a_change_package_for_every_accept_case():
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    accept = [f.stem for f in CASES if fixture_expect(f, Level.B3).get("verdict") == "accept"]
    assert len(accept) >= 4
    for name in accept:
        assert "change_package.md" in golden[name]
        assert any(rel.startswith("candidate/") for rel in golden[name]), name


@pytest.mark.slow
def test_crlf_version_of_a_golden_file_has_a_different_hash(runs: dict[str, Path]):
    """Đối chứng âm: đúng nội dung đó nhưng xuống dòng kiểu Windows thì KHÔNG khớp golden, tức phép so
    sha256 bắt được việc ghi file ở chế độ text của hệ điều hành."""
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    raw = (runs["clean-vlan30"] / "candidate" / "SW2.cfg").read_bytes()
    assert raw.count(b"\n") >= 5
    as_windows = raw.replace(b"\n", b"\r\n")
    assert hashlib.sha256(as_windows).hexdigest() != golden["clean-vlan30"]["candidate/SW2.cfg"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, không in được tiếng Việt
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        hashes = deterministic_hashes(run_all_cases(Path(tmp)))
    GOLDEN_FILE.parent.mkdir(exist_ok=True)
    text = json.dumps(hashes, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    GOLDEN_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"đã ghi {GOLDEN_FILE.relative_to(ROOT)}: {len(hashes)} case, {sum(map(len, hashes.values()))} file"
    )


def _write_text_without_lf(source: str) -> list[int]:
    """Số dòng của mọi lời gọi `.write_text(...)` không có `newline="\\n"`: trên Windows chúng ghi CRLF
    (D-022)."""
    import ast

    bad = []
    for node in ast.walk(ast.parse(source)):
        func = node.func if isinstance(node, ast.Call) else None
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            newline = next((k.value for k in node.keywords if k.arg == "newline"), None)
            if not (isinstance(newline, ast.Constant) and newline.value == "\n"):
                bad.append(node.lineno)
    return bad


def test_scripts_always_write_lf():
    """D-022: file do `scripts/` ghi (index.json của run_corpus, summary của evaluate…) là LF trên mọi máy."""
    scripts = ROOT / "scripts"
    offenders = {
        p.name: lines
        for p in sorted(scripts.glob("*.py"))
        if (lines := _write_text_without_lf(p.read_text(encoding="utf-8")))
    }
    assert offenders == {}
    # Đối chứng âm: đúng dòng cũ của run_corpus.py bị bắt.
    old = '(out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")'
    assert _write_text_without_lf(old) == [1]
