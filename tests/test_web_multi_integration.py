"""Real runner/MCP integration with fake_dsh; no model API or device deployment."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness_agent.web.reviews import ReviewService

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.slow
@pytest.mark.parametrize("role,verdict", [("network_admin", "accept"), ("access_operator", "reject")])
def test_fake_pipeline_preserves_contract_and_rbac(tmp_path, monkeypatch, role, verdict):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    case = json.loads((ROOT / "data/cases/clean-vlan30.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(ROOT / case["fake_scenario"]))
    service = ReviewService(ROOT / "config/fake.toml", ROOT / case["context"], role, tmp_path)
    result = service.prepare(case["intent"])
    assert result["state"] == "pending", result
    assert not list(tmp_path.rglob("change_package.md"))
    request_id = result["request_id"]
    _, csrf = service.browser_review(request_id)
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
    case = json.loads((ROOT / "data/cases/clean-vlan30.json").read_text(encoding="utf-8"))
    scenario = ROOT / case["fake_scenario"]
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    service = ReviewService(ROOT / "config/fake.toml", ROOT / case["context"], "network_admin", tmp_path)
    result = service.prepare(case["intent"])
    request_id = result["request_id"]
    _, csrf = service.browser_review(request_id)
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
    # Test-only wrapper: do not open a real desktop browser in offline tests.
    wrapper = tmp_path / "bridge_test.py"
    wrapper.write_text(
        "import runpy\n"
        "from pathlib import Path\n"
        "import harness_agent.web.reviews as reviews\n"
        "real_service = reviews.ReviewService\n"
        "reviews.ReviewService = lambda config, context, role, out: "
        f"real_service(config, context, role, Path({str(tmp_path / 'runs')!r}))\n"
        "import harness_agent.web.open_browser as opener\n"
        "opener.open_operator_page = lambda url: False\n"
        f"runpy.run_path({str(ROOT / 'scripts/web_multi_server.py')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )

    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(wrapper)],
            env={
                "NETWORK_CONFIG": str(ROOT / "config/fake.toml"),
                "NETWORK_CONTEXT": str(ROOT / "data/contexts/campus-small-01.json"),
                "NETWORK_ROLE": "network_admin",
                "PYTHONUTF8": "1",
                "FAKE_DSH_SCENARIO": str(
                    ROOT
                    / json.loads((ROOT / "data/cases/clean-vlan30.json").read_text(encoding="utf-8"))[
                        "fake_scenario"
                    ]
                ),
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
            case = json.loads((ROOT / "data/cases/clean-vlan30.json").read_text(encoding="utf-8"))
            prepared = await session.call_tool("prepare_network_request", {"intent": case["intent"]})
            payload = json.loads(prepared.content[0].text)
            assert payload["state"] == "pending", payload
            request_id = payload["request_id"]
            status = await session.call_tool("get_network_request", {"request_id": request_id})
            for response in [prepared, status]:
                assert "review_url" not in response.content[0].text
                assert "token=" not in response.content[0].text
                assert "csrf" not in response.content[0].text
            blocked = await session.call_tool("generate_approved_request", {"request_id": request_id})
            assert json.loads(blocked.content[0].text)["status"] == "blocked"
            for name, args in [
                ("generate_approved_request", {"request_id": "0" * 32}),
                ("prepare_network_request", {"intent": "VLAN 30", "approved": True}),
                ("approve_request", {"request_id": "0" * 32}),
            ]:
                result = await session.call_tool(name, args)
                assert json.loads(result.content[0].text)["status"] == "blocked"

    asyncio.run(check())
