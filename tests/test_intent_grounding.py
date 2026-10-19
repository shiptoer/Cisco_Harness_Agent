"""Intent grounding (D-061, hiện thực D-051 / D-030).

Gate G5 chỉ so TSA với ChangeRequest của CBA: CBA và TSA cùng sai (VLAN 10 khi user nói VLAN 20) thì gate
cho qua (test gate bên dưới ghi lại đúng điều đó). Runner đóng lỗ hổng này bằng bước grounding sau CBA: ở
B3/B4, giá trị không neo được vào intent gốc → clarify, `stopped_by == "intent_grounding"`. Đối chứng âm: gỡ
`ground` (trả `[]`) thì cùng kịch bản lại thành accept.
"""

import json
import sys
from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import ToolCall
from harness_agent.runner import pipeline
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import run_case

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data/contexts/campus-small-01.json"
INTENT = "Gán GigabitEthernet1/2 trên SW2 vào VLAN 20"


@pytest.fixture
def store():
    return ContextStore.load(ROOT / "data/contexts/campus-small-01.json")


def vlan_call(vlan_id):
    return {
        "tool": "set_access_vlan",
        "args": {
            "device": "SW2",
            "interface": "GigabitEthernet1/2",
            "vlan_id": vlan_id,
        },
    }


def check_gate(store, proposed_vlan, called_vlan):
    return evaluate(
        call=vlan_call(called_vlan),
        role="network_admin",
        store=store,
        approved=[ToolCall.model_validate(vlan_call(proposed_vlan))],
        staged=[],
    )


def test_matching_correct_proposal_is_allowed(store):
    # User wants VLAN 20; CBA and TSA both select VLAN 20.
    decision = check_gate(store, proposed_vlan=20, called_vlan=20)

    assert decision.allowed is True
    assert decision.first_failed is None


def test_tsa_deviation_is_blocked(store):
    # CBA selects VLAN 20, but TSA attempts VLAN 10.
    decision = check_gate(store, proposed_vlan=20, called_vlan=10)

    assert decision.allowed is False
    assert decision.first_failed == "G5_intent_consistency"


def test_gate_alone_lets_a_wrong_but_matching_proposal_through(store):
    # Lỗ hổng D-051 ở mức gate: CBA chọn VLAN 10 (user nói 20), TSA làm theo; G1–G6 không thấy intent gốc nên
    # cho qua. Bước grounding ở runner (các test dưới) mới là chỗ chặn.
    decision = check_gate(store, proposed_vlan=10, called_vlan=10)

    assert decision.allowed is True


# ------------------------------------------------------------------ runner: grounding qua run_case
def _cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def _run(tmp_path, monkeypatch, vlan_id: int, level: str):
    """CBA (giả) đề xuất gán SW2 Gi1/2 vào `vlan_id`; TSA làm đúng theo đề xuất. Intent luôn nói VLAN 20."""
    scenario = {
        "cba": [
            {
                "tool": "submit_change_request",
                "args": {"status": "ready", "changes": [vlan_call(vlan_id)]},
            }
        ],
        "tsa": "auto",
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path), CAMPUS, INTENT, "network_admin", tmp_path / "runs", "case", level)


def _grounding_events(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "runs" / "case" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x)["data"] for x in lines if json.loads(x)["event"] == "grounding"]


@pytest.mark.slow
def test_wrong_cba_and_tsa_are_stopped_by_intent_grounding_at_b4(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, vlan_id=10, level="B4")

    assert (r.status, r.verdict, r.stopped_by) == ("ok", "clarify", "intent_grounding"), r.reason
    assert "vlan_id" in r.reason and "10" not in r.reason  # lý do chỉ ghi tên trường, không ghi giá trị
    assert not (tmp_path / "runs" / "case" / "change_package.md").exists()
    assert r.tool_calls.get("tsa") is None  # dừng trước TSA
    [event] = _grounding_events(tmp_path)
    assert event["enforced"] is True
    assert [(v["index"], v["field"], v["value"]) for v in event["violations"]] == [(0, "vlan_id", "10")]


@pytest.mark.slow
def test_negative_control_without_grounding_the_wrong_proposal_is_accepted(tmp_path, monkeypatch):
    """Đối chứng âm: gỡ control (ground luôn trả []) thì đúng kịch bản trên lại thành accept."""
    monkeypatch.setattr(pipeline, "ground", lambda intent, changes, store: [])
    r = _run(tmp_path, monkeypatch, vlan_id=10, level="B4")

    assert (r.status, r.verdict, r.stopped_by) == ("ok", "accept", None), r.reason


@pytest.mark.slow
def test_correct_proposal_is_accepted_at_b4(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, vlan_id=20, level="B4")

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    assert _grounding_events(tmp_path) == [{"enforced": True, "violations": []}]


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2"])
def test_grounding_is_shadow_below_b3(tmp_path, monkeypatch, level):
    r = _run(tmp_path, monkeypatch, vlan_id=10, level=level)

    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    [event] = _grounding_events(tmp_path)
    assert event["enforced"] is False and [v["field"] for v in event["violations"]] == ["vlan_id"]
