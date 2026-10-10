"""Khởi động harness server không có tác dụng phụ ngoài việc ghi thêm vào trace.

dsh 0.2.0-rc.1 khởi động MCP server stdio HAI lần mỗi phiên: một process thăm dò bị dừng gần như ngay,
rồi tới process phục vụ (README của `@deepseek-ai/dsh-mcp-client`: "stdio negotiation starts a temporary
probe process before the serving process"; thấy ở lượt chạy thật đầu tiên, 01/10/2026: mỗi stage có 2
`server_start` trong trace). Runner cũng khởi động server thêm một lần ở bước preflight (`--selftest`).

Vì vậy việc khởi động phải lặp lại được: mỗi lần chỉ được NỐI THÊM bản ghi `server_start` vào trace,
không được tạo, xoá hay ghi đè file nào khác trong run-dir, dù process bị dừng ở thời điểm nào.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness_agent.harness.server import STAGE_TOOLS
from harness_agent.harness.trace import Trace, verify_chain

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}


def _args(stage: str, run_dir: Path, *extra: str) -> list[str]:
    return [
        "-m",
        "harness_agent.harness.server",
        "--stage",
        stage,
        "--context",
        str(CAMPUS),
        "--role",
        "network_admin",
        "--run-dir",
        str(run_dir),
        *extra,
    ]


def _run_dir(tmp_path: Path, stage: str) -> Path:
    """Run-dir như lúc dsh khởi động harness của `stage`: đã có trace của runner và file của bước trước."""
    run_dir = tmp_path / "case"
    run_dir.mkdir()
    Trace(run_dir / "trace.jsonl", actor="runner").write("case_start", intent="x")
    (run_dir / f"{stage}.prompt.txt").write_text("prompt đã gửi\n", encoding="utf-8")
    (run_dir / f"{stage}.patch.yml").write_text("- id: tool-bash\n  disabled: true\n", encoding="utf-8")
    (run_dir / ".dsh_home" / "profiles" / "headless").mkdir(parents=True)
    (run_dir / ".dsh_home" / "profiles" / "headless" / "package.json").write_text("{}", encoding="utf-8")
    if stage == "tsa":
        request = {"status": "ready", "changes": [VLAN]}
        (run_dir / "change_request.json").write_text(json.dumps(request), encoding="utf-8")
        (run_dir / "cba.ndjson").write_bytes(b'{"type":"final","text":"x"}\n')
    return run_dir


def _snapshot(run_dir: Path) -> dict[str, str]:
    """Mọi file và thư mục dưới run-dir; file kèm sha256 của nội dung."""
    state: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        rel = path.relative_to(run_dir).as_posix()
        state[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return state


def _events(raw: bytes) -> list[tuple[str, str]]:
    return [(r["actor"], r["event"]) for r in map(json.loads, raw.splitlines()) if r]


def _wait_for_starts(trace: Path, count: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if trace.read_bytes().count(b'"server_start"') >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"harness không ghi đủ {count} server_start trong {timeout}s")


def _start(stage: str, run_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, *_args(stage, run_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    )


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.communicate(timeout=30)


def _assert_only_trace_grew(run_dir: Path, before: dict[str, str], old_trace: bytes, stage: str, starts: int):
    after = _snapshot(run_dir)
    assert set(after) == set(before), "khởi động harness không được tạo hay xoá file, thư mục nào"
    changed = [rel for rel in before if after[rel] != before[rel]]
    assert changed == (["trace.jsonl"] if starts else []), changed
    new_trace = (run_dir / "trace.jsonl").read_bytes()
    assert new_trace.startswith(old_trace), "trace chỉ được nối thêm, không được ghi đè"
    assert _events(new_trace[len(old_trace) :]) == [(f"harness:{stage}", "server_start")] * starts
    assert verify_chain(run_dir / "trace.jsonl")


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_killed_probe_then_serving_process_only_append_to_the_trace(tmp_path: Path, stage: str):
    """Đúng chuỗi dsh thật tạo ra: process thăm dò bị dừng ngay sau khi khởi động xong, rồi process phục
    vụ khởi động, bắt tay MCP, liệt kê tool, và đóng."""
    run_dir = _run_dir(tmp_path, stage)
    trace = run_dir / "trace.jsonl"
    before, old_trace = _snapshot(run_dir), trace.read_bytes()

    probe = _start(stage, run_dir)
    try:
        _wait_for_starts(trace, 1)
    finally:
        _kill(probe)
    _assert_only_trace_grew(run_dir, before, old_trace, stage, starts=1)

    async def serve_once() -> list[str]:
        params = StdioServerParameters(command=sys.executable, args=_args(stage, run_dir), cwd=str(ROOT))
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            return sorted(tool.name for tool in (await session.list_tools()).tools)

    assert asyncio.run(serve_once()) == sorted(STAGE_TOOLS[stage])
    _assert_only_trace_grew(run_dir, before, old_trace, stage, starts=2)


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_process_killed_before_it_finished_starting_leaves_the_run_dir_untouched(tmp_path: Path, stage: str):
    """Dừng process ngay khi vừa tạo, trước khi nó nạp xong: không file nào đổi, trace không có dòng dở."""
    run_dir = _run_dir(tmp_path, stage)
    trace = run_dir / "trace.jsonl"
    before, old_trace = _snapshot(run_dir), trace.read_bytes()

    for _ in range(3):
        _kill(_start(stage, run_dir))

    after = _snapshot(run_dir)
    assert set(after) == set(before)
    assert [rel for rel in before if after[rel] != before[rel]] in ([], ["trace.jsonl"])
    new_trace = trace.read_bytes()
    assert new_trace.startswith(old_trace)
    appended = new_trace[len(old_trace) :]
    # Nếu có process kịp ghi thì đó phải là các dòng `server_start` trọn vẹn, và chuỗi hash vẫn liền.
    assert appended == b"" or appended.endswith(b"\n")
    assert set(_events(appended)) <= {(f"harness:{stage}", "server_start")}
    assert verify_chain(trace)


def _create(run_dir: Path) -> None:
    (run_dir / "change_request.json").write_text("{}", encoding="utf-8")


def _create_in_dsh_home(run_dir: Path) -> None:
    (run_dir / ".dsh_home" / "note.txt").write_text("x", encoding="utf-8")


def _delete(run_dir: Path) -> None:
    (run_dir / "cba.prompt.txt").unlink()


def _overwrite(run_dir: Path) -> None:
    (run_dir / "cba.patch.yml").write_text("- id: tool-bash\n  disabled: false\n", encoding="utf-8")


def _rewrite_trace(run_dir: Path) -> None:
    """Ghi lại trace từ đầu thay vì nối thêm: mất bản ghi của runner."""
    trace = run_dir / "trace.jsonl"
    trace.unlink()
    Trace(trace, actor="harness:cba").write("server_start")


@pytest.mark.parametrize("damage", [_create, _create_in_dsh_home, _delete, _overwrite, _rewrite_trace])
def test_negative_control_the_check_catches_every_kind_of_side_effect(tmp_path: Path, damage):
    """Đối chứng âm: một lần khởi động có tác dụng phụ (tạo, xoá, ghi đè file, hoặc ghi lại trace) làm phép
    kiểm của các test trên ĐỎ. Thiếu đối chứng này thì "xanh" có thể chỉ vì phép kiểm không nhìn thấy gì."""
    run_dir = _run_dir(tmp_path, "cba")
    trace = run_dir / "trace.jsonl"
    before, old_trace = _snapshot(run_dir), trace.read_bytes()
    if damage is not _rewrite_trace:
        Trace(trace, actor="harness:cba").write("server_start")  # phần hợp lệ của một lần khởi động
    _assert_only_trace_grew(run_dir, before, old_trace, "cba", starts=0 if damage is _rewrite_trace else 1)

    damage(run_dir)

    with pytest.raises(AssertionError):
        _assert_only_trace_grew(run_dir, before, old_trace, "cba", starts=1)


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_selftest_changes_nothing_at_all(tmp_path: Path, stage: str):
    """Preflight của runner (`--selftest`) chỉ in danh sách tool: không ghi cả vào trace."""
    run_dir = _run_dir(tmp_path, stage)
    before = _snapshot(run_dir)

    proc = subprocess.run(  # noqa: S603
        [sys.executable, *_args(stage, run_dir, "--selftest")],
        capture_output=True,
        cwd=ROOT,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads(proc.stdout)["tools"] == sorted(STAGE_TOOLS[stage])
    assert _snapshot(run_dir) == before


@pytest.mark.slow
def test_tsa_server_without_an_approved_change_request_writes_nothing(tmp_path: Path):
    """Stage tsa chưa có change request đã duyệt: server từ chối khởi động (fail-closed), không để lại gì."""
    run_dir = _run_dir(tmp_path, "tsa")
    (run_dir / "change_request.json").unlink()
    before = _snapshot(run_dir)

    proc = subprocess.run(  # noqa: S603
        [sys.executable, *_args("tsa", run_dir)], capture_output=True, cwd=ROOT, timeout=60, check=False
    )

    assert proc.returncode == 2
    assert _snapshot(run_dir) == before
