"""Offline approval-boundary tests; model calls are replaced by a recording runner."""

import http.client
import importlib.util
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

from harness_agent.runner import pipeline
from harness_agent.runner.dsh import DshSetupError
from harness_agent.runner.pipeline import CaseResult, ErrorCode, run_case
from harness_agent.runner.workspace import WorkspaceError
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
        # Bridge ghim hash context lúc khởi động và truyền "now" có múi giờ cho runner D (D-054).
        assert kwargs["context_sha256"] == digest(ctx)
        assert kwargs["evaluated_at"].tzinfo is not None
        assert kwargs["level"] == "B4"
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
        url = urlsplit(service.review_link(result["request_id"]))  # link người vận hành thấy ở stderr (A6)

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
        assert "review_url" not in tool.description  # A6: model không được hứa có link duyệt


def test_prepare_response_never_contains_the_view_token(service, capsys):
    """A6 (review 11/10): token xem trang duyệt chỉ dành cho người, không bao giờ vào kết quả tool MCP."""
    result = service.prepare("Change description")
    request_id = result["request_id"]
    token = service.records[request_id]["view_token"]
    for response in (result, service.status(request_id)):
        text = json.dumps(response, ensure_ascii=False)
        assert token not in text
        assert "review_url" not in response
        assert response["request_id"] == request_id
    # Đối chứng âm: token là chuỗi thật và nằm trong link, nên phép tìm ở trên bắt được nếu nó bị trả về.
    assert len(token) >= 32 and token in service.review_link(request_id)
    # Link được in ra stderr của bridge (terminal của người vận hành), không qua stdout là kênh MCP.
    captured = capsys.readouterr()
    assert token in captured.err
    assert token not in captured.out


def test_workspace_error_before_dsh_keeps_approval(service, monkeypatch, capsys):
    """A7 (review 11/10): lỗi máy trước khi dsh khởi động không tiêu thụ phê duyệt; lỗi được log ra stderr."""
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)
    fake_runner = service.runner

    def broken_workspace():
        raise WorkspaceError("workspace locked by another run (test)")

    # Runner thật của D: `_workdir` đổi WorkspaceError thành DshSetupError trước khi tạo run-dir hay gọi dsh.
    monkeypatch.setattr(pipeline.workspace, "temporary", broken_workspace)
    monkeypatch.setattr(service, "runner", run_case)
    with pytest.raises(ReviewError, match="kept") as caught:
        service.generate(request_id)
    assert "locked" not in str(caught.value)  # chi tiết chỉ ra stderr, không vào kết quả tool
    assert service.status(request_id)["state"] == "approved"
    err = capsys.readouterr().err
    assert "DshSetupError" in err and "workspace locked by another run (test)" in err
    # Sửa xong máy thì generate lại được bằng chính phê duyệt cũ.
    monkeypatch.setattr(service, "runner", fake_runner)
    assert service.generate(request_id)["state"] == "completed"


def test_setup_error_after_dsh_started_consumes_approval(service, monkeypatch):
    """Đối chứng âm cho A7: dsh đã chạy (có output stage) thì lỗi cấu hình vẫn tiêu thụ phê duyệt."""
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)

    def started_then_broken(cfg, ctx, intent, role, out, run_id, **kwargs):
        folder = out / run_id
        folder.mkdir()
        (folder / "cba.ndjson").write_text("{}\n", encoding="utf-8")
        raise DshSetupError("TSA stage could not start")

    monkeypatch.setattr(service, "runner", started_then_broken)
    with pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert service.status(request_id)["state"] == "failed"


def _preflight_failed_runner(*, write_ndjson):
    def runner(cfg, ctx, intent, role, out, run_id, **kwargs):
        folder = out / run_id
        folder.mkdir()
        if write_ndjson:  # dsh của stage CBA đã chạy, preflight của TSA mới hỏng
            (folder / "cba.ndjson").write_text("{}\n", encoding="utf-8")
        return CaseResult(
            run_id,
            "error",
            None,
            reason="preflight tsa thất bại: harness server exited 1 (test)",
            error_code=ErrorCode.PREFLIGHT_FAILED,
        )

    return runner


def test_preflight_failure_before_dsh_keeps_approval(service, monkeypatch, capsys):
    """Review Task 1 #1: preflight hỏng là kết cục ERROR (không phải exception) trước khi dsh chạy;
    phê duyệt còn."""
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)
    fake_runner = service.runner
    monkeypatch.setattr(service, "runner", _preflight_failed_runner(write_ndjson=False))
    with pytest.raises(ReviewError, match="kept") as caught:
        service.generate(request_id)
    assert "harness server" not in str(caught.value)
    assert service.status(request_id)["state"] == "approved"
    assert "execution" not in service.status(request_id)
    err = capsys.readouterr().err
    assert "PREFLIGHT_FAILED" in err and "harness server exited 1 (test)" in err
    # Runner thật xoá run-dir trùng tên trước khi chạy; runner giả của fixture thì không.
    shutil.rmtree(service.out_root / ("web-generate-" + request_id))
    monkeypatch.setattr(service, "runner", fake_runner)
    assert service.generate(request_id)["state"] == "completed"


def test_preflight_failure_after_dsh_started_consumes_approval(service, monkeypatch):
    """Đối chứng âm: đã có output stage của dsh thì kết cục ERROR được ghi nhận, không dùng lại phê duyệt."""
    request_id = service.prepare("Change description")["request_id"]
    decide(service, request_id)
    monkeypatch.setattr(service, "runner", _preflight_failed_runner(write_ndjson=True))
    result = service.generate(request_id)
    assert result["state"] == "completed"
    assert result["execution"]["error_code"] == "PREFLIGHT_FAILED"
    assert "change_package" not in result["execution"]
    # Gọi lại chỉ trả kết quả đã lưu; runner không chạy lần hai bằng phê duyệt cũ.
    monkeypatch.setattr(service, "runner", lambda *a, **k: pytest.fail("approval reused"))
    assert service.generate(request_id) == result
