from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import ToolCall

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: gate does not receive the original user intent; "
        "matching but incorrect CBA/TSA proposals are allowed."
    ),
)
def test_wrong_cba_and_tsa_should_not_satisfy_user_intent(store):
    # Original user intent: VLAN 20.
    # Simulate CBA selecting VLAN 10 and TSA following it.
    # The current gate has no input for the original intent.
    decision = check_gate(store, proposed_vlan=10, called_vlan=10)

    assert decision.allowed is False
