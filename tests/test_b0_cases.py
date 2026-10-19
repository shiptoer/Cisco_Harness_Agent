"""16 case chạy khô ở điều kiện B0 (D-044), so với KỲ VỌNG FIXTURE của kịch bản B0 (D-045).

Kỳ vọng fixture (`expect` trong `data/scenarios_b0/<case>.json`) là kỳ vọng của ĐƯỜNG ỐNG: kịch bản giả trả
lời gì thì runner phải phân loại đúng thế. Nó không phải nhãn đúng của case; nhãn đúng nằm trong file case
(`label`) và được kiểm ở `test_cases.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_agent.harness.leak import secret_lines
from harness_agent.levels import Level
from harness_agent.models import parse_tool_call
from harness_agent.runner.b0 import parse_response, read_context
from harness_agent.runner.cases import case_files, fixture_expect, label_outcome, load_case, scenario_path
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import _is_package_file, expect_mismatches, run_case
from harness_agent.tools.registry.render import render_by_device

ROOT = Path(__file__).resolve().parents[1]
CASES = case_files()  # 14 case có nhãn + 2 case fixture (`fault-*`)
CLEAN = [p for p in CASES if p.stem.startswith("clean-")]


def _fake_cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "unused_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


@pytest.mark.slow
@pytest.mark.parametrize("case_file", CASES, ids=lambda p: p.stem)
def test_dry_run_cases_at_b0(case_file: Path, tmp_path: Path, monkeypatch):
    case = load_case(case_file)
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario_path(case_file, Level.B0)))
    r = run_case(
        _fake_cfg(tmp_path),
        ROOT / case.context,
        case.intent,
        case.role.value,
        tmp_path,
        case.run_id,
        Level.B0,
    )
    assert expect_mismatches(r, fixture_expect(case_file, Level.B0)) == [], r.reason
    assert (r.level, r.controls) == ("B0", ["v"])
    # Ở mọi case: secret của context không nằm trong file nào ngoài thứ gửi người duyệt (D-018), kể cả
    # prompt đã lưu, dù prompt B0 thật có chứa context thô.
    secrets = read_context(ROOT / case.context)[1]
    run_dir = Path(r.run_dir)
    leaked = [
        p.relative_to(run_dir).as_posix()
        for p in run_dir.rglob("*")
        if p.is_file() and not _is_package_file(run_dir, p) and secret_lines(p.read_bytes(), secrets)
    ]
    assert secrets and leaked == []
    # Mục `dsh_home` ở B0 luôn là phía đầu vào.
    assert all(s.get("input_side") for s in r.leak_sources if s["source"] == "dsh_home" and "file" in s)


@pytest.mark.parametrize("case_file", CASES, ids=lambda p: p.stem)
def test_every_b0_fixture_expectation_is_complete(case_file: Path):
    expect = fixture_expect(case_file, Level.B0)
    assert expect is not None and set(expect) == {
        "status",
        "verdict",
        "stopped_by",
        "error_code",
        "leak_attempt_detected",
        "leak_in_package",
        "leak_sources",
    }
    scenario = json.loads(scenario_path(case_file, Level.B0).read_text(encoding="utf-8"))
    assert set(scenario) == {"expect", "b0"}


@pytest.mark.parametrize("case_file", CLEAN, ids=lambda p: p.stem)
def test_clean_b0_scenarios_answer_with_exactly_what_b3_renders(case_file: Path):
    """Ở 4 case clean, "model" giả của B0 trả đúng từng byte CLI mà B3 render từ change request của nó."""
    b3 = json.loads(scenario_path(case_file, Level.B3).read_text(encoding="utf-8"))
    changes = next(s for s in b3["cba"] if s["tool"] == "submit_change_request")["args"]["changes"]
    staged = [parse_tool_call(c) for c in changes]
    rendered = render_by_device([(s.tool, s.parsed) for s in staged])
    b0 = json.loads(scenario_path(case_file, Level.B0).read_text(encoding="utf-8"))["b0"]
    assert parse_response(b0["final"]).configs == rendered


def test_the_b0_fixtures_cover_every_outcome_class():
    seen = {
        (e["status"], e["verdict"], e["stopped_by"], e["error_code"], e["leak_in_package"])
        for e in (fixture_expect(p, Level.B0) for p in CASES)
    }
    assert seen == {
        ("ok", "accept", None, None, False),
        ("ok", "accept", None, None, True),
        ("ok", "clarify", None, None, False),
        ("ok", "reject", "model_refusal", None, False),
        ("error", None, None, "RUNTIME_TOOL_VIOLATION", False),
    }


def test_at_b0_the_fake_model_complies_with_attacks_so_fixture_and_label_disagree_by_design():
    """Vì sao hai cột của `run_cases` phải tách: ở B0 chạy khô, "model" giả làm theo 8 trong 9 tấn công. Kỳ
    vọng fixture ghi đúng điều đó (accept), còn theo nhãn đúng (reject) thì đó là SAI."""
    from harness_agent.runner.pipeline import CaseResult

    disagree = []
    for path in CASES:
        label = load_case(path).label
        expect = fixture_expect(path, Level.B0)
        if label is None or expect["status"] != "ok":
            continue
        as_fixture = CaseResult(run_id="x", status="ok", verdict=expect["verdict"])
        if label_outcome(as_fixture, label) == "SAI":
            disagree.append(path.stem)
    assert len(disagree) == 8 and all(name.startswith("adv-") for name in disagree)
    assert "adv-privilege-escalation" not in disagree  # kịch bản giả của case này từ chối
