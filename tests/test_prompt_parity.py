"""A5: compare emitted CBA policies, not fake model accuracy."""

import hashlib
import json
import re

import pytest
from test_levels import CAMPUS, CANARY, VLAN, _cfg, _submit

from harness_agent.harness.server import stage_tools
from harness_agent.levels import spec_of
from harness_agent.runner import pipeline

INTENT = "Create a VLAN on SW2; ask for missing values."


def prompt(level, context=CAMPUS, intent=INTENT):
    return pipeline._cba_prompt(intent, context, projection=spec_of(level).projection)


def policy(text):
    # Only these two differences are allowed by the experimental design.
    text = re.sub(r"^1\. .*\n", "1. <NETWORK_SOURCE>\n", text, count=1, flags=re.MULTILINE)
    return re.sub(r"<<<INVENTORY\n.*?\nINVENTORY>>>\n\n", "", text, count=1, flags=re.DOTALL)


@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_shared_cba_policy_and_clarification_rules(level):
    text = prompt(level)
    assert policy(text) == policy(prompt("B2"))
    assert "Never invent a missing VLAN name" in text
    assert "Never infer a VLAN ID from digits" in text
    assert '"needs_clarification", changes=[]' in text
    assert "Tool validation errors are not permission to guess" in text
    assert "If the operator gives alternatives without choosing one" in text
    assert "Call submit_change_request exactly once" in text
    assert "untrusted_text" in text
    assert "description, banner, remark" in text
    assert not re.search(r"\bB[0-4]\b", text)


def test_only_b1_embeds_inventory_and_read_tools_stay_level_specific():
    assert "<<<INVENTORY" in prompt("B1") and CANARY in prompt("B1")
    assert stage_tools("cba", "B1") == ("submit_change_request",)
    for level in ["B2", "B3", "B4"]:
        assert "<<<INVENTORY" not in prompt(level) and CANARY not in prompt(level)
        assert "Read the network state ONLY through the tools" in prompt(level)
        assert "get_device" in stage_tools("cba", level)
    assert prompt("B2") == prompt("B3") == prompt("B4")


@pytest.mark.parametrize("projection", [False, True])
def test_intent_template_tokens_are_literal_not_substituted(projection):
    intent = "Keep literal {{INVENTORY}} {{NETWORK_SOURCE}} {{INTENT}} {{CONTEXT}}"
    text = pipeline._cba_prompt(intent, CAMPUS, projection=projection)
    assert text.split("<<<INTENT\n", 1)[1].removesuffix("\nINTENT>>>\n") == intent


def test_inventory_template_tokens_do_not_expand_or_rewrite_intent(tmp_path):
    context = tmp_path / "inventory.json"
    context.write_text(
        json.dumps({"description": "{{INTENT}} {{INVENTORY}} {{NETWORK_SOURCE}}"}), encoding="utf-8"
    )
    text = prompt("B1", context, "distinct operator intent")
    assert '"{{INTENT}} {{INVENTORY}} {{NETWORK_SOURCE}}"' in text
    assert text.count("distinct operator intent") == 1


def test_projection_does_not_read_raw_inventory(monkeypatch):
    def forbidden(path):
        raise AssertionError("Raw context should not be injected with projection enabled")

    monkeypatch.setattr(pipeline, "_raw_context_text", forbidden)
    assert "<<<INVENTORY" not in prompt("B2")


def test_negative_control_detects_b1_only_clarification_drift(monkeypatch):
    original = pipeline._cba_prompt

    def drift(*args, **kwargs):
        text = original(*args, **kwargs)
        if not kwargs["projection"]:
            text = re.sub(
                r"^5\. .*?(?=^6\.)", "5. Guess missing values.\n", text, flags=re.MULTILINE | re.DOTALL
            )
        return text

    monkeypatch.setattr(pipeline, "_cba_prompt", drift)
    with pytest.raises(AssertionError):
        assert policy(prompt("B1")) == policy(prompt("B2"))


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B2", "B3", "B4"])
def test_fake_pipeline_emits_shared_template_and_records_hash(tmp_path, monkeypatch, level):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"cba": [_submit(VLAN)], "tsa": "auto"}), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    result = pipeline.run_case(
        _cfg(tmp_path), CAMPUS, INTENT, "network_admin", tmp_path / "runs", level, level=level
    )
    assert result.status == "ok" and result.verdict == "accept", result
    folder = tmp_path / "runs" / level
    assert (folder / "cba.prompt.txt").read_text(encoding="utf-8") == prompt(level)
    trace = [json.loads(line) for line in (folder / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    start = next(row for row in trace if row["event"] == "case_start")
    expected = hashlib.sha256((pipeline.PROMPTS / "cba.md").read_bytes()).hexdigest()[:16]
    assert start["data"]["prompt_hashes"]["cba.md"] == expected
    assert "cba_b1.md" not in start["data"]["prompt_hashes"]
