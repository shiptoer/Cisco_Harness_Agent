"""Real runner/MCP integration with fake_dsh; no model API or device deployment."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness_agent.levels import Level
from harness_agent.runner.cases import scenario_path
from harness_agent.web.reviews import ReviewService

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "data/cases/clean-vlan30.json"
# Runner D tìm kịch bản `fake_dsh` theo tên file case (D-045), không qua khoá `fake_scenario`.
SCENARIO = scenario_path(CASE, Level.B4)


@pytest.mark.slow
@pytest.mark.parametrize("role,verdict", [("network_admin", "accept"), ("access_operator", "reject")])
def test_fake_pipeline_preserves_contract_and_rbac(tmp_path, monkeypatch, role, verdict):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    case = json.loads(CASE.read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(SCENARIO))
    service = ReviewService(ROOT / "config/fake.toml", ROOT / case["context"], role, tmp_path)
    result = service.prepare(case["intent"])
    assert result["state"] == "pending", result
    assert not list(tmp_path.rglob("change_package.md"))
    request_id = result["request_id"]
    record = service.records[request_id]
    _, csrf = service.browser_review(request_id, record["view_token"])
    service.browser_decide(request_id, csrf, "approve")
    result = service.generate(request_id)
    execution = result["execution"]
    assert execution["status"] == "ok", execution
    assert execution["verdict"] == verdict, execution
    if verdict == "accept":
        assert "vlan 30" in execution["change_package"]
    else:
        assert execution["stopped_by"] == "G3_rbac"
        assert "change_package" not in execution


@pytest.mark.slow
def test_changed_model_proposal_is_rejected_after_approval(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    case = json.loads(CASE.read_text(encoding="utf-8"))
    scenario = SCENARIO
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    service = ReviewService(ROOT / "config/fake.toml", ROOT / case["context"], "network_admin", tmp_path)
    result = service.prepare(case["intent"])
    request_id = result["request_id"]
    _, csrf = service.browser_review(request_id, service.records[request_id]["view_token"])
    service.browser_decide(request_id, csrf, "approve")
    changed = scenario.read_text(encoding="utf-8").replace("FINANCE", "OTHER")
    altered = tmp_path / "different-scenario.json"
    altered.write_text(changed, encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(altered))
    execution = service.generate(request_id)["execution"]
    assert execution["verdict"] != "accept", execution
    assert "change_package" not in execution
    assert "tsa" not in execution["tool_calls"]


@pytest.mark.slow
def test_stdio_bridge_lists_only_workflow_tools_and_blocks_spoofed_approval(tmp_path):
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "scripts/web_multi_server.py")],
            env={
                "NETWORK_CONFIG": str(ROOT / "config/fake.toml"),
                "NETWORK_CONTEXT": str(ROOT / "data/contexts/campus-small-01.json"),
                "NETWORK_ROLE": "network_admin",
                "CHA_WEB_OUT": str(tmp_path / "runs"),  # chỉ ghi vào tmp_path, không vào runs/ của repo
                "PYTHONUTF8": "1",
            },
        )
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == {
                "prepare_network_request",
                "get_network_request",
                "generate_approved_request",
            }
            for name, args in [
                ("generate_approved_request", {"request_id": "0" * 32}),
                ("prepare_network_request", {"intent": "VLAN 30", "approved": True}),
                ("approve_request", {"request_id": "0" * 32}),
            ]:
                result = await session.call_tool(name, args)
                assert json.loads(result.content[0].text)["status"] == "blocked"

    asyncio.run(check())
    assert (tmp_path / "runs").is_dir()
