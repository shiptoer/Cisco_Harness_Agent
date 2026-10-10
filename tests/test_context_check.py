"""Control (i) ở runner (D-005, D-043): contract + invariants chạy TRƯỚC mọi lời gọi LLM.

- Context có vi phạm → reject, `stopped_by=context_validation`, trace `context_invalid` có đúng mã và
  đường dẫn, và KHÔNG có `stage_start` nào: dsh không được gọi.
- Đối chứng: cùng runner, context sạch thì đi tiếp tới stage đầu tiên (stage được thay bằng một hàm ghi
  nhận, nên test không sinh subprocess).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_agent.runner import pipeline
from harness_agent.runner.context_check import context_violations, format_loc
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import CaseResult, ErrorCode, run_case

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
MARKER = "MARKER-ctx-q7"


def _cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "unused_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _trace(r: CaseResult) -> list[dict]:
    lines = (Path(r.run_dir) / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


def _run(tmp_path: Path, context: Path, monkeypatch) -> CaseResult:
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(tmp_path / "unused.json"))
    return run_case(_cfg(tmp_path), context, "intent thử", "network_admin", tmp_path / "runs", "case")


def _with_missing_acl() -> dict:
    """ACL không tồn tại: chỉ invariant mới (`CTX_ACL_REF`) bắt được; `ContextStore` cũ cho qua. Vì vậy
    test dưới đỏ nếu runner bỏ bước `context_violations` (đã thử: case chạy tới dsh)."""
    data = json.loads(CAMPUS.read_text(encoding="utf-8"))
    data["devices"][0]["interfaces"][0]["acl_in"] = "NO-SUCH-ACL"
    data["devices"][0]["interfaces"][0]["description"] = MARKER
    return data


def test_a_violation_rejects_before_any_stage(tmp_path: Path, monkeypatch):
    r = _run(tmp_path, _write(tmp_path, _with_missing_acl()), monkeypatch)

    assert (r.status, r.verdict, r.stopped_by, r.error_code) == ("ok", "reject", "context_validation", None)
    events = [x["event"] for x in _trace(r)]
    assert "stage_start" not in events and "preflight_failed" not in events
    assert not list(Path(r.run_dir).glob("*.ndjson"))  # dsh không chạy
    invalid = [x["data"]["violations"] for x in _trace(r) if x["event"] == "context_invalid"]
    assert [[(v["code"], v["path"]) for v in vs] for vs in invalid] == [
        [("CTX_ACL_REF", "devices[0].interfaces[0].acl_in")]
    ]
    assert "CTX_ACL_REF@devices[0].interfaces[0].acl_in" in r.reason
    assert set(r.usage) == {"total"} and r.usage["total"]["requests"] == 0  # không request nào
    assert MARKER not in (Path(r.run_dir) / "trace.jsonl").read_text(encoding="utf-8")
    assert MARKER not in r.reason


def test_negative_control_a_clean_context_reaches_the_first_stage(tmp_path: Path, monkeypatch):
    """Đối chứng âm: context sạch thì runner đi tới stage CBA. Stage được thay bằng một hàm ghi nhận rồi dừng
    case bằng một lỗi có mã, nên không sinh subprocess."""
    reached: list[str] = []

    def record_then_stop(cfg, stage, *_rest):
        reached.append(stage)
        raise pipeline._StageError(ErrorCode.PREFLIGHT_FAILED, "dừng có chủ đích trong test")

    monkeypatch.setattr(pipeline, "_run_stage", record_then_stop)
    r = _run(tmp_path, CAMPUS, monkeypatch)
    assert reached == ["cba"]
    assert r.stopped_by is None and r.error_code is ErrorCode.PREFLIGHT_FAILED
    assert "context_invalid" not in [x["event"] for x in _trace(r)]


def test_contract_errors_become_ctx_schema_with_a_path(tmp_path: Path):
    data = json.loads(CAMPUS.read_text(encoding="utf-8"))
    data["devices"][1]["role"] = MARKER
    found = context_violations(_write(tmp_path, data))
    assert [(v.code, v.path) for v in found] == [("CTX_SCHEMA", "devices[1].role")]
    assert found[0].detail.startswith("[enum] ") and MARKER not in found[0].detail


def test_unreadable_and_non_object_files(tmp_path: Path):
    assert [v.code for v in context_violations(tmp_path / "missing.json")] == ["CTX_UNREADABLE"]
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert [v.code for v in context_violations(bad)] == ["CTX_SCHEMA"]


def test_a_byte_order_mark_is_accepted_like_the_context_store(tmp_path: Path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + CAMPUS.read_bytes())
    assert context_violations(path) == []


def test_real_context_has_no_violation():
    assert context_violations(CAMPUS) == []


@pytest.mark.parametrize(
    ("loc", "path"),
    [
        (("devices", 1, "role"), "devices[1].role"),
        (("policy", "protected_interfaces", "SW2", 2), "policy.protected_interfaces.SW2[2]"),
        ((), ""),
    ],
)
def test_format_loc_matches_the_violation_path_style(loc: tuple, path: str):
    assert format_loc(loc) == path
