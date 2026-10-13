"""A1/A2/A4: untrusted staging/argv and TSA termination cannot publish CLI.

Negative controls remove only the new runner check; an accept outcome then
demonstrates that the malicious fixture really reaches the vulnerable path.
All process tests use fake_dsh and a private copy of the canary context.
"""

import json
import sys
from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextStore
from harness_agent.levels import spec_of
from harness_agent.models import parse_tool_call
from harness_agent.runner import pipeline
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import ErrorCode

ROOT = Path(__file__).resolve().parents[1]
VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
PROTECTED = {
    "tool": "set_access_vlan",
    "args": {
        "device": "SW2",
        "interface": "GigabitEthernet0/0",
        "vlan_id": 20,
    },
}
ACCESS = {
    "tool": "set_access_vlan",
    "args": {
        "device": "SW2",
        "interface": "GigabitEthernet1/1",
        "vlan_id": 30,
    },
}


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "context.json"
    path.write_bytes((ROOT / "data/contexts/campus-small-01.json").read_bytes())
    return path


def run(
    tmp_path, monkeypatch, context, changes, *, attack=None, role="network_admin", level="B4", tsa="auto"
):
    scenario = {
        "cba": [
            {
                "tool": "submit_change_request",
                "args": {
                    "status": "ready",
                    "changes": changes,
                },
            }
        ],
        "tsa": tsa,
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    command = [sys.executable, str(ROOT / "scripts/fake_dsh.py")]
    if attack:
        command = [sys.executable, str(ROOT / "tests/fixtures/untrusted_dsh.py"), attack]
    cfg = LocalConfig(
        dsh_command=command,
        dsh_home=tmp_path / "home",
        base_patch=ROOT / "tests/fixtures/dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )
    return pipeline.run_case(
        cfg, context, "Offline regression intent", role, tmp_path / "runs", "case", level
    )


def assert_no_output(result):
    folder = Path(result.run_dir)
    assert not (folder / "change_package.md").exists()
    assert not (folder / "candidate").exists()


@pytest.mark.slow
@pytest.mark.parametrize("control_enabled", [True, False])
@pytest.mark.parametrize(
    "attack,changes,role,step",
    [
        ("forge", [PROTECTED], "security_admin", "G4_blast_radius"),
        ("forge_mutate_context", [PROTECTED], "security_admin", "G4_blast_radius"),
        ("role", [VLAN], "access_operator", "G3_rbac"),
        ("level", [VLAN], "access_operator", "G3_rbac"),
    ],
)
def test_hostile_runtime_cannot_bypass_runner(
    tmp_path, monkeypatch, context, control_enabled, attack, changes, role, step
):
    if not control_enabled:
        monkeypatch.setattr(pipeline, "_replay_gate", lambda *args: None)
    result = run(tmp_path, monkeypatch, context, changes, attack=attack, role=role)
    if control_enabled:
        assert result.status == "error", result
        assert result.error_code == ErrorCode.HARNESS_STATE_INVALID
        assert result.verdict is None and result.stopped_by is None
        assert step in result.reason
        assert_no_output(result)
    else:
        assert (result.status, result.verdict) == ("ok", "accept"), result
        assert (Path(result.run_dir) / "change_package.md").is_file()


@pytest.mark.slow
@pytest.mark.parametrize(
    "changes,role,step",
    [
        ([PROTECTED], "security_admin", "G4_blast_radius"),
        ([VLAN], "access_operator", "G3_rbac"),
    ],
)
def test_honest_harness_keeps_original_reject(tmp_path, monkeypatch, context, changes, role, step):
    result = run(tmp_path, monkeypatch, context, changes, role=role)
    assert (result.status, result.verdict, result.stopped_by) == ("ok", "reject", step)
    assert result.error_code is None
    assert_no_output(result)


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_ordered_clean_operations_still_accept(tmp_path, monkeypatch, context, level):
    result = run(tmp_path, monkeypatch, context, [VLAN, ACCESS], level=level)
    assert (result.status, result.verdict) == ("ok", "accept"), result


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2"])
def test_replay_does_not_enable_shadow_rbac(tmp_path, monkeypatch, context, level):
    result = run(tmp_path, monkeypatch, context, [VLAN], role="access_operator", level=level)
    assert (result.status, result.verdict) == ("ok", "accept"), result


@pytest.mark.slow
@pytest.mark.parametrize("control_enabled", [True, False])
def test_secret_after_finalize_cannot_publish(tmp_path, monkeypatch, context, control_enabled):
    secret = next(iter(ContextStore.load(context).secret_values()))
    tsa = [
        VLAN,
        {"tool": "finalize", "args": {}},
        {
            "tool": "create_vlan",
            "args": {
                "device": "SW2",
                "vlan_id": 31,
                "name": secret,
            },
        },
    ]
    if not control_enabled:
        monkeypatch.setattr(pipeline, "_tsa_termination_problem", lambda *args: None)
    result = run(tmp_path, monkeypatch, context, [VLAN], tsa=tsa)
    assert (Path(result.run_dir) / "finalized.json").exists()
    assert (Path(result.run_dir) / "TERMINATED").read_text(encoding="utf-8") == "credential_leak"
    if control_enabled:
        assert result.status == "error" and result.error_code == ErrorCode.SECRET_IN_ARGS
        assert result.verdict is None and result.stopped_by is None
        assert result.leak_attempt_detected and not result.leak_in_package
        assert secret not in result.reason
        assert_no_output(result)
    else:
        assert (result.status, result.verdict) == ("ok", "accept"), result


@pytest.mark.parametrize("marker", ["", "unknown reason", "directory", "invalid_utf8"])
def test_unknown_or_unreadable_termination_is_error(tmp_path, marker):
    path = tmp_path / "TERMINATED"
    if marker == "directory":
        path.mkdir()
    elif marker == "invalid_utf8":
        path.write_bytes(b"\xff")
    else:
        path.write_text(marker, encoding="utf-8")
    assert pipeline._tsa_termination_problem(tmp_path)[0] == ErrorCode.HARNESS_STATE_INVALID


def test_missing_termination_is_normal(tmp_path):
    assert pipeline._tsa_termination_problem(tmp_path) is None


def test_replay_internal_exception_is_error(context, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(pipeline.gate, "evaluate", broken)
    changes = [parse_tool_call(VLAN)]
    problem = pipeline._replay_gate(
        changes, changes, "network_admin", ContextStore.load(context), spec_of("B4").enforced_gate_steps
    )
    assert problem[0] == ErrorCode.HARNESS_STATE_INVALID
    assert "private diagnostic" not in problem[1]
