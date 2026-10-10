"""Cách runner gọi dsh: thứ tự tham số và cách đọc config cục bộ.

dsh 0.2.0-rc.1 có hai tầng tham số: cờ của launcher (`--profile`, `--patch`) rồi tới tham số của app
headless (`--json`, task). Launcher dừng đọc cờ của mình ở token đầu tiên nó không nhận ra. Runner từng
đặt `--patch` sau `--json`: dsh thật trả `unknown option '--patch'`, thoát mã 1, và mọi lượt chạy thật
ra DSH_EXIT. `scripts/fake_dsh.py` nay kiểm thứ tự như launcher thật để chạy khô bắt được lỗi này.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness_agent.runner import dsh as dsh_module
from harness_agent.runner.dsh import LocalConfig, build_patch, dsh_arguments, run_dsh
from harness_agent.runner.pipeline import ErrorCode, run_case

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
FAKE_DSH = ROOT / "scripts" / "fake_dsh.py"

_spec = importlib.util.spec_from_file_location("fake_dsh", FAKE_DSH)
fake_dsh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_dsh)

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
CLEAN = {
    "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
    "tsa": "auto",
}


def old_order(patch: Path, extra_args: list[str], task: str | None) -> list[str]:
    """Thứ tự runner dùng trước khi sửa: `--json` đứng trước `--patch`."""
    return [
        "--profile",
        "headless",
        "--json",
        "--patch",
        str(patch),
        *extra_args,
        "-" if task is None else task,
    ]


def _cfg(tmp_path: Path, **overrides) -> LocalConfig:
    base = {
        "dsh_command": [sys.executable, str(FAKE_DSH)],
        "dsh_home": tmp_path / "dsh_home",
        "base_patch": ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        "python_exe": Path(sys.executable),
        "requires_api_key": False,
    }
    return LocalConfig(**{**base, **overrides})


def _scenario(tmp_path: Path, monkeypatch, scenario: dict) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))


# ------------------------------------------------------------ thứ tự tham số
def test_launcher_flags_come_before_app_arguments(tmp_path: Path):
    patch = tmp_path / "cba.patch.yml"
    assert dsh_arguments(patch, [], None) == ["--profile", "headless", "--patch", str(patch), "--json", "-"]

    args = dsh_arguments(patch, ["--session-id", "session-1"], "tạo VLAN 30")
    assert args == [
        "--profile",
        "headless",
        "--patch",
        str(patch),
        "--json",
        "--session-id",
        "session-1",
        "tạo VLAN 30",
    ]
    # `--json` là token đầu tiên launcher không nhận ra: mọi cờ của launcher phải đứng trước nó.
    assert args.index("--profile") < args.index("--patch") < args.index("--json")


@pytest.mark.parametrize(
    ("prompt_via", "last", "stdin"), [("stdin", "-", "việc cần làm"), ("arg", "việc cần làm", None)]
)
def test_run_dsh_uses_that_order(tmp_path: Path, monkeypatch, prompt_via: str, last: str, stdin: str | None):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["input"], seen["cwd"] = cmd, kwargs.get("input"), kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, b'{"type": "final", "text": ""}\n', b"")

    monkeypatch.setattr(dsh_module.subprocess, "run", fake_run)
    cfg = _cfg(
        tmp_path,
        dsh_command=["node", "bin.js"],
        prompt_via=prompt_via,
        extra_args=["--session-id", "s"],
        dsh_cwd=tmp_path / "cwd",
    )
    patch = tmp_path / "cba.patch.yml"

    run_dsh(cfg, patch, "việc cần làm", tmp_path / "cba")

    expected_tail = ["--profile", "headless", "--patch", str(patch), "--json", "--session-id", "s", last]
    assert seen["cmd"] == ["node", "bin.js", *expected_tail]
    assert seen["input"] == (stdin.encode("utf-8") if stdin else None)
    assert seen["cwd"] == tmp_path / "cwd"  # không phải gốc repo (D-023, D-025)


# ------------------------------------------------------------ fake_dsh mô phỏng launcher thật
def test_fake_launcher_stops_at_the_first_token_it_does_not_know():
    split = fake_dsh.split_launcher_args
    assert split(["--profile", "headless", "--patch", "p.yml", "--json", "-"]) == (
        "headless",
        ["p.yml"],
        ["--json", "-"],
    )
    # Thứ tự cũ: launcher dừng ở --json, nên --patch rơi xuống app và không patch nào được áp.
    assert split(["--profile", "headless", "--json", "--patch", "p.yml", "-"]) == (
        "headless",
        [],
        ["--json", "--patch", "p.yml", "-"],
    )
    # `dsh headless ...` là cách viết tắt của `dsh --profile headless ...`.
    assert split(["headless", "--patch", "a.yml", "--patch", "b.yml", "việc"]) == (
        "headless",
        ["a.yml", "b.yml"],
        ["việc"],
    )


def test_fake_headless_app_rejects_unknown_options():
    parse = fake_dsh.parse_headless_args
    assert parse(["--json", "-"]) == "-"
    assert parse(["--json", "--session-id", "s1", "chạy", "test"]) == "chạy test"
    assert parse(["--json"]) == "-"
    with pytest.raises(fake_dsh.UsageError, match=r"unknown option '--patch'"):
        parse(["--json", "--patch", "p.yml", "-"])


def _run_fake(tmp_path: Path, monkeypatch, order) -> subprocess.CompletedProcess:
    _scenario(tmp_path, monkeypatch, CLEAN)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    patch = build_patch(_cfg(tmp_path), "cba", CAMPUS, "network_admin", run_dir)
    return subprocess.run(  # noqa: S603
        [sys.executable, str(FAKE_DSH), *order(patch, [], None)],
        input=b"ping",
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.slow
def test_fake_dsh_rejects_the_old_order_like_real_dsh(tmp_path: Path, monkeypatch):
    """Đúng những gì dsh 0.2.0-rc.1 thật in ra với thứ tự cũ (đã thử không có API key, 01/10/2026)."""
    proc = _run_fake(tmp_path, monkeypatch, old_order)
    assert proc.returncode == 1
    assert [json.loads(x) for x in proc.stdout.decode("utf-8").splitlines()] == [
        {"type": "error", "message": "unknown option '--patch'"}
    ]
    assert not (tmp_path / "run" / "trace.jsonl").exists()  # harness không được khởi động


@pytest.mark.slow
def test_fake_dsh_accepts_the_new_order(tmp_path: Path, monkeypatch):
    proc = _run_fake(tmp_path, monkeypatch, dsh_arguments)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    types = [json.loads(x)["type"] for x in proc.stdout.decode("utf-8").splitlines()]
    assert types[0] == "session" and types[-1] == "final" and "tool_call" in types
    assert "server_start" in (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")


# ------------------------------------------------------------ qua pipeline
@pytest.mark.slow
def test_pipeline_accepts_a_clean_case_with_the_new_order(tmp_path: Path, monkeypatch):
    _scenario(tmp_path, monkeypatch, CLEAN)
    r = run_case(_cfg(tmp_path), CAMPUS, "intent thử", "network_admin", tmp_path / "runs", "case")
    assert (r.status, r.verdict, r.error_code) == ("ok", "accept", None), r.reason


@pytest.mark.slow
def test_negative_control_old_order_makes_every_case_dsh_exit(tmp_path: Path, monkeypatch):
    """Đối chứng âm: trả thứ tự cũ về thì cùng case sạch ở trên ra ERROR DSH_EXIT, không sinh change
    package. Đây là điều sẽ xảy ra với MỌI lượt chạy dsh thật nếu lỗi quay lại."""
    monkeypatch.setattr(dsh_module, "dsh_arguments", old_order)
    _scenario(tmp_path, monkeypatch, CLEAN)

    r = run_case(_cfg(tmp_path), CAMPUS, "intent thử", "network_admin", tmp_path / "runs", "case")

    assert (r.status, r.verdict, r.error_code) == ("error", None, ErrorCode.DSH_EXIT), r.reason
    run_dir = Path(r.run_dir)
    assert json.loads((run_dir / "cba.ndjson").read_text(encoding="utf-8")) == {
        "type": "error",
        "message": "unknown option '--patch'",
    }
    assert not (run_dir / "change_package.md").exists() and not (run_dir / "candidate").exists()
