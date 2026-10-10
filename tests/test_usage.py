"""Token, số request và thời gian trong result.json (D-021).

Nguyên tắc được kiểm: chỉ chép lại những gì dsh báo. Trường dsh không cung cấp là None, không bao giờ
là 0 và không bao giờ được ước tính.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import CaseResult, ErrorCode, run_case
from harness_agent.runner.usage import stage_usage, token_sum, total_usage

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
SUBMIT = {"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}
CLEAN = {"cba": [{"tool": "list_devices", "args": {}}, SUBMIT], "tsa": "auto"}
# Mỗi bước của fake_dsh báo đúng các số này (FAKE_USAGE trong scripts/fake_dsh.py).
STEP = {"input_uncached": 100, "input_cached": 300, "cache_write": 0, "output": 20}
NO_TOKENS = {
    "input_uncached": None,
    "input_cached": None,
    "cache_write": None,
    "output": None,
    "reasoning": None,
}


def _steps(n: int) -> dict:
    return {"requests": n, **{k: v * n for k, v in STEP.items()}, "reasoning": None}


def _cfg(tmp_path: Path, **overrides) -> LocalConfig:
    base = {
        "dsh_command": [sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        "dsh_home": tmp_path / "unused_home",
        "base_patch": ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        "python_exe": Path(sys.executable),
        "requires_api_key": False,
    }
    return LocalConfig(**{**base, **overrides})


def _run(tmp_path: Path, monkeypatch, scenario: dict, **cfg) -> CaseResult:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path, **cfg), CAMPUS, "intent thử", "network_admin", tmp_path / "runs", "case")


def _line(**event) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def _step_end(**usage) -> bytes:
    return _line(type="status", phase="step_end", turn=1, step=1, **({"usage": usage} if usage else {}))


# ------------------------------------------------------------ stage_usage
def test_real_dsh_lines_are_summed_per_step():
    """Ba dòng `step_end` thật của stage cba trong lượt chạy LLM đầu tiên (01/10/2026)."""
    real = (
        b'{"type":"status","phase":"step_start","turn":1,"step":1}\n'
        b'{"type":"status","phase":"step_end","turn":1,"step":1,"usage":{"inputTokens":1600,"outputTokens":114,'
        b'"cacheReadTokens":0,"cacheWriteTokens":0,"totalTokens":1714}}\n'
        b'{"type":"status","phase":"step_end","turn":1,"step":2,"usage":{"inputTokens":627,"outputTokens":344,'
        b'"cacheReadTokens":1664,"cacheWriteTokens":0,"totalTokens":2635}}\n'
        b'{"type":"status","phase":"step_end","turn":1,"step":3,"usage":{"inputTokens":236,"outputTokens":30,'
        b'"cacheReadTokens":2432,"cacheWriteTokens":0,"totalTokens":2698}}\n'
        b'{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"completed"}}\n'
        b'{"type":"final","text":"done"}\n'
    )
    usage = stage_usage(real)
    assert usage == {
        "requests": 3,
        "input_uncached": 2463,
        "input_cached": 4096,
        "cache_write": 0,
        "output": 488,
        "reasoning": None,  # dsh không báo riêng token reasoning
    }
    assert token_sum(usage) == 7047  # bằng tổng `totalTokens` của ba bước


def test_real_dsh_without_an_api_key_counts_the_failed_step_and_reports_no_tokens():
    """Output thật của dsh 0.2.0-rc.1 khi không có API key (01/10/2026; bỏ dòng `session`, thông báo lỗi
    rút gọn). Bước lỗi có `step_end` nhưng không có `usage`: được đếm là một bước, token là None (không
    phải 0)."""
    real = (
        b'{"type":"status","phase":"turn_start","turn":1}\n'
        b'{"type":"status","phase":"step_start","turn":1,"step":1}\n'
        b'{"type":"status","phase":"step_end","turn":1,"step":1}\n'
        b'{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"error","error":'
        b'{"message":"llm-deepseek: no API key","code":"MISSING_CREDENTIAL"}}}\n'
        b'{"type":"final","text":""}\n'
    )
    assert stage_usage(real) == {"requests": 1, **NO_TOKENS}


def test_no_step_means_unknown_not_zero():
    assert stage_usage(b"") == {"requests": 0, **NO_TOKENS}
    assert stage_usage(_line(type="final", text="x")) == {"requests": 0, **NO_TOKENS}
    assert stage_usage(b"not json\n" + _line(type="error", message="unknown option")) == {
        "requests": 0,
        **NO_TOKENS,
    }


def test_step_without_usage_makes_tokens_unknown_but_still_counts_the_request():
    reported = {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 5, "cacheWriteTokens": 0}
    usage = stage_usage(_step_end(**reported) + _step_end())
    assert usage == {"requests": 2, **NO_TOKENS}
    assert token_sum(usage) is None


def test_a_missing_or_invalid_field_is_unknown_only_for_that_field():
    ok = {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 5, "cacheWriteTokens": 0}
    partial = {
        "inputTokens": 7,
        "outputTokens": "many",
        "cacheWriteTokens": 0,
    }  # thiếu cacheRead; output sai kiểu
    assert stage_usage(_step_end(**ok) + _step_end(**partial)) == {
        "requests": 2,
        "input_uncached": 17,
        "input_cached": None,
        "cache_write": 0,
        "output": None,
        "reasoning": None,
    }
    for bad in (-1, True, 1.5, None):
        assert stage_usage(_step_end(**{**ok, "inputTokens": bad}))["input_uncached"] is None


def test_total_is_unknown_when_any_stage_is_unknown():
    assert total_usage({"cba": _steps(2), "tsa": _steps(3)}) == _steps(5)
    assert total_usage({"cba": _steps(2), "tsa": {"requests": 1, **NO_TOKENS}}) == {
        "requests": 3,
        **NO_TOKENS,
    }
    assert total_usage({}) == {"requests": 0, **NO_TOKENS}


# ------------------------------------------------------------ result.json
@pytest.mark.slow
def test_accept_case_reports_usage_and_time_per_stage_and_total(tmp_path: Path, monkeypatch):
    r = _run(tmp_path, monkeypatch, CLEAN)
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason

    # cba: list_devices, submit, bước kết = 3 request; tsa: create_vlan, finalize, bước kết = 3 request.
    assert r.usage == {"cba": _steps(3), "tsa": _steps(3), "total": _steps(6)}
    assert set(r.duration_s) == {"cba", "tsa", "total"}
    assert all(isinstance(v, float) and v > 0 for v in r.duration_s.values())
    assert r.duration_s["total"] >= r.duration_s["cba"] + r.duration_s["tsa"]
    saved = json.loads((Path(r.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert saved["usage"] == r.usage and saved["duration_s"] == r.duration_s


@pytest.mark.slow
def test_stage_that_never_ran_is_absent_not_zero(tmp_path: Path, monkeypatch):
    ask = {"status": "needs_clarification", "clarification_question": "Thiết bị nào?"}
    r = _run(tmp_path, monkeypatch, {"cba": [{"tool": "submit_change_request", "args": ask}]})
    assert r.verdict == "clarify"
    assert r.usage == {"cba": _steps(2), "total": _steps(2)}
    assert set(r.duration_s) == {"cba", "total"}


@pytest.mark.slow
def test_usage_is_null_when_dsh_does_not_report_it(tmp_path: Path, monkeypatch):
    """Đối chứng cho test accept ở trên: cùng kịch bản, dsh không kèm `usage`. Kết cục không đổi, token
    là null, số request vẫn đếm được."""
    r = _run(tmp_path, monkeypatch, {**CLEAN, "usage": None})
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    unknown = {"requests": 3, **NO_TOKENS}
    assert r.usage == {"cba": unknown, "tsa": unknown, "total": {"requests": 6, **NO_TOKENS}}
    saved = json.loads((Path(r.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert saved["usage"]["total"]["input_uncached"] is None  # null trong JSON, không phải 0


@pytest.mark.slow
def test_error_outcomes_still_carry_usage_and_time(tmp_path: Path, monkeypatch):
    """dsh thoát ngay vì tham số sai: không có bước nào, token là null; thời gian vẫn được đo."""
    cfg = {"dsh_command": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    r = _run(tmp_path, monkeypatch, CLEAN, **cfg)
    assert r.error_code is ErrorCode.DSH_EXIT
    assert r.usage == {"cba": {"requests": 0, **NO_TOKENS}, "total": {"requests": 0, **NO_TOKENS}}
    assert r.duration_s["cba"] > 0 and r.duration_s["total"] >= r.duration_s["cba"]


@pytest.mark.slow
def test_partial_output_of_a_timed_out_stage_is_counted(tmp_path: Path, monkeypatch):
    """Stage quá giờ: những bước dsh đã báo trước khi bị dừng vẫn được tính (đã tốn tiền)."""
    step = json.dumps(
        {
            "type": "status",
            "phase": "step_end",
            "turn": 1,
            "step": 1,
            "usage": {
                "inputTokens": 11,
                "outputTokens": 3,
                "cacheReadTokens": 7,
                "cacheWriteTokens": 0,
                "totalTokens": 21,
            },
        }
    )
    code = f"import time; print({step!r}, flush=True); time.sleep(60)"
    r = _run(tmp_path, monkeypatch, CLEAN, dsh_command=[sys.executable, "-c", code], timeout_s=3)
    assert r.error_code is ErrorCode.TIMEOUT
    assert r.usage["cba"] == {
        "requests": 1,
        "input_uncached": 11,
        "input_cached": 7,
        "cache_write": 0,
        "output": 3,
        "reasoning": None,
    }
    assert r.duration_s["cba"] >= 3


def test_context_rejected_before_any_stage_has_empty_usage(tmp_path: Path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(tmp_path / "unused.json"))
    r = run_case(_cfg(tmp_path), bad, "intent", "network_admin", tmp_path / "runs", "case")
    assert (r.verdict, r.stopped_by) == ("reject", "context_validation")
    assert r.usage == {"total": {"requests": 0, **NO_TOKENS}}
    assert set(r.duration_s) == {"total"}


# ------------------------------------------------------------ scripts/run_cases.py
@pytest.mark.slow
@pytest.mark.parametrize("names", [["clean-vlan30", "ambiguous-vlan40"]])
def test_run_cases_prints_token_and_seconds_columns_and_a_total(tmp_path: Path, names: list[str]):
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_cases.py",
            "--config",
            "config/fake.toml",
            "--out",
            str(tmp_path),
            *names,
        ],
        capture_output=True,
        cwd=ROOT,
        timeout=300,
        check=False,
    )
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, out + proc.stderr.decode("utf-8", "replace")
    lines = out.splitlines()
    header = lines[0].split()
    assert header[-4:-2] == ["token", "giây"]
    results = {n: json.loads((tmp_path / n / "result.json").read_text(encoding="utf-8")) for n in names}
    for name in names:
        row = next(x for x in lines if x.startswith(name)).split()
        assert int(row[-3]) == token_sum(results[name]["usage"]["total"])
        assert float(row[-2]) == pytest.approx(results[name]["duration_s"]["total"], abs=0.06)
    total = next(x for x in lines if x.startswith("Tổng:"))
    expected = sum(token_sum(r["usage"]["total"]) for r in results.values())
    requests = sum(r["usage"]["total"]["requests"] for r in results.values())
    assert f"Tổng: {expected} token" in total and f"{requests} request" in total
    assert "không có số liệu" not in total
