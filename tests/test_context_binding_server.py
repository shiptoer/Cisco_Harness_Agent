"""Harness server chỉ nạp ĐÚNG bộ byte context mà runner đã ràng buộc cho lượt chạy (D-054).

Runner truyền `--context-sha256` qua patch do runner sinh (cùng kênh với role, model không đọc hay sửa được).
File đọc được khác hash đó, hoặc không đọc được, thì server không dựng gì từ file, ghi một dòng
`context_changed` vào trace (để runner phân loại `CTX_CHANGED_DURING_RUN`) và thoát mã 3. Kiểm ở cả
`--selftest` (preflight) lẫn server thật, ở mọi mức B1–B4.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from harness_agent.harness import server as harness_server
from harness_agent.harness.trace import verify_chain

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"


def _argv(context: Path, sha: str, run_dir: Path, stage: str = "cba", level: str = "B3") -> list[str]:
    return [
        "--stage",
        stage,
        "--context",
        str(context),
        "--context-sha256",
        sha,
        "--role",
        "network_admin",
        "--run-dir",
        str(run_dir),
        "--level",
        level,
        "--selftest",
    ]


def _records(run_dir: Path) -> list[dict]:
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return []
    return [json.loads(line) for line in trace.read_bytes().splitlines() if line.strip()]


@pytest.fixture
def context(tmp_path: Path) -> Path:
    path = tmp_path / "ctx" / "campus.json"
    path.parent.mkdir()
    shutil.copyfile(CAMPUS, path)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_matching_hash_starts_and_writes_nothing(tmp_path: Path, context: Path, stage: str, level: str):
    run_dir = tmp_path / "run"
    assert harness_server.main(_argv(context, _sha(context), run_dir, stage, level)) == 0
    assert _records(run_dir) == []


@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_changed_file_is_refused_at_every_level(
    tmp_path: Path, context: Path, stage: str, level: str, capsys
):
    bound = _sha(context)
    # Đổi file sau khi runner đã ràng buộc hash.
    context.write_bytes(context.read_bytes().replace(b'"SW2"', b'"SW9"', 1))
    run_dir = tmp_path / "run"

    assert (
        harness_server.main(_argv(context, bound, run_dir, stage, level))
        == harness_server.CONTEXT_CHANGED_EXIT
    )
    assert "sha256 lệch" in capsys.readouterr().err
    [record] = _records(run_dir)
    assert (record["actor"], record["event"]) == (f"harness:{stage}", "context_changed")
    assert record["data"] == {"expected_sha256": bound, "actual_sha256": _sha(context)}
    assert verify_chain(run_dir / "trace.jsonl") == "unsigned"


def test_negative_control_without_the_hash_check_the_changed_file_is_served(
    tmp_path: Path, context: Path, monkeypatch
):
    """Đối chứng âm: gỡ phép so hash (server đọc file như trước D-054) thì cùng file đã đổi được nạp và
    preflight báo thành công, tức test trên bắt được việc gỡ control."""
    bound = _sha(context)
    context.write_bytes(context.read_bytes().replace(b'"SW2"', b'"SW9"', 1))
    monkeypatch.setattr(
        harness_server, "read_bound_context", lambda path, _expected: (Path(path).read_bytes(), "")
    )

    assert harness_server.main(_argv(context, bound, tmp_path / "run")) == 0


def test_missing_file_is_a_changed_context(tmp_path: Path, context: Path):
    bound = _sha(context)
    context.unlink()
    run_dir = tmp_path / "run"

    assert harness_server.main(_argv(context, bound, run_dir)) == harness_server.CONTEXT_CHANGED_EXIT
    [record] = _records(run_dir)
    assert record["data"]["actual_sha256"] == "unreadable"


def test_corrupt_trace_does_not_let_the_server_start(tmp_path: Path, context: Path):
    """Trace hỏng thì không ghi được `context_changed`; server vẫn từ chối khởi động (runner ra
    HARNESS_STATE_INVALID)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "trace.jsonl").write_bytes(b"not json\n")

    assert harness_server.main(_argv(context, "0" * 64, run_dir)) == harness_server.CONTEXT_CHANGED_EXIT
    assert (run_dir / "trace.jsonl").read_bytes() == b"not json\n"


@pytest.mark.parametrize("sha", ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64])
def test_malformed_hash_argument_is_a_usage_error(tmp_path: Path, context: Path, sha: str):
    with pytest.raises(SystemExit) as exc:
        harness_server.main(_argv(context, sha, tmp_path / "run"))
    assert exc.value.code == 2


def test_hash_argument_is_required(tmp_path: Path, context: Path):
    argv = _argv(context, _sha(context), tmp_path / "run")
    i = argv.index("--context-sha256")
    with pytest.raises(SystemExit):
        harness_server.main(argv[:i] + argv[i + 2 :])
