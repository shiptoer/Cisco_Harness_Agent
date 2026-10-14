"""Offline A6 channel-isolation probes, including a deliberately leaky control."""

import http.client
import io
import json
import re
import subprocess
from urllib.parse import urlencode, urlsplit

import pytest
from test_web_multi import service as recording_service

from harness_agent.web import open_browser
from harness_agent.web.approval_http import start_approval_server
from harness_agent.web.reviews import ReviewError

service = recording_service


class Browser:
    def __init__(self, server):
        self.server = server
        self.cookie = ""

    def request(self, path, *, data=None, headers=None):
        headers = dict(headers or {})
        if self.cookie:
            headers.setdefault("Cookie", self.cookie)
        if data is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("GET" if data is None else "POST", path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            conn.close()

    def bootstrap(self, url):
        parsed = urlsplit(url)
        status, headers, _ = self.request(parsed.path + "?" + parsed.query)
        assert status == 303
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        return headers

    def nonce(self, request_id):
        status, _, page = self.request("/review/" + request_id)
        assert status == 200
        return re.search(r"name='csrf' value='([^']+)'", page)[1]

    def decide(self, request_id, nonce, origin, decision="approve"):
        return self.request(
            "/review/" + request_id,
            data=urlencode({"csrf": nonce, "decision": decision}),
            headers={"Origin": origin},
        )[0]


@pytest.fixture
def approval(service):
    server = start_approval_server(service)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_only_private_bootstrap_can_open_operator_session(service, approval):
    request_id = service.prepare("Private request")["request_id"]
    attacker = Browser(approval)
    for path in [
        "/operator",
        "/review/" + request_id,
        "/open?token=" + request_id,
        "/open?token=wrong",
        "/open?" + urlencode({"token": "không-hợp-lệ"}),
        "/open",
        "/review/" + request_id + "?token=" + request_id,
    ]:
        status, _, body = attacker.request(path)
        assert status == 400
        assert "Private request" not in body and "name='csrf'" not in body
    assert attacker.decide(request_id, "guessed", service.origin) == 400
    with pytest.raises(ReviewError, match="approval"):
        service.generate(request_id)
    operator = Browser(approval)
    headers = operator.bootstrap(approval.operator_url)
    assert headers["Location"] == "/operator"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert (
        attacker.request(urlsplit(approval.operator_url).path + "?" + urlsplit(approval.operator_url).query)[
            0
        ]
        == 400
    )
    assert request_id in operator.request("/operator")[2]
    csrf = operator.nonce(request_id)
    # Even knowledge of a valid nonce and spoofed Origin is insufficient without the cookie.
    assert attacker.decide(request_id, csrf, service.origin) == 400
    assert service.status(request_id)["state"] == "pending"
    assert operator.decide(request_id, csrf, service.origin) == 200
    assert operator.decide(request_id, csrf, service.origin) == 400
    assert len(service.calls) == 1


@pytest.mark.parametrize(
    "cookie", ["wrong=x", "cha_operator_{port}=wrong", 'cha_operator_{port}="é"', "[=bad"]
)
def test_bad_cookies_fail_closed(service, approval, cookie):
    request_id = service.prepare("Request")["request_id"]
    browser = Browser(approval)
    browser.cookie = cookie.format(port=approval.server_port)
    assert browser.request("/operator")[0] == 400
    assert browser.request("/review/" + request_id)[0] == 400
    assert service.status(request_id)["state"] == "pending"


def test_cookie_is_server_specific_and_csrf_is_request_specific(service, approval):
    a = service.prepare("A")["request_id"]
    b = service.prepare("B")["request_id"]
    operator = Browser(approval)
    operator.bootstrap(approval.operator_url)
    csrf = operator.nonce(a)
    assert operator.decide(b, csrf, service.origin) == 400
    assert operator.decide(a, csrf, "https://foreign.example") == 400
    assert operator.decide(a, csrf, service.origin) == 200
    old_origin = service.origin
    other = start_approval_server(service)
    try:
        second = Browser(other)
        second.cookie = operator.cookie
        assert second.request("/operator")[0] == 400
        # Renaming the cookie cannot rescue a previous session secret.
        second.cookie = f"cha_operator_{other.server_port}=" + operator.cookie.split("=", 1)[1]
        assert second.request("/operator")[0] == 400
    finally:
        other.shutdown()
        other.server_close()
        service.origin = old_origin


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_model_projection_and_artifacts_have_no_operator_capability(service, approval, decision, capsys):
    prepared = service.prepare("Normal request")
    request_id = prepared["request_id"]
    operator = Browser(approval)
    operator.bootstrap(approval.operator_url)
    csrf = operator.nonce(request_id)
    replies = [prepared, service.status(request_id)]
    assert operator.decide(request_id, csrf, service.origin, decision) == 200
    replies.append(service.status(request_id))
    if decision == "approve":
        replies.append(service.generate(request_id))
    else:
        with pytest.raises(ReviewError):
            service.generate(request_id)
    replies.append(service.status(request_id))
    serialized = json.dumps(replies)
    for forbidden_key in ["review_url", "view_token", "csrf", "operator_url", "session_token"]:
        assert forbidden_key not in serialized
    assert "view_token" not in service.records[request_id]
    artifacts = "".join(p.read_text(encoding="utf-8") for p in service.out_root.rglob("*") if p.is_file())
    captured = capsys.readouterr()
    for secret in [
        approval.operator_url,
        urlsplit(approval.operator_url).query.split("=", 1)[1],
        operator.cookie.split("=", 1)[1],
        csrf,
    ]:
        assert secret not in serialized + artifacts + captured.out + captured.err


def test_negative_control_exposed_bootstrap_allows_automated_self_approval(service, approval, monkeypatch):
    # Deliberately restore a capability-bearing tool response. No human is involved.
    original = service.status
    monkeypatch.setattr(service, "status", lambda key: {**original(key), "review_url": approval.operator_url})
    response = service.prepare("Negative control")
    attacker = Browser(approval)
    attacker.bootstrap(response["review_url"])
    request_id = response["request_id"]
    assert attacker.decide(request_id, attacker.nonce(request_id), service.origin) == 200
    assert service.generate(request_id)["execution"]["change_package"]


def test_browser_helper_sends_capability_only_over_stdin(monkeypatch):
    url = "http://127.0.0.1:12345/open?token=private-test-capability"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(open_browser.subprocess, "run", run)
    assert open_browser.open_operator_page(url)
    argv, kwargs = calls[0]
    assert url not in repr(argv)
    assert "env" not in kwargs
    assert kwargs["input"] == url.encode("utf-8")
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "error", [OSError("private-test-capability"), subprocess.TimeoutExpired("helper", 15)]
)
def test_browser_failure_never_prints_capability(monkeypatch, capsys, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(open_browser.subprocess, "run", fail)
    assert not open_browser.open_operator_page("http://127.0.0.1:12345/open?token=private-test-capability")
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize(
    "url,valid",
    [
        ("http://127.0.0.1:12345/open?token=private", True),
        ("https://external.example/open?token=private", False),
        ("http://localhost:12345/open?token=private", False),
        ("http://127.0.0.1:12345/review/id", False),
        ("http://user:password@127.0.0.1:12345/open?token=private", False),
    ],
)
def test_browser_helper_only_opens_local_bootstrap(monkeypatch, url, valid):
    opened = []
    monkeypatch.setattr(open_browser.sys, "stdin", io.TextIOWrapper(io.BytesIO(url.encode("utf-8"))))
    monkeypatch.setattr(open_browser.webbrowser, "open", lambda value, **kwargs: opened.append(value) or True)
    assert open_browser.main() == (0 if valid else 1)
    assert opened == ([url] if valid else [])
