"""`fake_dsh` ở chế độ B0 (D-044): patch không có khối MCP client và kịch bản có khoá "b0".

Chỉ kiểm bản giả lập; runner được kiểm ở `test_b0_pipeline.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_agent.runner import dsh_home
from harness_agent.runner.dsh import LocalConfig, build_b0_patch, build_patch, dsh_arguments

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "scripts" / "fake_dsh.py"
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
# Hash của bộ byte context mà runner ràng buộc cho lượt chạy (D-054); đi vào patch.
CAMPUS_SHA256 = hashlib.sha256(CAMPUS.read_bytes()).hexdigest()
ANSWER = "```cisco SW2\nvlan 30\nend\n```\n"

pytestmark = pytest.mark.slow


def _run(
    tmp_path: Path, scenario: dict, prompt: str = "PROMPT-b0-fake", with_mcp: bool = False
) -> tuple[list[dict], Path]:
    cfg = LocalConfig(
        dsh_command=[sys.executable, str(FAKE)],
        dsh_home=tmp_path / "home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )
    if with_mcp:
        patch = build_patch(cfg, "cba", CAMPUS, "network_admin", tmp_path, context_sha256=CAMPUS_SHA256)
    else:
        patch = build_b0_patch(cfg, tmp_path)
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    env = {**os.environ, "FAKE_DSH_SCENARIO": str(scenario_file), "DSH_HOME": str(cfg.dsh_home)}
    proc = subprocess.run(  # noqa: S603 — chạy script của repo bằng chính Python của test
        [sys.executable, str(FAKE), *dsh_arguments(patch, [], None)],
        input=prompt.encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    events = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    return events, cfg.dsh_home


def _session_text(home: Path) -> str:
    logs = list(home.rglob("*.jsonl.zstd"))
    assert len(logs) == 1
    return dsh_home.read_file(logs[0]).decode("utf-8")


def test_b0_mode_answers_with_the_scenario_text_and_offers_no_tool(tmp_path: Path):
    events, home = _run(tmp_path, {"b0": {"final": ANSWER}})
    finals = [e for e in events if e["type"] == "final"]
    assert [e["text"] for e in finals] == [ANSWER]
    assert not [e for e in events if e["type"] == "tool_call"]
    assert next(e for e in events if e["type"] == "session")["offered_tools"] == []
    assert dsh_home.offered_tools(home) == (1, [])
    # Như dsh thật, session log giữ nguyên prompt: ở B0 prompt đó chứa cả context.
    assert "PROMPT-b0-fake" in _session_text(home)
    assert any(e.get("usage") for e in events if e.get("phase") == "step_end")


def test_b0_scenario_can_offer_tools_and_emit_tool_calls(tmp_path: Path):
    scenario = {
        "b0": {
            "final": ANSWER,
            "offered_tools": ["bash"],
            "tool_calls": [{"tool": "bash", "args": {"command": "dir"}}],
        }
    }
    events, home = _run(tmp_path, scenario)
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == ["bash"]
    assert dsh_home.offered_tools(home) == (1, ["bash"])
    assert [e["text"] for e in events if e["type"] == "final"] == [ANSWER]


def test_negative_control_a_patch_with_the_mcp_client_never_enters_b0_mode(tmp_path: Path):
    """Đối chứng âm: cùng kịch bản có khoá "b0", nhưng patch CÓ khối MCP client (patch của B3) → fake_dsh
    đi đường cũ: khởi động harness server, không trả lời bằng chuỗi `final` của kịch bản."""
    events, home = _run(tmp_path, {"b0": {"final": ANSWER}, "cba": []}, with_mcp=True)
    assert [e["text"] for e in events if e["type"] == "final"] == ["fake_dsh: done"]
    assert dsh_home.offered_tools(home)[1] != []


def _run_b0_raw(tmp_path: Path, scenario: dict) -> subprocess.CompletedProcess:
    cfg = LocalConfig(
        dsh_command=[sys.executable, str(FAKE)],
        dsh_home=tmp_path / "home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    env = {**os.environ, "FAKE_DSH_SCENARIO": str(scenario_file), "DSH_HOME": str(cfg.dsh_home)}
    return subprocess.run(  # noqa: S603 — chạy script của repo bằng chính Python của test
        [sys.executable, str(FAKE), *dsh_arguments(build_b0_patch(cfg, tmp_path), [], None)],
        input=b"PROMPT",
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        {"b0": "```cisco SW2\nvlan 30\nend\n```\n"},  # dạng chuỗi của corpus v2 bản đầu
        {"b0": {"text": ANSWER}},
        {"b0": {"final": 30}},
        {"cba": []},  # không có khoá b0
    ],
)
def test_malformed_b0_scenario_is_an_error_not_fail_open(tmp_path: Path, scenario: dict):
    """Bản đầu rơi vào nhánh fail-open và tạo thư mục `session-fake-?`, Windows từ chối → DSH_EXIT cho cả cột
    B0 của corpus v2. Nay: một event `error`, mã thoát 2, không ghi gì vào DSH_HOME."""
    proc = _run_b0_raw(tmp_path, scenario)
    assert proc.returncode == 2
    events = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["error"] and "b0" in events[0]["message"]
    home = tmp_path / "home"
    assert not home.exists() or not [p for p in home.rglob("*") if "?" in p.name]
    assert not (home / "sessions").exists()


def test_negative_control_a_well_formed_b0_scenario_still_answers(tmp_path: Path):
    proc = _run_b0_raw(tmp_path, {"b0": {"final": ANSWER}})
    assert proc.returncode == 0
    events = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    assert [e["text"] for e in events if e["type"] == "final"] == [ANSWER]
