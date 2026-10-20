"""Regression tests for collector/sensor hardening; no live network access."""

import json
from pathlib import Path

import pytest
from registry_samples import VALID_ARGS

from harness_agent.collector import fetch
from harness_agent.collector.discover import discover
from harness_agent.collector.parse import _vlan_list, identify_platform, parse_running_config
from harness_agent.models import parse_tool_call
from harness_agent.sensors import run_sensors
from harness_agent.sensors.intent import check_intent
from harness_agent.tools.registry.render import render_by_device


@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (["10,20-22", "remove 20-21"], [10, 22]),
        (["none", "add 20,30", "remove 20"], [30]),
        (["all", "except 2-4094"], [1]),
        (["remove 2-4094"], [1]),
        (["add 10"], list(range(1, 4095))),
        (["10", "20"], [20]),
    ],
)
def test_ios_trunk_set_algebra(commands, expected):
    text = "hostname SW\ninterface Ethernet0/0\n switchport mode trunk\n"
    text += "".join(f" switchport trunk allowed vlan {c}\n" for c in commands)
    data, _ = parse_running_config(text, "access_switch")
    assert data["interfaces"][0]["trunk_allowed"] == expected


@pytest.mark.parametrize("value", ["1-999999999", "20-10", "0", "4095", "1,,2", "-2"])
def test_vlan_range_bounded_and_invalid_rejected(value):
    with pytest.raises(ValueError):
        _vlan_list(value)


def test_observations_keep_security_without_secret():
    data, _ = parse_running_config(
        "hostname R1\nvrf definition MGMT\ninterface Ethernet0/0\n vrf forwarding MGMT\n"
        "line vty 0 4\n access-class ADMIN in\n login authentication VTY\n"
        "aaa authorization exec default group tacacs+ local\nusername admin secret 9 HIDDENVALUE\n",
        "router",
    )
    obs = data["collected_security"]
    assert obs["vrf_interfaces"] == {"Ethernet0/0": "MGMT"}
    assert "access-class ADMIN in" in obs["vty"]
    assert "aaa_authorization" in obs["unsupported"]
    assert "HIDDENVALUE" not in json.dumps(data)


@pytest.mark.parametrize("host", ["../escape", "..", "x/y", "x\\y", "x\n"])
def test_inventory_path_traversal_rejected(tmp_path, host):
    p = tmp_path / "inventory.json"
    p.write_text(
        json.dumps(
            {
                "context_id": "lab",
                "devices": [
                    dict(
                        hostname=host,
                        host="10.0.0.1",
                        role="router",
                        site="HQ",
                        platform="ios",
                        mgmt_interface="Ethernet0/0",
                    )
                ],
            }
        )
    )
    with pytest.raises(fetch.CollectorError):
        fetch.load_inventory(p)
    with pytest.raises(fetch.CollectorError):
        fetch.save_outputs(tmp_path, {host: {"show running-config": "hostname R1\nend"}})


def test_ssh_strict_defaults(monkeypatch):
    pytest.importorskip("netmiko", reason="Requires optional SSH dependencies ([demo])")
    from netmiko.cisco import CiscoIosSSH

    options = {}
    monkeypatch.setattr(CiscoIosSSH, "__init__", lambda self, **kw: options.update(kw))
    device = fetch.InventoryDevice("R1", "10.0.0.1", "router", "HQ", "ios", "Ethernet0/0")
    fetch._connect(device, "test-user", "test-password")
    assert options["ssh_strict"] is True and options["system_host_keys"] is True
    assert options["session_log"] is None and device.legacy_ssh is False


def test_required_batfish_fails_closed(monkeypatch):
    monkeypatch.setenv("CHA_REQUIRE_BATFISH", "1")
    report = run_sensors({"SW": "end\n"})
    assert report.sensors["batfish"] == "skipped"
    assert report.blocking_sensor == "batfish"
    monkeypatch.delenv("CHA_REQUIRE_BATFISH")
    assert not run_sensors({"SW": "end\n"}).failures


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "10.0.0.0/8", "8.8.8.0/24", "224.0.0.0/24"])
def test_discovery_rejects_unbounded_or_public_scope(cidr):
    with pytest.raises(ValueError):
        discover(cidr)


def test_discovery_only_requested_hosts(monkeypatch):
    from harness_agent.collector import discover as module

    seen = []

    def probe(host):
        seen.append(host)
        return {"host": host}

    monkeypatch.setattr(module, "_probe", probe)
    assert len(discover("192.168.1.0/30")) == 2
    assert set(seen) == {"192.168.1.1", "192.168.1.2"}


def test_os_identification_does_not_guess_from_inventory():
    assert identify_platform("Cisco IOS Software, Linux Software") == "cisco_ios"
    assert identify_platform("Cisco IOS XE Software, Version 17") == "cisco_ios"
    assert identify_platform("Cisco Nexus Operating System (NX-OS)") == "cisco_nxos"
    assert identify_platform("SSH-2.0-OpenSSH") == "unknown"


def test_all_production_tools_and_extra_command_control():
    for tool, args in VALID_ARGS.items():
        call = parse_tool_call({"tool": tool, "args": args})
        configs = render_by_device([(tool, call.parsed)])
        assert check_intent(configs, [call]) == []
        altered = {dev: "aaa new-model\n" + text for dev, text in configs.items()}
        assert check_intent(altered, [call])


def test_apply_missing_staged_ops_is_blocked(tmp_path):
    from harness_agent.apply.plan import ApplyError, check_preapply
    from harness_agent.models import NetworkContext

    ctx = NetworkContext.model_validate_json(
        (Path(__file__).resolve().parents[1] / "data/contexts/campus-small-01.json").read_text()
    )
    with pytest.raises(ApplyError, match="staged"):
        check_preapply(tmp_path, "SW2", "vlan 30\n name FINANCE\nexit\nend\n", ctx)


def test_trunk_default_and_no_command_restore_all():
    for body in ("", " switchport trunk allowed vlan 10\n no switchport trunk allowed vlan\n"):
        data, _ = parse_running_config(
            "interface Ethernet0/0\n switchport mode trunk\n" + body, "access_switch"
        )
        assert data["interfaces"][0]["trunk_allowed"] == list(range(1, 4095))


def test_gate_blocks_incomplete_observations():
    from harness_agent.harness.context_store import ContextStore
    from harness_agent.harness.gate import evaluate

    ctx = json.loads((Path(__file__).resolve().parents[1] / "data/contexts/campus-small-01.json").read_text())
    call = parse_tool_call({"tool": "create_vlan", "args": VALID_ARGS["create_vlan"]})
    assert evaluate(call.model_dump(), "security_admin", ContextStore(ctx), [call], []).allowed
    device = next(d for d in ctx["devices"] if d["hostname"] == "SW2")
    device["collected_security"] = {"unsupported": ["vrf_forwarding"]}
    result = evaluate(call.model_dump(), "security_admin", ContextStore(ctx), [call], [])
    assert result.allowed  # unrelated VLAN creation is safe despite VRF observations
    from harness_agent.harness.observations import observation_problem

    assert observation_problem(device, "add_static_route", {}) is not None
    assert observation_problem(device, "set_interface_description", {}) is None
