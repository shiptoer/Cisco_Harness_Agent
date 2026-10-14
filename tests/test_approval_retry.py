"""Offline A7 regressions: contention before execution versus failures after start."""

import contextlib
import json

import pytest
from test_dsh_workspace import _another_run_holding
from test_web_multi import decide
from test_web_multi import service as recording_service

from harness_agent.runner import pipeline
from harness_agent.runner.dsh import DshSetupError
from harness_agent.runner.pipeline import CaseResult
from harness_agent.web import reviews
from harness_agent.web.reviews import ReviewError, digest

service = recording_service


def fixed_workspace(service, tmp_path):
    path = tmp_path / "cha" / "workspace"
    text = service.config.read_text(encoding="utf-8")
    assert "temp_workspace = true" in text
    service.config.write_text(
        text.replace("temp_workspace = true", f'workspace = "{path.as_posix()}"'), encoding="utf-8"
    )
    service.config_hash = digest(service.config)
    return path


@pytest.mark.slow
def test_real_lock_contention_retains_approval_and_allows_explicit_retry(service, tmp_path):
    ws = fixed_workspace(service, tmp_path)
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    original = service.runner

    def runner(cfg, *args, **kwargs):
        # Real runner also acquires this lock; exercise the same entry boundary.
        with pipeline.lock_workspace(cfg):
            return original(cfg, *args, **kwargs)

    service.runner = runner
    with _another_run_holding(ws):
        with pytest.raises(ReviewError):
            service.generate(request_id)
        assert service.status(request_id)["state"] == "approved"
        assert len(service.calls) == 1
        assert not (service.out_root / ("web-generate-" + request_id)).exists()
    result = service.generate(request_id)
    assert result["state"] == "completed"
    assert service.generate(request_id) == result
    assert len(service.calls) == 2


@pytest.mark.parametrize("exception", [RuntimeError, DshSetupError, OSError])
def test_exception_after_runner_entry_remains_terminal(service, exception, capsys):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    calls = []

    def broken(*args, **kwargs):
        calls.append(True)
        raise exception("private diagnostic CANARY-DO-NOT-PRINT")

    service.runner = broken
    with pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert service.status(request_id)["state"] == "failed"
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "CANARY-DO-NOT-PRINT" not in captured.out + captured.err


@pytest.mark.parametrize("status,verdict", [("error", None), ("ok", "reject"), ("ok", "clarify")])
def test_returned_terminal_outcome_is_cached_without_retry(service, status, verdict):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    calls = []

    def runner(*args, **kwargs):
        calls.append(True)
        return CaseResult("terminal", status, verdict)

    service.runner = runner
    result = service.generate(request_id)
    assert result["execution"]["status"] == status
    assert result["execution"]["verdict"] == verdict
    assert service.generate(request_id) == result
    assert len(calls) == 1


def test_busy_service_does_not_consume_approval(service):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    with service.runner_lock:
        with pytest.raises(ReviewError, match="running"):
            service.generate(request_id)
    assert service.status(request_id)["state"] == "approved"
    assert len(service.calls) == 1


def test_prestart_failure_logs_category_not_private_message(service, monkeypatch, capsys):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)

    @contextlib.contextmanager
    def busy(cfg):
        raise DshSetupError("CANARY-private-path-and-secret")
        yield  # pragma: no cover

    monkeypatch.setattr(reviews, "lock_workspace", busy)
    with pytest.raises(ReviewError, match="approval retained") as caught:
        service.generate(request_id)
    assert service.status(request_id)["state"] == "approved"
    assert len(service.calls) == 1
    captured = capsys.readouterr()
    assert "phase=before_start error=DshSetupError" in captured.err
    assert "CANARY" not in captured.out + captured.err + str(caught.value)


@pytest.mark.parametrize("target", ["context", "config", "model", "proposal", "contract"])
def test_retry_still_checks_all_approved_hashes(service, monkeypatch, target):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    real_lock = reviews.lock_workspace

    @contextlib.contextmanager
    def busy(cfg):
        raise DshSetupError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(reviews, "lock_workspace", busy)
    with pytest.raises(ReviewError, match="approval retained"):
        service.generate(request_id)
    monkeypatch.setattr(reviews, "lock_workspace", real_lock)
    record = service.records[request_id]
    path = {
        "context": service.context,
        "config": service.config,
        "model": service.model_file,
        "proposal": record["proposal_path"],
        "contract": record["contract_path"],
    }[target]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ReviewError, match="changed"):
        service.generate(request_id)
    assert len(service.calls) == 1


@pytest.mark.slow
def test_negative_control_without_early_reservation_consumes_approval(service, tmp_path, monkeypatch):
    ws = fixed_workspace(service, tmp_path)
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    original = service.runner

    def runner(cfg, *args, **kwargs):
        with pipeline.lock_workspace(cfg):
            return original(cfg, *args, **kwargs)

    service.runner = runner
    # Remove only the new pre-start reservation; runner's real lock remains active.
    monkeypatch.setattr(reviews, "lock_workspace", lambda cfg: contextlib.nullcontext())
    with _another_run_holding(ws), pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert service.status(request_id)["state"] == "failed"
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)
    assert len(service.calls) == 1


@pytest.mark.parametrize("fail_state", ["running", "completed"])
def test_audit_failure_is_terminal_and_releases_service_lock(service, monkeypatch, fail_state):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    original = service._audit

    def broken(record):
        if record["state"] == fail_state:
            raise OSError("private audit error")
        original(record)

    monkeypatch.setattr(service, "_audit", broken)
    with pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert service.status(request_id)["state"] == "failed"
    assert "execution" not in service.status(request_id)
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)
    assert not service.runner_lock.locked()
    audit = json.loads(service.records[request_id]["audit_path"].read_text(encoding="utf-8"))
    assert audit["state"] == "failed"


@pytest.mark.slow
def test_explicit_retry_runs_real_fake_pipeline_with_nested_workspace_lock(service, tmp_path, monkeypatch):
    from test_web_multi import CHANGES

    ws = fixed_workspace(service, tmp_path)
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": CHANGES}}],
                "tsa": "auto",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(scenario))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    service.runner = pipeline.run_case
    with _another_run_holding(ws), pytest.raises(ReviewError, match="approval retained"):
        service.generate(request_id)
    result = service.generate(request_id)
    assert result["state"] == "completed"
    assert result["execution"]["status"] == "ok", result
    assert result["execution"]["verdict"] == "accept", result
    assert "SALES-PC-03" in result["execution"]["change_package"]
    assert service.generate(request_id) == result


def test_reservation_spans_runner_and_is_released_after_failure(service, monkeypatch):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    events = []

    @contextlib.contextmanager
    def reserve(cfg):
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    def runner(*args, **kwargs):
        assert events == ["acquired"]
        events.append("runner")
        raise RuntimeError("failure after entry")

    monkeypatch.setattr(reviews, "lock_workspace", reserve)
    service.runner = runner
    with pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert events == ["acquired", "runner", "released"]
    assert service.status(request_id)["state"] == "failed"


def test_concurrent_generate_cannot_spend_approval_twice(service):
    import concurrent.futures
    import threading

    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)
    entered = threading.Event()
    finish = threading.Event()
    original = service.runner
    calls = []

    def runner(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert finish.wait(timeout=10)
        return original(*args, **kwargs)

    service.runner = runner
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.generate, request_id)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(ReviewError, match="running"):
                service.generate(request_id)
        finally:
            finish.set()
        result = first.result(timeout=10)
    assert service.generate(request_id) == result
    assert len(calls) == 1


def test_failure_audit_cannot_reopen_approval_or_hide_terminal_state(service, monkeypatch):
    request_id = service.prepare("Description change")["request_id"]
    decide(service, request_id)

    def broken_audit(record):
        raise OSError("CANARY-private-audit")

    monkeypatch.setattr(service, "_audit", broken_audit)
    with pytest.raises(ReviewError, match="Generation failed"):
        service.generate(request_id)
    assert service.status(request_id)["state"] == "failed"
    assert not service.runner_lock.locked()
    assert len(service.calls) == 1
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)
