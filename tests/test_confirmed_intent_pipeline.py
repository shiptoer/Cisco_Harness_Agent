"""Confirmed intent integration tests. Uses fake_dsh, no model/API."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from harness_agent.runner import pipeline
from harness_agent.runner.dsh import LocalConfig

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data/contexts/campus-small-01.json"

INTENT = "Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 của SW2 vào VLAN 30"

CONFIRMED_CHANGES = [
    {
        "tool": "create_vlan",
        "args": {
            "device": "SW2",
            "vlan_id": 30,
            "name": "FINANCE",
        },
    },
    {
        "tool": "set_access_vlan",
        "args": {
            "device": "SW2",
            "interface": "GigabitEthernet1/1",
            "vlan_id": 30,
        },
    },
]


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )


# Intent grounding (D-061) chạy TRƯỚC confirmed intent và cưỡng chế từ B3: tên "OTHER" không có trong intent
# nên ở B3 đề xuất `wrong_name` dừng ở grounding (clarify), confirmed intent không được kiểm. Ở B2 grounding
# chỉ shadow, nên chính confirmed intent bắt đề xuất đó.
GROUNDING_FIRST = {("wrong_name", "B3")}


@pytest.mark.slow
@pytest.mark.parametrize(
    ("variant", "level"),
    [
        ("match", "B3"),
        ("wrong_name", "B2"),
        ("wrong_name", "B3"),
        ("missing_operation", "B3"),
        ("reordered", "B3"),
    ],
)
def test_confirmed_proposal_controls_tsa(tmp_path, monkeypatch, variant, level):
    cfg = LocalConfig(
        dsh_command=[
            sys.executable,
            str(ROOT / "scripts/fake_dsh.py"),
        ],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests/fixtures/dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )

    # Work on an isolated copy of the repository's small context.
    context_path = tmp_path / "context.json"
    context_path.write_bytes(CAMPUS.read_bytes())

    contract_path = tmp_path / "confirmed.json"
    write_json(
        contract_path,
        {
            "intent": INTENT,
            "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
            "changes": CONFIRMED_CHANGES,
        },
    )

    proposed = copy.deepcopy(CONFIRMED_CHANGES)
    if variant == "wrong_name":
        proposed[0]["args"]["name"] = "OTHER"
    elif variant == "missing_operation":
        proposed = proposed[:1]
    elif variant == "reordered":
        proposed.reverse()

    scenario_path = tmp_path / "scenario.json"
    write_json(
        scenario_path,
        {
            "cba": [
                {
                    "tool": "submit_change_request",
                    "args": {
                        "status": "ready",
                        "changes": proposed,
                    },
                }
            ],
            "tsa": "auto",
        },
    )
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario_path))

    real_run_stage = pipeline._run_stage
    attempted_stages = []

    def watched_stage(cfg, stage, ctx, role, run_dir, prompt, trace, *extra):
        attempted_stages.append(stage)
        if stage == "tsa" and variant != "match":
            pytest.fail("TSA must not run for a mismatched proposal")
        return real_run_stage(cfg, stage, ctx, role, run_dir, prompt, trace, *extra)

    monkeypatch.setattr(pipeline, "_run_stage", watched_stage)

    result = pipeline.run_case(
        cfg,
        context_path,
        INTENT,
        "network_admin",
        tmp_path / "runs",
        variant,
        level,
        confirmed_intent_path=contract_path,
    )
    run_dir = tmp_path / "runs" / variant

    assert result.status == "ok", result.reason
    assert result.error_code is None

    events = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    contract_checks = [
        event["data"]["matched"] for event in events if event["event"] == "intent_contract_check"
    ]
    assert contract_checks == ([] if (variant, level) in GROUNDING_FIRST else [variant == "match"])

    if variant == "match":
        assert attempted_stages == ["cba", "tsa"]
        assert result.verdict == "accept", result.reason
        assert result.stopped_by is None
        assert (run_dir / "change_package.md").is_file()

        candidate = run_dir / "candidate"
        assert sorted(p.name for p in candidate.iterdir()) == ["SW2.cfg"]
        assert (candidate / "SW2.cfg").read_text(encoding="utf-8") == (
            "vlan 30\n"
            " name FINANCE\nexit\n"
            "interface GigabitEthernet1/1\n"
            " switchport mode access\n"
            " switchport access vlan 30\nexit\n"
            "end\n"
        )
    else:
        assert attempted_stages == ["cba"]
        if (variant, level) in GROUNDING_FIRST:
            assert (result.verdict, result.stopped_by) == ("clarify", "intent_grounding"), result.reason
        else:
            assert result.verdict == "reject", result.reason
            assert result.stopped_by == "confirmed_intent"
        assert "tsa" not in result.tool_calls

        for name in (
            "staged_ops.jsonl",
            "finalized.json",
            "candidate",
            "change_package.md",
        ):
            assert not (run_dir / name).exists(), name
