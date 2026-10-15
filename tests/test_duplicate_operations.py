"""A8: duplicate proposals reject before review/TSA, not STAGED_MISMATCH."""

import json
from pathlib import Path

import pytest
from test_levels import CAMPUS, VLAN, _cfg, _submit

from harness_agent.harness import server
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.proposal import DUPLICATE_REASON, has_duplicate_operations
from harness_agent.models import ChangeRequest
from harness_agent.runner import pipeline


def proposal(*changes):
    return ChangeRequest.model_validate(_submit(*changes)["args"])


@pytest.mark.parametrize("variant", ["exact", "key_order", "normalized", "nonadjacent"])
def test_duplicate_equality(variant):
    other = {"tool": VLAN["tool"], "args": dict(reversed(list(VLAN["args"].items())))}
    if variant == "normalized":
        other["args"]["vlan_id"] = "30"
    distinct = {"tool": VLAN["tool"], "args": {**VLAN["args"], "name": "OTHER"}}
    changes = [VLAN, distinct, other] if variant == "nonadjacent" else [VLAN, other]
    assert has_duplicate_operations(proposal(*changes))
    assert not has_duplicate_operations(proposal(VLAN, distinct))


@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_submit_rejects_without_artifact_and_is_terminal(tmp_path, level):
    state = server.RunState(tmp_path, ContextStore.load(CAMPUS), "network_admin", "cba", level)
    result = server.handle_call(state, "submit_change_request", _submit(VLAN, VLAN)["args"])
    assert result["status"] == "rejected" and result["terminal"] is True
    assert result["reason"] == DUPLICATE_REASON
    assert not state.cr_path.exists()
    with pytest.raises(server.ToolFailure, match="terminated"):
        server.handle_call(state, "submit_change_request", _submit(VLAN)["args"])


def test_negative_control_submit_accepts_when_check_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "has_duplicate_operations", lambda cr: False)
    state = server.RunState(tmp_path, ContextStore.load(CAMPUS), "network_admin", "cba")
    assert server.handle_call(state, "submit_change_request", _submit(VLAN, VLAN)["args"])["accepted"]
    assert len(state.approved_changes()) == 2


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_real_mcp_rejects_duplicate_proposal(tmp_path, monkeypatch, level):
    result = run(tmp_path, monkeypatch, [VLAN, VLAN], level)
    assert_rejected(result)
    assert not (Path(result.run_dir) / "change_request.json").exists()


def run(tmp_path, monkeypatch, changes, level="B4", prepare=False):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"cba": [_submit(*changes)], "tsa": "auto"}), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    return pipeline.run_case(
        _cfg(tmp_path),
        CAMPUS,
        "Create VLAN 30 FINANCE on SW2",
        "network_admin",
        tmp_path / "runs",
        "case",
        level=level,
        prepare_only=prepare,
    )


def assert_rejected(result):
    assert (result.status, result.verdict, result.error_code) == ("ok", "reject", None)
    assert result.stopped_by == "change_request_validation"
    assert result.reason == DUPLICATE_REASON
    folder = Path(result.run_dir)
    for name in ["tsa.prompt.txt", "review.json", "candidate", "change_package.md"]:
        assert not (folder / name).exists(), name


@pytest.mark.slow
@pytest.mark.parametrize("prepare", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_runner_rechecks_forged_proposal_with_negative_control(tmp_path, monkeypatch, prepare, enabled):
    original = pipeline._run_stage

    def forge(cfg, stage, ctx, role, run_dir, prompt, trace, *rest):
        result = original(cfg, stage, ctx, role, run_dir, prompt, trace, *rest)
        if stage == "cba":
            (run_dir / "change_request.json").write_text(
                proposal(VLAN, VLAN).model_dump_json(), encoding="utf-8"
            )
        return result

    monkeypatch.setattr(pipeline, "_run_stage", forge)
    if not enabled:
        monkeypatch.setattr(pipeline, "has_duplicate_operations", lambda cr: False)
    result = run(tmp_path, monkeypatch, [VLAN], prepare=prepare)
    if enabled:
        assert_rejected(result)
    elif prepare:
        assert result.status == "pending_review"
    else:
        assert result.status == "error" and result.error_code == "STAGED_MISMATCH"


@pytest.mark.slow
def test_repeated_tsa_call_remains_idempotent(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"cba": [_submit(VLAN)], "tsa": [VLAN, VLAN, {"tool": "finalize", "args": {}}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    result = pipeline.run_case(
        _cfg(tmp_path), CAMPUS, "Create VLAN", "network_admin", tmp_path / "runs", "case"
    )
    assert (result.status, result.verdict) == ("ok", "accept")
    assert len((Path(result.run_dir) / "staged_ops.jsonl").read_text(encoding="utf-8").splitlines()) == 1
