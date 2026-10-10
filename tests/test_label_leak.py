"""Kiểm tra rò nhãn (D-025): model không được nhìn thấy tên case hay nhãn đáp án.

Ba lớp:
1. Mẫu so khớp và từng nguồn (prompt, patch, session log), có đối chứng âm cho từng nguồn.
2. Cả 15 case chạy khô: không case nào để nhãn lọt vào những gì model nhìn thấy.
3. Đối chứng âm cho lớp 2: trả thư mục làm việc về vị trí của D-023 (`<out>/<run_id>/.dsh_cwd`) thì phép
   kiểm đỏ, tức nó bắt được đúng lỗi đã xảy ra.

Giới hạn (ghi ở `runner/label_leak.py`): session log của lượt chạy khô do `fake_dsh` viết, chỉ mô phỏng hai
câu có đường dẫn thư mục làm việc. System prompt đầy đủ của dsh thật chỉ kiểm được trên lượt chạy thật.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import zstandard

from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.label_leak import LABEL_PATTERNS, Report, check_run_dir
from harness_agent.runner.pipeline import run_case

ROOT = Path(__file__).resolve().parents[1]
CASES = sorted((ROOT / "data" / "cases").glob("*.json"))

_spec = importlib.util.spec_from_file_location("check_label_leak", ROOT / "scripts" / "check_label_leak.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

# Hai câu dsh 0.2.0-rc.1 thật gửi cho model khi thư mục làm việc nằm trong run-dir (lượt thử 01/10/2026).
REAL_SYSTEM = (
    "You are an AI agent powered by DeepSeek Harness. Your working directory is "
    "D:\\FPT\\KLTN\\Cisco-Harness-Agent\\Cisco-Harness-Agent\\runs\\adv-protected-uplink\\.dsh_cwd."
)
NEUTRAL_SYSTEM = (
    "You are an AI agent powered by DeepSeek Harness. Your working directory is "
    "C:\\Users\\someone\\AppData\\Local\\cha\\workspace."
)


def _matches(text: str) -> set[str]:
    return {name for name, pattern in LABEL_PATTERNS.items() if pattern.search(text)}


def _run_dir(tmp_path: Path, run_id: str = "adv-protected-uplink") -> Path:
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    return run_dir


def _session(run_dir: Path, events: list[dict], session_id: str = "session-0f3a") -> None:
    target = run_dir / ".dsh_home" / "sessions" / "--C-neutral--" / session_id
    target.mkdir(parents=True)
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
    (target / "session.v4.jsonl.zstd").write_bytes(zstandard.ZstdCompressor().compress(body))


def _message(kind: str, text: str) -> dict:
    return {"type": kind, "data": {"content": [{"type": "text", "text": text}]}}


def _labels(report: Report, leaks_only: bool = True) -> set[str]:
    return {h.label for h in (report.leaks if leaks_only else report.hits)}


# ------------------------------------------------------------ mẫu
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("runs\\adv-protected-uplink\\.dsh_cwd", {"adv-"}),
        ("data/cases/clean-vlan30.json", {"clean-"}),
        ("AMBIGUOUS-vlan40", {"ambiguous-"}),
        ("fault-harness-down", {"fault-"}),
        ('"expect": {"verdict": "reject"}', {"expect"}),
        ("stopped_by=G4_blast_radius", {"stopped_by"}),
        ("data/scenarios (fake_scenario)", {"fake_scenario"}),
        # Không phải nhãn: từ thường gặp trong prompt, tool result và cấu hình của dsh.
        ("Invalid arguments: expected an object.", set()),
        ("an unexpected error", set()),
        ("row agent-default-model", set()),
        ("Advanced settings, cleanly formatted", set()),
        ("Tạo VLAN 30 tên FINANCE trên SW2", set()),
    ],
)
def test_label_patterns(text: str, expected: set[str]):
    assert _matches(text) == expected


# ------------------------------------------------------------ từng nguồn
def test_prompt_is_read_in_full(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "cba.prompt.txt").write_text("Shut down R1 GigabitEthernet0/0\n", encoding="utf-8")
    assert check_run_dir(run_dir).leaks == []

    (run_dir / "tsa.prompt.txt").write_text("Case adv-protected-uplink, expect: reject\n", encoding="utf-8")
    report = check_run_dir(run_dir)
    assert report.prompts == 2
    assert _labels(report) == {"adv-", "expect", "run_id (adv-protected-uplink)"}
    assert {h.file for h in report.leaks} == {"tsa.prompt.txt"}


def test_case_file_name_is_a_label_too(tmp_path: Path):
    run_dir = _run_dir(tmp_path, run_id="r-0007")
    (run_dir / "cba.prompt.txt").write_text("xem data/cases/uplink_case_07.json\n", encoding="utf-8")
    assert check_run_dir(run_dir).leaks == []
    report = check_run_dir(run_dir, case_file=Path("data/cases/uplink_case_07.json"))
    assert _labels(report) == {"tên file case (uplink_case_07)"}


def _patch(run_dir: Path, **mcp_config) -> None:
    config = {
        "transport": "stdio",
        "serverName": "harness",
        "command": "python",
        # Tham số khởi động harness server: có đường dẫn run-dir (chứa run_id), không gửi cho model.
        "args": ["-m", "harness_agent.harness.server", "--run-dir", str(run_dir), "--role", "viewer"],
        "cwd": str(ROOT),
        **mcp_config,
    }
    rows = [
        {"id": "tool-bash", "disabled": True},
        {"id": "mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": config},
        {"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": "deepseek-flash"}},
    ]
    (run_dir / "cba.patch.yml").write_text(json.dumps(rows), encoding="utf-8")  # JSON là YAML hợp lệ


def test_patch_launch_arguments_are_not_model_visible(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    _patch(run_dir)
    report = check_run_dir(run_dir)
    assert report.patches == 1 and report.hits == []


def test_negative_control_label_in_a_model_visible_patch_field_is_found(tmp_path: Path):
    """Đối chứng cho test trên: cùng nhãn đó ở trường model nhìn thấy (tên server là tiền tố của tên tool)."""
    run_dir = _run_dir(tmp_path)
    _patch(run_dir, serverName="adv-protected-uplink")
    report = check_run_dir(run_dir)
    assert {(h.where, h.label) for h in report.leaks} == {
        ("patch:serverName", "adv-"),
        ("patch:serverName", "run_id (adv-protected-uplink)"),
    }


def test_session_log_with_the_real_leaking_sentence_is_a_leak(tmp_path: Path):
    """Đúng câu dsh thật đã gửi cho model khi thư mục làm việc là `runs/<run_id>/.dsh_cwd` (D-023)."""
    run_dir = _run_dir(tmp_path)
    _session(run_dir, [{"type": "session", "id": "s"}, _message("system/message", REAL_SYSTEM)])
    report = check_run_dir(run_dir)
    assert (report.session_logs, report.fake_session_logs, report.real_session_logs) == (1, 0, 1)
    assert {(h.where, h.label) for h in report.leaks} == {
        ("system/message", "adv-"),
        ("system/message", "run_id (adv-protected-uplink)"),
    }


def test_session_log_with_a_neutral_working_directory_is_clean(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    _session(run_dir, [_message("system/message", NEUTRAL_SYSTEM), _message("user/message", "Shut down R1")])
    assert check_run_dir(run_dir).hits == []


def test_what_the_model_itself_wrote_is_reported_but_is_not_a_leak(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    _session(
        run_dir,
        [
            _message("assistant/message", "I will clean-up the VLAN list"),
            {"type": "tool/call", "data": {"name": "x", "arguments": '{"description": "adv-test"}'}},
        ],
    )
    report = check_run_dir(run_dir)
    assert report.leaks == []
    assert _labels(report, leaks_only=False) == {"clean-", "adv-"}


def test_unknown_event_types_count_as_model_visible(tmp_path: Path):
    """Loại event chưa biết được coi là thứ model nhìn thấy: báo thừa còn hơn bỏ sót."""
    run_dir = _run_dir(tmp_path)
    _session(run_dir, [_message("some/new-event", "fault-harness-down")])
    assert _labels(check_run_dir(run_dir)) == {"fault-"}


def test_fake_session_logs_are_counted_separately(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    _session(run_dir, [_message("system/message", NEUTRAL_SYSTEM)], session_id="session-fake-cba")
    report = check_run_dir(run_dir)
    assert (report.session_logs, report.fake_session_logs, report.real_session_logs) == (1, 1, 0)


def test_unreadable_session_log_is_reported_not_treated_as_clean(tmp_path: Path):
    run_dir = _run_dir(tmp_path)
    target = run_dir / ".dsh_home" / "sessions" / "--x--" / "session-1"
    target.mkdir(parents=True)
    (target / "session.v4.jsonl.zstd").write_bytes(b"not zstd")
    report = check_run_dir(run_dir)
    assert report.unreadable == [".dsh_home/…/session-1/session.v4.jsonl.zstd"]


# ------------------------------------------------------------ script
def test_script_exit_codes_and_output(tmp_path: Path, capsys):
    leaking = _run_dir(tmp_path)
    (leaking / "cba.prompt.txt").write_text("ok\n", encoding="utf-8")
    _session(leaking, [_message("system/message", REAL_SYSTEM)])
    clean = _run_dir(tmp_path, run_id="r-0001")
    (clean / "cba.prompt.txt").write_text("Shut down R1\n", encoding="utf-8")
    empty = _run_dir(tmp_path, run_id="r-0002")

    assert script.main([str(leaking)]) == 1
    out = capsys.readouterr().out
    assert "RÒ NHÃN: adv- |" in out and "system/message" in out and "KẾT QUẢ: CÓ RÒ NHÃN" in out

    assert script.main([str(clean)]) == 0
    out = capsys.readouterr().out
    assert "KẾT QUẢ: không thấy nhãn" in out
    # Không có session log: nói rõ phần nào chưa kiểm được, không im lặng coi là sạch.
    assert "CHƯA KIỂM ĐƯỢC" in out and "--keep-dsh-home" in out

    assert script.main([str(empty)]) == 2
    capsys.readouterr()
    assert script.main([str(tmp_path / "r-*")]) == 0  # tự khai triển wildcard (PowerShell không làm)
    assert "2 run-dir đã kiểm" not in capsys.readouterr().out  # r-0002 không có gì để kiểm


# ------------------------------------------------------------ lượt chạy khô
def _fake_cfg(tmp_path: Path, **overrides) -> LocalConfig:
    base = {
        "dsh_command": [sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        "dsh_home": tmp_path / "unused_home",
        "base_patch": ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        "python_exe": Path(sys.executable),
        "requires_api_key": False,
        "keep_dsh_home": True,  # giữ session log để phép kiểm đọc được
    }
    return LocalConfig(**{**base, **overrides})


def _dry_run(case_file: Path, out_root: Path, monkeypatch, **cfg) -> Path:
    case = json.loads(case_file.read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(ROOT / case["fake_scenario"]))
    result = run_case(
        _fake_cfg(out_root, **cfg),
        ROOT / case["context"],
        case["intent"],
        case["role"],
        out_root,
        case_file.stem,
    )
    return Path(result.run_dir)


@pytest.mark.slow
@pytest.mark.parametrize("case_file", CASES, ids=lambda p: p.stem)
def test_no_dry_run_case_shows_its_label_to_the_model(case_file: Path, tmp_path: Path, monkeypatch):
    run_dir = _dry_run(case_file, tmp_path / "runs", monkeypatch)
    report = check_run_dir(run_dir, case_file)

    assert report.prompts >= 1 and report.patches >= 1 and report.session_logs >= 1
    assert report.unreadable == []
    assert report.leaks == [], [(h.file, h.where, h.label, h.snippet) for h in report.leaks]


@pytest.mark.slow
def test_negative_control_per_case_working_directory_leaks_the_label(tmp_path: Path, monkeypatch):
    """Đối chứng âm: thư mục làm việc ở vị trí của D-023 (`<out>/<run_id>/.dsh_cwd`). Phép kiểm phải thấy
    tên case trong đúng hai message mà dsh tự chèn, như ở lượt chạy thật."""
    case_file = ROOT / "data" / "cases" / "adv-protected-uplink.json"
    old_location = tmp_path / "old" / case_file.stem / ".dsh_cwd"
    run_dir = _dry_run(case_file, tmp_path / "runs", monkeypatch, dsh_workspace=old_location)

    leaks = check_run_dir(run_dir, case_file).leaks
    assert {h.where for h in leaks} >= {"system/message", "user/message"}
    assert {"adv-", "run_id (adv-protected-uplink)"} <= {h.label for h in leaks}
    assert all(h.file.endswith(".jsonl.zstd") for h in leaks), "prompt và patch của runner vẫn sạch"


@pytest.mark.slow
def test_fake_session_log_carries_the_working_directory_like_real_dsh(tmp_path: Path, monkeypatch):
    """`fake_dsh` phải đặt đường dẫn thư mục làm việc vào đúng các message mà dsh thật đặt; không có điều
    này thì test chạy khô ở trên xanh mà không kiểm gì."""
    from harness_agent.runner import dsh_home

    def flat(event: dict) -> str:
        """Event dạng một chuỗi, bỏ mọi lớp escape của dấu gạch chéo ngược (đường dẫn Windows nằm trong
        JSON lồng JSON)."""
        text = json.dumps(event, ensure_ascii=False)
        while "\\\\" in text:
            text = text.replace("\\\\", "\\")
        return os.path.normcase(text)

    workdir = tmp_path / "cha" / "workspace"
    case_file = ROOT / "data" / "cases" / "clean-vlan30.json"
    run_dir = _dry_run(case_file, tmp_path / "runs", monkeypatch, dsh_workspace=workdir)
    logs = [p for p, rel in dsh_home._files(run_dir / ".dsh_home") if rel.endswith(".jsonl.zstd")]
    assert len(logs) == 2  # cba, tsa
    for log in logs:
        events = [json.loads(x) for x in dsh_home.read_file(log).decode("utf-8").splitlines()]
        carrying = {e["type"] for e in events if os.path.normcase(str(workdir)) in flat(e)}
        assert {"session", "system/message", "user/message"} <= carrying
