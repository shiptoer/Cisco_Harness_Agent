"""Freshness binding ở runner (D-047 (a), D-054): các hàm thuần, không sinh process.

- `bind_context`: đọc file MỘT lần; hash lệch hash ghim → `CTX_HASH_MISMATCH` (lỗi dữ liệu case); context
  collector thiếu `evaluated_at` → `CTX_EVALUATED_AT_MISSING`.
- `freshness`: so `collected_at` với `evaluated_at` của case, KHÔNG đọc đồng hồ máy.
- `_binding_problem`: phép so ngay trước render (file + mọi `server_start`).
Lượt chạy thật qua `run_case` nằm ở `tests/test_freshness_pipeline.py`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness_agent.harness.trace import Trace
from harness_agent.models import Provenance
from harness_agent.runner import context_binding
from harness_agent.runner.context_binding import (
    STALE_EXPIRED,
    STALE_FUTURE,
    ContextBindingError,
    bind_context,
    freshness,
    package_lines,
    snapshot,
)
from harness_agent.runner.context_check import context_violations_from_bytes
from harness_agent.runner.pipeline import ErrorCode, _binding_problem

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
COLLECTED_AT = datetime(2026, 10, 13, 8, 39, 15, tzinfo=UTC)
TTL = 86400
COLLECTOR = {"source": "collector", "collected_at": "2026-10-13T08:39:15Z", "ttl_seconds": TTL}


def _context(tmp_path: Path, provenance: dict | None) -> Path:
    data = json.loads(CAMPUS.read_text(encoding="utf-8"))
    if provenance is not None:
        data = {
            "schema_version": data["schema_version"],
            "context_id": data["context_id"],
            "provenance": provenance,
        }
        data |= {k: v for k, v in json.loads(CAMPUS.read_text(encoding="utf-8")).items() if k not in data}
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ bind_context
def test_bind_reads_the_file_once_and_keeps_its_bytes(tmp_path: Path):
    path = tmp_path / "c.json"
    shutil.copyfile(CAMPUS, path)
    bound = bind_context(path, _sha(path))
    path.write_bytes(b"{}")  # đổi file SAU khi ràng buộc: bộ byte đã đọc không đổi theo
    assert bound.raw == CAMPUS.read_bytes()
    assert bound.sha256 == _sha(CAMPUS)
    assert (bound.context_id, bound.provenance, bound.provenance_invalid) == ("campus-small-01", None, False)


def test_pinned_hash_mismatch_is_a_case_data_error(tmp_path: Path):
    path = tmp_path / "c.json"
    shutil.copyfile(CAMPUS, path)
    with pytest.raises(ContextBindingError) as exc:
        bind_context(path, "0" * 64)
    assert exc.value.code == "CTX_HASH_MISMATCH"
    assert _sha(path) in str(exc.value)


def test_unreadable_file_with_a_pinned_hash_is_a_mismatch(tmp_path: Path):
    with pytest.raises(ContextBindingError) as exc:
        bind_context(tmp_path / "nowhere.json", _sha(CAMPUS))
    assert exc.value.code == "CTX_HASH_MISMATCH"


def test_unreadable_file_without_a_pin_is_left_to_the_level(tmp_path: Path):
    """Không ghim hash: giữ hành vi trước D-054 (B0: PREFLIGHT_FAILED; B1–B4: reject CTX_UNREADABLE)."""
    bound = bind_context(tmp_path / "nowhere.json")
    assert (bound.raw, bound.sha256) == (None, None)


def test_collector_context_needs_evaluated_at(tmp_path: Path):
    path = _context(tmp_path, COLLECTOR)
    with pytest.raises(ContextBindingError) as exc:
        bind_context(path)
    assert exc.value.code == "CTX_EVALUATED_AT_MISSING"
    bound = bind_context(path, evaluated_at=COLLECTED_AT)
    assert bound.provenance == Provenance.model_validate(COLLECTOR)


def test_declared_collector_with_invalid_provenance_still_needs_evaluated_at(tmp_path: Path):
    path = _context(tmp_path, {"source": "collector", "collected_at": "2026-10-13T08:39:15Z"})
    with pytest.raises(ContextBindingError):
        bind_context(path)
    bound = bind_context(path, evaluated_at=COLLECTED_AT)
    assert (bound.provenance, bound.provenance_invalid) == (None, True)


def test_synthetic_context_does_not_need_evaluated_at(tmp_path: Path):
    assert bind_context(_context(tmp_path, {"source": "synthetic"})).provenance is not None
    assert bind_context(CAMPUS).provenance is None


def test_naive_evaluated_at_is_refused():
    with pytest.raises(ValueError, match="múi giờ"):
        bind_context(CAMPUS, evaluated_at=datetime(2026, 10, 13, 12))


# ------------------------------------------------------------------ freshness
P = Provenance.model_validate(COLLECTOR)


@pytest.mark.parametrize(
    ("offset", "status"),
    [
        (timedelta(0), "fresh"),
        (timedelta(seconds=TTL), "fresh"),  # đúng bằng TTL vẫn còn hạn
        (timedelta(seconds=TTL, microseconds=1), "stale"),  # không làm tròn
        (timedelta(days=30), "stale"),
        (timedelta(seconds=-1), "stale"),  # collected_at nằm sau evaluated_at: không tin được
    ],
)
def test_freshness_boundaries(offset: timedelta, status: str):
    got, violations = freshness(P, COLLECTED_AT + offset)
    assert got == status
    assert [(v.code, v.path) for v in violations] == (
        [] if status == "fresh" else [("CTX_STALE", "provenance.collected_at")]
    )


@pytest.mark.parametrize(
    ("offset", "reason", "other"),
    [
        (timedelta(seconds=TTL + 1), STALE_EXPIRED, STALE_FUTURE),
        (timedelta(days=30), STALE_EXPIRED, STALE_FUTURE),
        (timedelta(seconds=-1), STALE_FUTURE, STALE_EXPIRED),
        (timedelta(microseconds=-1), STALE_FUTURE, STALE_EXPIRED),  # lệch dưới 1 giây vẫn là "tương lai"
    ],
)
def test_stale_detail_says_which_of_the_two_reasons(offset: timedelta, reason: str, other: str):
    """Quá hạn TTL (dữ liệu cũ: thu thập lại) và collected_at ở tương lai (mốc sai: sửa mốc) có cách sửa khác
    nhau, nên `detail` nói rõ là lý do nào."""
    [violation] = freshness(P, COLLECTED_AT + offset)[1]
    assert violation.detail.startswith(reason + ": ")
    assert other not in violation.detail


def test_freshness_is_not_applicable_without_a_collection_time():
    assert freshness(None, None) == ("no_ttl", [])
    assert freshness(Provenance(source="synthetic"), COLLECTED_AT) == ("no_ttl", [])


def test_collector_freshness_without_evaluated_at_is_a_programming_error():
    with pytest.raises(ValueError):
        freshness(P, None)


def test_freshness_code_never_reads_the_machine_clock():
    """Phép kiểm tĩnh, bổ sung cho test "hai ngày máy" ở test_freshness_pipeline.py."""
    source = inspect.getsource(context_binding)
    assert ".now(" not in source and "time.time" not in source and ".today(" not in source


def test_stale_violation_from_bytes_and_its_negative_control(tmp_path: Path):
    raw = _context(tmp_path, COLLECTOR).read_bytes()
    late = COLLECTED_AT + timedelta(days=2)
    found = context_violations_from_bytes(raw, check_freshness=True, freshness_at=late)
    assert [v.code for v in found] == ["CTX_STALE"]
    # Đối chứng âm: tắt freshness (mức B0/B1) thì cùng bộ byte không có vi phạm nào.
    assert context_violations_from_bytes(raw, check_freshness=False, freshness_at=late) == []


# ------------------------------------------------------------------ snapshot
def test_snapshot_has_no_path_and_states_what_was_checked(tmp_path: Path):
    path = _context(tmp_path, COLLECTOR)
    bound = bind_context(path, evaluated_at=COLLECTED_AT + timedelta(hours=3))
    snap = snapshot(bound, COLLECTED_AT + timedelta(hours=3), checked=True)
    assert snap == {
        "context_id": "campus-small-01",
        "sha256": _sha(path),
        "provenance": "declared",
        "source": "collector",
        "collected_at": "2026-10-13T08:39:15Z",
        "ttl_seconds": TTL,
        "evaluated_at": "2026-10-13T11:39:15Z",
        "freshness": "fresh",
    }
    assert str(tmp_path) not in json.dumps(snap)
    assert package_lines(snap, "B3")[-1] == "- Freshness: còn hạn tại evaluated_at 2026-10-13T11:39:15Z"
    unchecked = snapshot(bound, COLLECTED_AT, checked=False)
    assert package_lines(unchecked, "B1")[-1] == "- Freshness: KHÔNG kiểm ở mức B1 (freshness có từ B2)"


def test_synthetic_snapshot_says_ttl_is_not_checked():
    snap = snapshot(bind_context(CAMPUS), None, checked=True)
    assert (snap["source"], snap["provenance"], snap["freshness"]) == ("synthetic", None, "no_ttl")
    assert package_lines(snap, "B3")[1:] == [
        "- Nguồn context: synthetic (không có provenance)",
        "- Freshness: không kiểm TTL",
    ]


# ------------------------------------------------------------------ _binding_problem
ROLE, LEVEL = "network_admin", "B3"


def _run_dir(tmp_path: Path, starts: dict[str, list[str]], **override: str) -> Path:
    run_dir = tmp_path / "run"
    Trace(run_dir / "trace.jsonl", actor="runner").write("case_start")
    for stage, hashes in starts.items():
        for h in hashes:
            data = {"context_sha256": h, "role": ROLE, "level": LEVEL}
            Trace(run_dir / "trace.jsonl", actor=f"harness:{stage}").write("server_start", **data)
    for stage, values in override.items():  # vd tsa="role=security_admin": thêm một server_start lệch
        key, value = values.split("=")
        data = {"context_sha256": _sha(CAMPUS), "role": ROLE, "level": LEVEL, key: value}
        Trace(run_dir / "trace.jsonl", actor=f"harness:{stage}").write("server_start", **data)
    return run_dir


def _binding(path: Path, sha: str | None, run_dir: Path, stages: tuple[str, ...]):
    return _binding_problem(path, sha, run_dir, stages, role=ROLE, level=LEVEL)


def test_binding_holds_when_file_and_every_server_start_match(tmp_path: Path):
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha, sha], "tsa": [sha, sha]})
    assert _binding(CAMPUS, sha, run_dir, ("cba", "tsa")) is None


def test_a_later_server_start_with_another_hash_is_caught(tmp_path: Path):
    """dsh khởi động server hai lần mỗi stage (thăm dò + phục vụ). Lần thăm dò khớp mà lần phục vụ nạp file
    khác thì gate đã chạy trên context khác: phải bắt được, không chỉ nhìn lần đầu."""
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha, sha], "tsa": [sha, "f" * 64]})
    code, reason = _binding(CAMPUS, sha, run_dir, ("cba", "tsa"))
    assert code is ErrorCode.CTX_CHANGED_DURING_RUN and "tsa" in reason


def test_missing_server_start_cannot_be_verified(tmp_path: Path):
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha]})
    code, _ = _binding(CAMPUS, sha, run_dir, ("cba", "tsa"))
    assert code is ErrorCode.CTX_CHANGED_DURING_RUN


def test_server_refusal_in_the_trace_is_caught(tmp_path: Path):
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha]})
    Trace(run_dir / "trace.jsonl", actor="harness:tsa").write(
        "context_changed", expected_sha256=sha, actual_sha256="e" * 64
    )
    code, reason = _binding(CAMPUS, sha, run_dir, ())
    assert code is ErrorCode.CTX_CHANGED_DURING_RUN and "harness:tsa" in reason


def test_changed_file_is_caught_before_the_trace_is_read(tmp_path: Path):
    path = tmp_path / "c.json"
    shutil.copyfile(CAMPUS, path)
    sha = _sha(path)
    run_dir = _run_dir(tmp_path, {"cba": [sha], "tsa": [sha]})
    assert _binding(path, sha, run_dir, ("cba", "tsa")) is None
    path.write_bytes(path.read_bytes() + b" ")
    code, reason = _binding(path, sha, run_dir, ("cba", "tsa"))
    assert code is ErrorCode.CTX_CHANGED_DURING_RUN and "đã đổi" in reason


def test_unreadable_trace_is_harness_state_invalid(tmp_path: Path):
    sha = _sha(CAMPUS)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "trace.jsonl").write_bytes(b"not json\n")
    code, _ = _binding(CAMPUS, sha, run_dir, ("cba",))
    assert code is ErrorCode.HARNESS_STATE_INVALID


def test_server_start_with_another_role_is_caught(tmp_path: Path):
    """D-060 (F1): dsh khởi động harness tsa với role khác role của lượt chạy."""
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha], "tsa": [sha]}, tsa="role=security_admin")
    code, reason = _binding(CAMPUS, sha, run_dir, ("cba", "tsa"))
    assert code is ErrorCode.HARNESS_STATE_INVALID and "role" in reason and "security_admin" in reason
    # Đối chứng: không có dòng lệch thì binding giữ.
    assert (
        _binding(CAMPUS, sha, _run_dir(tmp_path / "ok", {"cba": [sha], "tsa": [sha]}), ("cba", "tsa")) is None
    )


def test_server_start_with_another_level_is_caught(tmp_path: Path):
    sha = _sha(CAMPUS)
    run_dir = _run_dir(tmp_path, {"cba": [sha], "tsa": [sha]}, cba="level=B1")
    code, reason = _binding(CAMPUS, sha, run_dir, ("cba", "tsa"))
    assert code is ErrorCode.HARNESS_STATE_INVALID and "level" in reason


def test_server_start_without_role_is_caught(tmp_path: Path):
    """Trace trước D-060 (hoặc dòng giả) không có role: không xác minh được, fail-closed."""
    sha = _sha(CAMPUS)
    run_dir = tmp_path / "run"
    Trace(run_dir / "trace.jsonl", actor="runner").write("case_start")
    Trace(run_dir / "trace.jsonl", actor="harness:cba").write("server_start", context_sha256=sha)
    code, _ = _binding(CAMPUS, sha, run_dir, ("cba",))
    assert code is ErrorCode.HARNESS_STATE_INVALID
