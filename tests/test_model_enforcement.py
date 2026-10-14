"""A3: per-stage fail-closed pin checks, using offline fake_dsh + real MCP."""

import json
import sys
from pathlib import Path

import pytest
import zstandard

from harness_agent.runner import dsh_home, pipeline
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.model import ModelConfig, pin_problem
from harness_agent.runner.pipeline import ErrorCode

ROOT = Path(__file__).resolve().parents[1]
PIN = ModelConfig("deepseek-official", "deepseek-flash", "high", True)
ROW = {"provider": PIN.provider, "model": PIN.name, "reasoningEffort": PIN.reasoning_effort}
VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}


def log_file(home, events, name="one"):
    path = home / "sessions" / name / "session.v4.jsonl.zstd"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(event) + "\n" for event in events).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(body))
    return path


def header(config):
    return {"type": "request/header", "data": {"header": {"config": config}}}


@pytest.mark.parametrize("field", ["provider", "model", "reasoningEffort"])
def test_every_request_must_match_not_just_first_or_last(tmp_path, field):
    log_file(tmp_path, [header(ROW), header({**ROW, field: "other"}), header(ROW)])
    assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, set())) == "mismatch"


@pytest.mark.parametrize(
    "event",
    [
        [],
        {},
        {"type": "request/header"},
        header(None),
        header({}),
        header({**ROW, "model": 123}),
        header({**ROW, "model": "x" * 101}),
    ],
)
def test_malformed_or_missing_config_is_not_silently_dropped(tmp_path, event):
    log_file(tmp_path, [header(ROW), event])
    assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, set())) == "unverified"


def test_bad_json_and_bad_compression_fail_even_with_a_good_log(tmp_path):
    log_file(tmp_path, [header(ROW)])
    bad = log_file(tmp_path, [], "bad")
    for body in [b"not compressed", zstandard.ZstdCompressor().compress(b"{broken\n")]:
        bad.write_bytes(body)
        assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, set())) == "unverified"


def test_escaped_header_name_is_parsed(tmp_path):
    path = log_file(tmp_path, [])
    text = json.dumps(header({**ROW, "model": "other"})).replace("request/header", "request\\u002fheader")
    path.write_bytes(zstandard.ZstdCompressor().compress(text.encode()))
    assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, set())) == "mismatch"


def test_retained_or_rewritten_old_session_cannot_supply_new_stage_evidence(tmp_path):
    log_file(tmp_path, [header(ROW)])
    before = dsh_home.session_paths(tmp_path)
    log_file(tmp_path, [header(ROW), header(ROW)])
    assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, before)) == "unverified"
    log_file(tmp_path, [header(ROW)], "new")
    assert pin_problem(PIN, dsh_home.request_evidence(tmp_path, before)) is None


def run(
    tmp_path,
    monkeypatch,
    *,
    stage="cba",
    attack=None,
    disabled=False,
    level="B4",
    keep=False,
    prepare=False,
    outcome="ready",
    effort="high",
):
    config_path = tmp_path / "model.toml"
    config_path.write_text(
        '[model]\nprovider="deepseek-official"\nname="deepseek-flash"\n'
        f'reasoning_effort="{effort}"\nthinking={str(effort != "off").lower()}\n'
    )
    scenario = {
        "cba": [
            {
                "tool": "submit_change_request",
                "args": {
                    "status": outcome,
                    "changes": [VLAN] if outcome == "ready" else [],
                    "clarification_question": "Which VLAN?" if outcome != "ready" else None,
                },
            }
        ],
        "tsa": "auto",
        "b0": "=== SW2 ===\nvlan 30\n name FINANCE\nexit\nend\n",
    }
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario_path))
    actual = pipeline.run_dsh

    def tamper(cfg, patch, prompt, prefix, *args):
        result = actual(cfg, patch, prompt, prefix, *args)
        if prefix.name == stage and attack:
            logs = list(cfg.dsh_home.rglob("*.jsonl.zstd"))
            # Fake runtime uses a unique stage suffix; do not mutate retained logs.
            current = next(p for p in logs if f"session-fake-{stage}" in p.parts)
            events = [json.loads(line) for line in dsh_home.read_file(current).splitlines()]
            if attack == "missing":
                events = [e for e in events if e.get("type") != "request/header"]
            elif attack == "config_file":
                config_path.write_text(config_path.read_text().replace("deepseek-flash", "other"))
            elif attack == "exit":
                events = []
                result.exit_code = 7
            else:
                for event in events:
                    if event.get("type") == "request/header":
                        event["data"]["header"]["config"][attack] = "other"
            current.write_bytes(
                zstandard.ZstdCompressor().compress("".join(json.dumps(e) + "\n" for e in events).encode())
            )
        return result

    monkeypatch.setattr(pipeline, "run_dsh", tamper)
    if disabled:
        monkeypatch.setattr(pipeline, "pin_problem", lambda *args: None)
    cfg = LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts/fake_dsh.py")],
        dsh_home=tmp_path / "home",
        base_patch=ROOT / "tests/fixtures/dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
        model_file=config_path,
        keep_dsh_home=keep,
    )
    return pipeline.run_case(
        cfg,
        ROOT / "data/contexts/campus-small-01.json",
        "Create VLAN 30 FINANCE",
        "network_admin",
        tmp_path / "runs",
        "case",
        level,
        prepare_only=prepare,
    )


def no_output(result):
    assert result.status == "error"
    assert result.verdict is None and result.stopped_by is None
    folder = Path(result.run_dir)
    assert not (folder / "candidate").exists()
    assert not (folder / "change_package.md").exists()
    assert not (folder / "review.json").exists()


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["cba", "tsa"])
@pytest.mark.parametrize("attack", ["provider", "model", "reasoningEffort", "missing"])
@pytest.mark.parametrize("disabled", [False, True])
def test_stage_pin_and_negative_control(tmp_path, monkeypatch, stage, attack, disabled):
    r = run(tmp_path, monkeypatch, stage=stage, attack=attack, disabled=disabled, keep=True)
    if disabled:
        assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    else:
        no_output(r)
        expected = ErrorCode.MODEL_PIN_UNVERIFIED if attack == "missing" else ErrorCode.MODEL_PIN_MISMATCH
        assert r.error_code == expected, r.reason
        assert stage in r.reason
        if stage == "cba":
            assert not (Path(r.run_dir) / "tsa.ndjson").exists()


@pytest.mark.slow
@pytest.mark.parametrize("effort", ["off", "low", "high", "max"])
def test_matching_pin_and_thinking_modes(tmp_path, monkeypatch, effort):
    r = run(tmp_path, monkeypatch, effort=effort)
    assert (r.status, r.verdict, r.reasoning_effort) == ("ok", "accept", effort), r.reason


@pytest.mark.slow
def test_pin_snapshot_survives_model_file_change_between_stages(tmp_path, monkeypatch):
    r = run(tmp_path, monkeypatch, attack="config_file")
    assert (r.status, r.verdict, r.model) == ("ok", "accept", PIN.name), r.reason
    assert "model: deepseek-flash" in (Path(r.run_dir) / "tsa.patch.yml").read_text(encoding="utf-8")


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_runtime_error_keeps_its_code_when_evidence_missing(tmp_path, monkeypatch, stage):
    r = run(tmp_path, monkeypatch, stage=stage, attack="exit")
    no_output(r)
    assert r.error_code == ErrorCode.DSH_EXIT


def test_preflight_error_does_not_become_model_error(tmp_path, monkeypatch):
    def fail(*args):
        raise DshSetupError("offline preflight failure")

    monkeypatch.setattr(pipeline, "preflight", fail)
    r = run(tmp_path, monkeypatch)
    no_output(r)
    assert r.error_code == ErrorCode.PREFLIGHT_FAILED


@pytest.mark.slow
def test_bad_pin_cannot_become_pending_review(tmp_path, monkeypatch):
    r = run(tmp_path, monkeypatch, attack="model", prepare=True)
    no_output(r)
    assert r.error_code == ErrorCode.MODEL_PIN_MISMATCH


@pytest.mark.slow
def test_bad_pin_cannot_become_clarify(tmp_path, monkeypatch):
    r = run(tmp_path, monkeypatch, attack="model", outcome="needs_clarification")
    no_output(r)
    assert r.error_code == ErrorCode.MODEL_PIN_MISMATCH


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B0", "B1", "B2", "B3"])
def test_pin_check_is_fixed_across_ablation_levels(tmp_path, monkeypatch, level):
    r = run(tmp_path, monkeypatch, level=level, stage="b0" if level == "B0" else "tsa", attack="model")
    no_output(r)
    assert r.error_code == ErrorCode.MODEL_PIN_MISMATCH
