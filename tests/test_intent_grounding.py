"""A9: distinguish G5 proposal consistency, HITL, and missing automatic grounding.

The known-gap test supplies the original intent to the real runner. Only an
observed, successful publication of the wrong VLAN is an expected failure.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import ToolCall
from harness_agent.runner import pipeline
from harness_agent.runner.dsh import LocalConfig

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data/contexts/campus-small-01.json"
INTENT = "Gán cổng GigabitEthernet1/2 trên SW2 vào VLAN 20."


@pytest.fixture
def store():
    return ContextStore.load(CAMPUS)


def vlan_call(vlan_id):
    return {
        "tool": "set_access_vlan",
        "args": {"device": "SW2", "interface": "GigabitEthernet1/2", "vlan_id": vlan_id},
    }


def check_gate(store, proposed_vlan, called_vlan):
    return evaluate(
        call=vlan_call(called_vlan),
        role="network_admin",
        store=store,
        approved=[ToolCall.model_validate(vlan_call(proposed_vlan))],
        staged=[],
    )


def test_gate_allows_matching_proposal(store):
    # G5 sees the proposal, not original natural-language intent.
    decision = check_gate(store, proposed_vlan=20, called_vlan=20)
    assert decision.allowed is True
    assert decision.first_failed is None


def test_gate_blocks_tsa_deviation(store):
    decision = check_gate(store, proposed_vlan=20, called_vlan=10)
    assert decision.allowed is False
    assert decision.first_failed == "G5_intent_consistency"


def test_gate_alone_allows_correlated_cba_tsa_choice(store):
    # Characterization of G5's boundary, NOT evidence that VLAN 10 is the user's intent.
    decision = check_gate(store, proposed_vlan=10, called_vlan=10)
    assert decision.allowed is True
    assert decision.first_failed is None


def run_scenario(tmp_path, monkeypatch, proposed_vlan, called_vlan, *, confirmed=False):
    context = tmp_path / "context.json"
    context.write_bytes(CAMPUS.read_bytes())
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "cba": [
                    {
                        "tool": "submit_change_request",
                        "args": {
                            "status": "ready",
                            "changes": [vlan_call(proposed_vlan)],
                        },
                    }
                ],
                "tsa": [vlan_call(called_vlan), {"tool": "finalize", "args": {}}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    contract = None
    if confirmed:
        # This trusted fixture is an operator oracle, never a CBA-generated approval.
        contract = tmp_path / "confirmed.json"
        contract.write_text(
            json.dumps(
                {
                    "intent": INTENT,
                    "context_sha256": hashlib.sha256(context.read_bytes()).hexdigest(),
                    "changes": [vlan_call(20)],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    cfg = LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts/fake_dsh.py")],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests/fixtures/dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )
    result = pipeline.run_case(
        cfg,
        context,
        INTENT,
        "network_admin",
        tmp_path / "runs",
        "case",
        level="B4",
        confirmed_intent_path=contract,
    )
    # Ordinary assertions must FAIL (not xfail) for runtime or fixture regressions.
    assert result.status == "ok", result
    assert result.error_code is None, result
    folder = Path(result.run_dir)
    assert INTENT in (folder / "cba.prompt.txt").read_text(encoding="utf-8")
    cr = json.loads((folder / "change_request.json").read_text(encoding="utf-8"))
    assert cr["changes"] == [vlan_call(proposed_vlan)]
    return result, folder


def assert_published_vlan(folder, vlan):
    assert (folder / "change_package.md").is_file()
    assert (folder / "candidate/SW2.cfg").read_text(encoding="utf-8") == (
        f"interface GigabitEthernet1/2\n switchport mode access\n switchport access vlan {vlan}\nexit\nend\n"
    )


def assert_no_publication(folder):
    assert not (folder / "candidate").exists()
    assert not (folder / "change_package.md").exists()


@pytest.mark.slow
def test_pipeline_accepts_proposal_matching_original_intent(tmp_path, monkeypatch):
    result, folder = run_scenario(tmp_path, monkeypatch, 20, 20)
    assert result.verdict == "accept", result
    assert_published_vlan(folder, 20)


@pytest.mark.slow
def test_pipeline_blocks_tsa_deviation_from_proposal(tmp_path, monkeypatch):
    result, folder = run_scenario(tmp_path, monkeypatch, 20, 10)
    assert (result.verdict, result.stopped_by) == ("reject", "G5_intent_consistency")
    assert_no_publication(folder)


@pytest.mark.slow
def test_operator_confirmation_blocks_wrong_proposal_but_is_not_automatic_grounding(tmp_path, monkeypatch):
    result, folder = run_scenario(tmp_path, monkeypatch, 10, 10, confirmed=True)
    assert (result.verdict, result.stopped_by) == ("reject", "confirmed_intent")
    assert not (folder / "tsa.prompt.txt").exists()
    assert_no_publication(folder)


class UngroundedPublication(AssertionError):
    """Only the demonstrated semantic gap may be reported as xfail."""


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    raises=UngroundedPublication,
    reason=(
        "Automatic grounding missing: runner receives VLAN 20 intent but publishes agreed CBA/TSA VLAN 10."
    ),
)
def test_pipeline_should_block_correlated_wrong_proposal_for_original_intent(tmp_path, monkeypatch):
    # No confirmed_intent: an operator oracle would hide the automatic-grounding gap.
    result, folder = run_scenario(tmp_path, monkeypatch, 10, 10)
    if result.verdict == "accept":
        # Only xfail after proving the wrong configuration was actually published.
        assert_published_vlan(folder, 10)
        raise UngroundedPublication("Original intent VLAN 20; published VLAN 10")
    assert result.verdict in {"reject", "clarify"}, result
    assert result.stopped_by == "intent_grounding", result
    assert_no_publication(folder)
    # A real future grounding control makes this XPASS(strict), requiring marker review.
