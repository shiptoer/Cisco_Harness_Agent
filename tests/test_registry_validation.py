"""A10a: strict numeric tool arguments and IPv4 host boundaries, offline only."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_levels import CAMPUS, _cfg

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import ToolCall
from harness_agent.runner import pipeline
from harness_agent.tools.registry.render import render_by_device

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "TEST"}}
ACCESS = {
    "tool": "set_access_vlan",
    "args": {"device": "SW2", "interface": "GigabitEthernet1/2", "vlan_id": 20},
}
TRUNK = {
    "tool": "set_trunk",
    "args": {
        "device": "SW2",
        "interface": "GigabitEthernet0/1",
        "allowed_vlans": [10, 20],
        "native_vlan": 10,
    },
}
IP = {
    "tool": "set_interface_ip",
    "args": {"device": "R1", "interface": "GigabitEthernet0/2", "ip": "10.1.1.1", "prefix_len": 24},
}
ACL = {
    "tool": "add_acl_entry",
    "args": {
        "device": "R1",
        "acl_name": "TEST",
        "seq": 10,
        "action": "permit",
        "protocol": "ip",
        "src": "any",
        "dst": "any",
    },
}
FIELDS = [
    (VLAN, "vlan_id"),
    (ACCESS, "vlan_id"),
    (TRUNK, "native_vlan"),
    (TRUNK, "allowed_vlans"),
    (IP, "prefix_len"),
    (ACL, "seq"),
]


@pytest.mark.parametrize("call,field", FIELDS)
@pytest.mark.parametrize("bad", [True, False, "30", 30.0])
def test_rejects_numeric_coercion_at_json_boundary(call, field, bad):
    payload = copy.deepcopy(call)
    payload["args"][field] = [10, bad] if field == "allowed_vlans" else bad
    with pytest.raises(ValidationError):
        ToolCall.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("call,field", FIELDS)
def test_valid_integer_arguments_still_render(call, field):
    validated = ToolCall.model_validate_json(json.dumps(call))
    assert validated.args[field] == call["args"][field]
    assert render_by_device([(validated.tool, validated.parsed)])


@pytest.mark.parametrize(
    "ip,prefix", [("10.1.1.0", 24), ("10.1.1.255", 24), ("10.1.1.4", 30), ("10.1.1.7", 30)]
)
def test_rejects_subnet_endpoints(ip, prefix):
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"tool": IP["tool"], "args": {**IP["args"], "ip": ip, "prefix_len": prefix}})


@pytest.mark.parametrize(
    "ip,prefix",
    [
        ("10.1.1.1", 24),
        ("10.1.1.254", 24),
        ("10.1.1.0", 23),
        ("10.1.0.255", 23),
        ("10.1.1.0", 31),
        ("10.1.1.1", 31),
        ("10.1.1.0", 32),
        ("10.1.1.255", 32),
    ],
)
def test_valid_hosts_and_31_32_exceptions_render(ip, prefix):
    call = ToolCall.model_validate(
        {"tool": IP["tool"], "args": {**IP["args"], "ip": ip, "prefix_len": prefix}}
    )
    assert f"ip address {ip} " in render_by_device([(call.tool, call.parsed)])["R1"]


@pytest.mark.parametrize(
    "call",
    [
        {"tool": "create_vlan", "args": {**VLAN["args"], "vlan_id": "30"}},
        {"tool": "set_interface_ip", "args": {**IP["args"], "ip": "10.1.1.0"}},
    ],
)
def test_gate_rejects_invalid_arguments_before_other_policy(call):
    decision = evaluate(call, "network_admin", ContextStore.load(CAMPUS), [], [])
    assert not decision.allowed
    assert decision.first_failed == "G2_schema"


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_invalid_tsa_numeric_argument_rejects_without_publication(tmp_path, monkeypatch, level):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
                "tsa": [
                    {"tool": "create_vlan", "args": {**VLAN["args"], "vlan_id": "30"}},
                    {"tool": "finalize", "args": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    result = pipeline.run_case(
        _cfg(tmp_path),
        CAMPUS,
        "Create VLAN 30 TEST on SW2",
        "network_admin",
        tmp_path / "runs",
        "case",
        level=level,
    )
    assert (result.status, result.verdict, result.stopped_by) == ("ok", "reject", "G2_schema")
    folder = Path(result.run_dir)
    assert not (folder / "candidate").exists()
    assert not (folder / "change_package.md").exists()
