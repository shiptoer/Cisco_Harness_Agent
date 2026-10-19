"""Freshness binding qua `run_case` thật (fake_dsh + harness server thật), D-047 (a), D-054.

- `CTX_STALE`: reject của control (i), chỉ từ B2; B0/B1 không kiểm.
- `CTX_HASH_MISMATCH`: lỗi dữ liệu case, raise trước khi đụng tới run-dir; `run_cases` dừng cả lô.
- `CTX_CHANGED_DURING_RUN`: ERROR ở MỌI mức khi file đổi trong lúc chạy; không để lại change package.
- "Hai ngày máy": cùng case, đồng hồ máy khác nhau, kết quả giống hệt.
Mỗi control có một đối chứng âm: gỡ control thì test đỏ.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness_agent.eval import evaluate_case
from harness_agent.harness import trace as trace_module
from harness_agent.models import TestCase
from harness_agent.runner import context_binding, context_check, pipeline
from harness_agent.runner.cases import CaseError
from harness_agent.runner.context_binding import ContextBindingError
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import CaseResult, ErrorCode, run_case

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
COLLECTOR = {"source": "collector", "collected_at": "2026-10-13T08:39:15Z", "ttl_seconds": 86400}
FRESH_AT = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
STALE_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
CLEAN = {
    "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
    "tsa": "auto",
}
B0_CONFIG = {"b0": {"final": "```cisco SW2\nvlan 30\n name FINANCE\nexit\nend\n```\n"}}
LEVELS = ("B0", "B1", "B2", "B3", "B4")


def _cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def _context(tmp_path: Path, provenance: dict | None = None) -> Path:
    """Bản sao của campus-small-01 trong tmp_path, có thêm `provenance` ngay sau `context_id`."""
    data = json.loads(CAMPUS.read_text(encoding="utf-8"))
    if provenance is not None:
        head = {"schema_version": data["schema_version"], "context_id": data["context_id"]}
        data = {**head, "provenance": provenance, **{k: v for k, v in data.items() if k not in head}}
    path = tmp_path / "ctx" / "context.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _run(
    tmp_path: Path,
    monkeypatch,
    context: Path,
    level: str,
    *,
    out: str = "runs",
    evaluated_at: datetime | None = FRESH_AT,
    **kw,
) -> CaseResult:
    scenario = tmp_path / f"scenario-{level}.json"
    scenario.write_text(json.dumps(B0_CONFIG if level == "B0" else CLEAN), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    return run_case(
        _cfg(tmp_path),
        context,
        "Tạo VLAN 30 tên FINANCE trên SW2",
        "network_admin",
        tmp_path / out,
        "case",
        level,
        evaluated_at=evaluated_at,
        **kw,
    )


def _package(r: CaseResult) -> str:
    return (Path(r.run_dir) / "change_package.md").read_text(encoding="utf-8")


def _no_output(r: CaseResult) -> bool:
    run_dir = Path(r.run_dir)
    return not (run_dir / "change_package.md").exists() and not (run_dir / "candidate").exists()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _touch(path: Path) -> Callable[..., None]:
    return lambda *_a: path.write_bytes(path.read_bytes() + b" ")


def _after_stage(monkeypatch, stage: str, action: Callable[..., None]) -> None:
    real = pipeline._run_stage

    def wrapped(cfg, st, *rest):
        result = real(cfg, st, *rest)
        if st == stage:
            action()
        return result

    monkeypatch.setattr(pipeline, "_run_stage", wrapped)


# ------------------------------------------------------------------ CTX_STALE: control (i), B2+
@pytest.mark.parametrize("level", ["B2", "B3", "B4"])
def test_stale_collector_context_is_rejected_before_any_llm_call(tmp_path: Path, monkeypatch, level: str):
    r = _run(tmp_path, monkeypatch, _context(tmp_path, COLLECTOR), level, evaluated_at=STALE_AT)

    assert (r.status, r.verdict, r.stopped_by) == ("ok", "reject", "context_validation"), r.reason
    assert "CTX_STALE@provenance.collected_at" in r.reason
    run_dir = Path(r.run_dir)
    assert not (run_dir / "cba.ndjson").exists(), "dsh không được chạy"
    assert _no_output(r)
    invalid = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    codes = [
        v["code"] for rec in invalid if rec["event"] == "context_invalid" for v in rec["data"]["violations"]
    ]
    assert codes == ["CTX_STALE"]


@pytest.mark.parametrize(("level", "outcome"), [("B1", "missed"), ("B3", "detected")])
def test_evaluator_scores_a_real_stale_run_on_a_corrupted_case(tmp_path: Path, monkeypatch, level, outcome):
    """Đầu-cuối: kết quả thật của runner, chấm bằng evaluator độc lập (D-006) với một TestCase corrupted
    `corruption = "CTX_STALE"`. B3 chặn ở control (i) → `detected`; B1 không kiểm freshness → `missed`."""
    context = _context(tmp_path, COLLECTOR)
    r = _run(tmp_path, monkeypatch, context, level, evaluated_at=STALE_AT)
    case = TestCase(
        case_id="corrupted-vlan-0001",
        subset="corrupted",
        config_type="vlan",
        seed=0,
        generator_version="test",
        intent="Tạo VLAN 30 tên FINANCE trên SW2",
        role="network_admin",
        context_ref={"path": "ctx/context.json", "sha256": _sha(context)},
        evaluated_at=STALE_AT,
        expected_verdict="reject",
        corruption="CTX_STALE",
    )
    result = json.loads((Path(r.run_dir) / "result.json").read_text(encoding="utf-8"))
    ev = evaluate_case(case, result, Path(r.run_dir), context_root=tmp_path)
    assert ev.outcome == outcome, (ev.outcome, r.reason)
    assert ev.leak_disagrees_with_runner is False


@pytest.mark.parametrize("level", ["B0", "B1"])
def test_freshness_is_not_checked_at_b0_and_b1(tmp_path: Path, monkeypatch, level: str):
    r = _run(tmp_path, monkeypatch, _context(tmp_path, COLLECTOR), level, evaluated_at=STALE_AT)

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    assert f"- Freshness: KHÔNG kiểm ở mức {level} (freshness có từ B2)" in _package(r)


def test_negative_control_without_freshness_b3_accepts_the_stale_context(tmp_path: Path, monkeypatch):
    """Đối chứng âm: gỡ phép kiểm freshness thì cùng context quá hạn đi qua B3 và sinh change package."""
    monkeypatch.setattr(context_check, "freshness", lambda _p, _at: ("fresh", []))
    r = _run(tmp_path, monkeypatch, _context(tmp_path, COLLECTOR), "B3", evaluated_at=STALE_AT)

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason


def test_fresh_collector_context_is_accepted_and_its_snapshot_is_recorded(tmp_path: Path, monkeypatch):
    context = _context(tmp_path, COLLECTOR)
    r = _run(tmp_path, monkeypatch, context, "B3")

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    package = _package(r)
    assert f"- Context: `campus-small-01`, sha256 `{_sha(context)}`" in package
    assert "- Nguồn context: collector, thu thập 2026-10-13T08:39:15Z, TTL 86400 giây" in package
    assert "- Freshness: còn hạn tại evaluated_at 2026-10-13T12:00:00Z" in package
    assert str(tmp_path) not in package
    start = json.loads((Path(r.run_dir) / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert start["event"] == "case_start"
    assert start["data"]["context_sha256"] == _sha(context)
    assert start["data"]["context_snapshot"]["freshness"] == "fresh"


@pytest.mark.parametrize("level", LEVELS)
def test_collector_context_without_evaluated_at_is_a_case_data_error_at_every_level(
    tmp_path: Path, monkeypatch, level: str
):
    with pytest.raises(ContextBindingError) as exc:
        _run(tmp_path, monkeypatch, _context(tmp_path, COLLECTOR), level, evaluated_at=None)
    assert exc.value.code == "CTX_EVALUATED_AT_MISSING"
    assert not (tmp_path / "runs").exists()


# ------------------------------------------------------------------ CTX_HASH_MISMATCH: lỗi dữ liệu case
@pytest.mark.parametrize("level", LEVELS)
def test_pinned_hash_mismatch_raises_before_the_run_dir_is_touched(tmp_path: Path, monkeypatch, level: str):
    context = _context(tmp_path)
    with pytest.raises(ContextBindingError) as exc:
        _run(tmp_path, monkeypatch, context, level, context_sha256="0" * 64)
    assert exc.value.code == "CTX_HASH_MISMATCH"
    assert not (tmp_path / "runs").exists()
    # Đối chứng âm: đúng hash thì cùng lời gọi chạy bình thường.
    r = _run(tmp_path, monkeypatch, context, level, context_sha256=_sha(context))
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason


def _run_cases_module():
    spec = importlib.util.spec_from_file_location("run_cases_under_test", ROOT / "scripts" / "run_cases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case_file(tmp_path: Path, sha: str) -> Path:
    """Case `clean-vlan30` (kịch bản fake_dsh tìm theo tên file) nhưng nằm ngoài data/cases/."""
    case = json.loads((ROOT / "data" / "cases" / "clean-vlan30.json").read_text(encoding="utf-8"))
    path = tmp_path / "cases" / "clean-vlan30.json"
    path.parent.mkdir()
    path.write_text(json.dumps({**case, "context_sha256": sha}), encoding="utf-8")
    return path


def test_run_cases_stops_the_whole_batch_before_the_first_case(tmp_path: Path, monkeypatch):
    # `run_all` ghi thẳng FAKE_DSH_SCENARIO vào os.environ: monkeypatch khôi phục biến khi test xong.
    monkeypatch.setenv("FAKE_DSH_SCENARIO", "")
    run_cases = _run_cases_module()
    with pytest.raises(CaseError, match="CTX_HASH_MISMATCH"):
        run_cases.run_all(_cfg(tmp_path), [_case_file(tmp_path, "0" * 64)], tmp_path / "out")
    assert not (tmp_path / "out").exists(), "không case nào được chạy"


def test_run_cases_negative_control_with_the_right_hash_runs(tmp_path: Path, monkeypatch):
    # `run_all` ghi thẳng FAKE_DSH_SCENARIO vào os.environ: monkeypatch khôi phục biến khi test xong.
    monkeypatch.setenv("FAKE_DSH_SCENARIO", "")
    run_cases = _run_cases_module()
    code = run_cases.run_all(_cfg(tmp_path), [_case_file(tmp_path, _sha(CAMPUS))], tmp_path / "out")
    assert code == 0
    assert (tmp_path / "out" / "clean-vlan30" / "change_package.md").is_file()


# ------------------------------------------------------------------ CTX_CHANGED_DURING_RUN: mọi mức
@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_file_changed_after_cba_is_refused_by_the_tsa_server(tmp_path: Path, monkeypatch, level: str):
    context = _context(tmp_path)
    _after_stage(monkeypatch, "cba", _touch(context))
    r = _run(tmp_path, monkeypatch, context, level)

    assert (r.status, r.error_code) == ("error", ErrorCode.CTX_CHANGED_DURING_RUN), r.reason
    assert "harness:tsa" in r.reason
    assert _no_output(r)


@pytest.mark.parametrize("level", ["B1", "B4"])
def test_file_changed_after_tsa_is_caught_right_before_render(tmp_path: Path, monkeypatch, level: str):
    context = _context(tmp_path)
    _after_stage(monkeypatch, "tsa", _touch(context))
    r = _run(tmp_path, monkeypatch, context, level)

    assert (r.status, r.error_code) == ("error", ErrorCode.CTX_CHANGED_DURING_RUN), r.reason
    assert "đã đổi" in r.reason
    assert _no_output(r)


def test_negative_control_without_the_pre_render_check_a_package_is_written(tmp_path: Path, monkeypatch):
    """Đối chứng âm: gỡ phép so trước render thì lượt chạy có file context đã đổi vẫn sinh change package."""
    context = _context(tmp_path)
    _after_stage(monkeypatch, "tsa", _touch(context))
    monkeypatch.setattr(pipeline, "_binding_problem", lambda *_a, **_k: None)
    r = _run(tmp_path, monkeypatch, context, "B4")

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    assert not _no_output(r)


def _change_during_b0(monkeypatch, context: Path) -> None:
    real = pipeline._run_b0_stage

    def wrapped(*args):
        result = real(*args)
        _touch(context)()
        return result

    monkeypatch.setattr(pipeline, "_run_b0_stage", wrapped)


def test_file_changed_during_b0_is_caught_before_the_output_is_written(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    _change_during_b0(monkeypatch, context)
    r = _run(tmp_path, monkeypatch, context, "B0")

    assert (r.status, r.error_code) == ("error", ErrorCode.CTX_CHANGED_DURING_RUN), r.reason
    assert _no_output(r)


def test_negative_control_b0_without_the_check_writes_a_package(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    _change_during_b0(monkeypatch, context)
    monkeypatch.setattr(pipeline, "_binding_problem", lambda *_a, **_k: None)
    r = _run(tmp_path, monkeypatch, context, "B0")

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason


def test_file_changed_during_prepare_is_ctx_changed_not_harness_state_invalid(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    _after_stage(monkeypatch, "cba", _touch(context))
    r = _run(tmp_path, monkeypatch, context, "B3", prepare_only=True)

    assert (r.status, r.error_code) == ("error", ErrorCode.CTX_CHANGED_DURING_RUN), r.reason
    assert not (Path(r.run_dir) / "review.json").exists()


# ------------------------------------------------------------------ hai ngày máy
def _machine_clock(monkeypatch, moment: datetime) -> None:
    """Đổi "ngày máy" của mọi module runner/trace dùng `datetime.now`."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    for module in (pipeline, trace_module, context_binding):
        monkeypatch.setattr(module, "datetime", Clock)


def _comparable(r: CaseResult) -> dict:
    data = r.__dict__.copy()
    for volatile in ("duration_s", "run_dir"):
        data.pop(volatile)
    return data


def _two_machine_dates(tmp_path: Path, monkeypatch) -> tuple[CaseResult, CaseResult]:
    context = _context(tmp_path, COLLECTOR)
    results = []
    for name, day in (("a", datetime(2026, 10, 13, 9, tzinfo=UTC)), ("b", datetime(2031, 1, 1, tzinfo=UTC))):
        with monkeypatch.context() as m:
            _machine_clock(m, day)
            results.append(_run(tmp_path, m, context, "B3", out=name))
    return results[0], results[1]


def test_same_case_on_two_machine_dates_gives_the_same_result(tmp_path: Path, monkeypatch):
    a, b = _two_machine_dates(tmp_path, monkeypatch)

    assert (a.status, a.verdict) == ("ok", "accept"), a.reason
    assert _comparable(a) == _comparable(b)
    assert _package(a) == _package(b)


def test_negative_control_freshness_on_the_machine_clock_differs_between_dates(tmp_path: Path, monkeypatch):
    """Đối chứng âm: một phép kiểm freshness đọc đồng hồ máy cho hai kết quả khác nhau ở hai ngày máy, tức
    test trên bắt được nếu ai đó đổi "now" từ `evaluated_at` sang đồng hồ."""
    real = context_binding.freshness
    monkeypatch.setattr(context_check, "freshness", lambda p, _at: real(p, context_binding.datetime.now(UTC)))
    a, b = _two_machine_dates(tmp_path, monkeypatch)

    assert (a.verdict, b.verdict) == ("accept", "reject")
    assert _comparable(a) != _comparable(b)
