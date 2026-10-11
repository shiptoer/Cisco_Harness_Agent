"""Offline approval-boundary tests; model calls are replaced by a recording runner."""

import http.client
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

from harness_agent.runner.pipeline import CaseResult
from harness_agent.web.approval_http import start_approval_server
from harness_agent.web.reviews import ReviewError, ReviewService, digest, write_json

ROOT = Path(__file__).resolve().parents[1]
CHANGES = [
    {
        "tool": "set_interface_description",
        "args": {
            "device": "SW2",
            "interface": "GigabitEthernet1/2",
            "text": "SALES-PC-03",
        },
    }
]


@pytest.fixture
def service(tmp_path):
    context = tmp_path / "context.json"
    context.write_bytes((ROOT / "data/contexts/campus-small-01.json").read_bytes())
    config = tmp_path / "runner.toml"
    config.write_bytes((ROOT / "config/fake.toml").read_bytes())
    calls = []

    def runner(cfg, ctx, intent, role, out, run_id, **kwargs):
        calls.append(kwargs)
        folder = out / run_id
        folder.mkdir()
        if kwargs.get("prepare_only"):
            write_json(folder / "change_request.json", {"status": "ready", "changes": CHANGES})
            write_json(
                folder / "review.json",
                {
                    "intent": intent,
                    "role": role,
                    "context_sha256": digest(ctx),
                    "proposal_sha256": digest(folder / "change_request.json"),
                },
            )
            return CaseResult(run_id, "pending_review", None)
        contract = json.loads(kwargs["confirmed_intent_path"].read_text(encoding="utf-8"))
        assert contract == {"intent": intent, "context_sha256": digest(ctx), "changes": CHANGES}
        (folder / "change_package.md").write_text("Approved test package", encoding="utf-8")
        return CaseResult(run_id, "ok", "accept")

    instance = ReviewService(config, context, "network_admin", tmp_path / "runs", runner=runner)
    # A private model copy lets mutation tests avoid changing repository config.
    model = tmp_path / "model.toml"
    model.write_bytes(instance.model_file.read_bytes())
    instance.model_file = model
    instance.calls = calls
    return instance


def decide(service, request_id, decision="approve"):
    token = service.records[request_id]["view_token"]
    _, csrf = service.browser_review(request_id, token)
    service.browser_decide(request_id, csrf, decision)


def test_separate_requests_need_separate_approval(service):
    a = service.prepare("First intent")["request_id"]
    b = service.prepare("Second intent")["request_id"]
    assert a != b
    assert not list(service.out_root.rglob("change_package.md"))
    with pytest.raises(ReviewError, match="approval"):
        service.generate(a)
    decide(service, a)
    assert service.status(b)["state"] == "pending"
    assert len(service.calls) == 2  # Approval itself never calls the model.
    result = service.generate(a)
    assert result["execution"]["change_package"] == "Approved test package"
    assert service.generate(a) == result
    assert len(service.calls) == 3  # Completed requests are idempotent.
    assert "csrf" not in json.dumps(result)


def test_reject_never_generates(service):
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id, "reject")
    with pytest.raises(ReviewError):
        service.generate(request_id)
    assert len(service.calls) == 1


@pytest.mark.parametrize("target", ["context", "config", "model", "proposal", "contract"])
def test_modified_inputs_cannot_use_approval(service, target):
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)
    paths = {
        "context": service.context,
        "config": service.config,
        "model": service.model_file,
        "proposal": service.records[request_id]["proposal_path"],
        "contract": service.records[request_id]["contract_path"],
    }
    path = paths[target]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ReviewError, match="changed"):
        service.generate(request_id)
    assert len(service.calls) == 1


def test_approval_token_is_request_bound_and_single_use(service):
    a = service.prepare("First")["request_id"]
    b = service.prepare("Second")["request_id"]
    _, csrf = service.browser_review(a, service.records[a]["view_token"])
    with pytest.raises(ReviewError):
        service.browser_decide(b, csrf, "approve")
    service.browser_decide(a, csrf, "approve")
    with pytest.raises(ReviewError):
        service.browser_decide(a, csrf, "reject")


def test_exception_consumes_approval_and_does_not_retry(service):
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)

    def broken(*args, **kwargs):
        raise RuntimeError("sensitive internal diagnostic")

    service.runner = broken
    with pytest.raises(ReviewError, match="Generation failed") as caught:
        service.generate(request_id)
    assert "sensitive" not in str(caught.value)
    assert service.status(request_id)["state"] == "failed"
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)


@pytest.mark.parametrize("flag", ["leak_attempt_detected", "leak_in_package", "leak_scan_complete"])
def test_leak_checks_block_review(service, flag):
    runner = service.runner

    def leaking(*args, **kwargs):
        result = runner(*args, **kwargs)
        setattr(result, flag, flag != "leak_scan_complete")
        return result

    service.runner = leaking
    with pytest.raises(ReviewError, match="leak"):
        service.prepare("Change description")
    assert not service.records


def test_clarification_does_not_offer_approval(service):
    service.runner = lambda *args, **kwargs: CaseResult("clarify", "ok", "clarify")
    result = service.prepare("Create a VLAN")
    assert result["result"]["verdict"] == "clarify"
    assert "review_url" not in result
    assert not service.records


def test_secret_intent_and_busy_runner_are_blocked(service):
    assert service.secrets
    with pytest.raises(ReviewError, match="secret"):
        service.prepare("Use " + next(iter(service.secrets)))
    service.runner_lock.acquire()
    try:
        with pytest.raises(ReviewError, match="running"):
            service.prepare("Normal intent")
    finally:
        service.runner_lock.release()
    assert not service.calls


def test_review_metadata_must_match_intent(service):
    runner = service.runner

    def changed(*args, **kwargs):
        result = runner(*args, **kwargs)
        path = args[4] / args[5] / "review.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["intent"] = "Different intent"
        write_json(path, metadata)
        return result

    service.runner = changed
    with pytest.raises(ReviewError, match="metadata"):
        service.prepare("Normal intent")


def test_http_approval_requires_local_origin_and_nonce(service):
    server = start_approval_server(service)
    try:
        result = service.prepare("Description <script>alert(1)</script>")
        url = urlsplit(result["review_url"])

        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                conn.close()

        status, page = request("GET", url.path + "?" + url.query)
        assert status == 200
        assert "<script>" not in page and "&lt;script&gt;" in page
        csrf = re.search(r"name='csrf' value='([^']+)'", page)[1]
        body = urlencode({"csrf": csrf, "decision": "approve"})
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        assert request("POST", url.path, body, headers)[0] == 400
        headers["Origin"] = "https://foreign.example"
        assert request("POST", url.path, body, headers)[0] == 400
        assert service.status(result["request_id"])["state"] == "pending"
        headers["Origin"] = service.origin
        assert request("POST", url.path, body, headers)[0] == 200
        assert request("POST", url.path, body, headers)[0] == 400
        assert request("GET", url.path + "?token=wrong")[0] == 400
        assert len(service.calls) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_exposes_no_approval_or_context_override_tool():
    spec = importlib.util.spec_from_file_location("multi_bridge", ROOT / "scripts/web_multi_server.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    tools = bridge.tool_definitions()
    assert {t.name for t in tools} == {
        "prepare_network_request",
        "get_network_request",
        "generate_approved_request",
    }
    for tool in tools:
        assert tool.inputSchema["additionalProperties"] is False
        assert len(tool.inputSchema["properties"]) == 1
