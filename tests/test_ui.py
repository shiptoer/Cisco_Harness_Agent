"""Giao diện vận hành (D-057): các control bảo mật, mỗi control một đối chứng âm.

Không sinh process: server chạy trong thread, runner được thay bằng một launcher giả ghi ra run-dir
giống runner thật (trace dựng bằng chính `Trace`, nên chuỗi hash là thật). Lượt chạy thật qua process con:
`tests/test_ui_e2e.py` (slow).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from harness_agent.harness.trace import Trace, verify_chain
from harness_agent.levels import Level
from harness_agent.ui import views
from harness_agent.ui.app import UiApp, UiError, UiSettings
from harness_agent.ui.web import COOKIE, Auth, make_server

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
TOKEN = "test-token-0123456789"  # noqa: S105 — token của test, không phải bí mật
XSS_DESC = '<script>alert("desc")</script>'
XSS_BANNER = '"><img src=x onerror=alert(1)>'
XSS_INTENT = "Tạo VLAN 30 </textarea><script>alert('intent')</script>"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
CFG = "vlan 30\n name FINANCE\nexit\nend\n"


def _context(tmp_path: Path, provenance: dict | None = None) -> Path:
    data = json.loads(CAMPUS.read_text(encoding="utf-8"))
    for d in data["devices"]:
        if d["hostname"] == "SW2":
            d["interfaces"][1]["description"] = XSS_DESC
        if d["hostname"] == "R1":
            d["banner_motd"] = XSS_BANNER
    if provenance:
        head = {"schema_version": data["schema_version"], "context_id": data["context_id"]}
        data = {**head, "provenance": provenance, **{k: v for k, v in data.items() if k not in head}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "context.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _real_config(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "real.toml"
    workspace = (tmp_path / "ws").as_posix()
    path.write_text(
        '[dsh]\ncommand = ["python", "scripts/fake_dsh.py"]\n'
        'base_patch = "tests/fixtures/dsh.base.patch.yml"\n'
        f'requires_api_key = true\nworkspace = "{workspace}"\n[python]\nexe = "python"\n',
        encoding="utf-8",
    )
    return path


class FakeProc:
    def __init__(self, launcher: FakeLauncher, argv: list[str]):
        self.launcher, self.argv = launcher, argv

    def wait(self, timeout: float | None = None) -> int:
        self.launcher.gate.wait(10)
        self.launcher.write_run(self.argv)
        return 0

    def kill(self) -> None:
        pass


class FakeLauncher:
    """Thay `python -m harness_agent.runner`: ghi run-dir như runner, với kết cục chọn trước."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.leak = False
        self.secret = ""
        self.trace_key: bytes | None = None  # như runner thật (D-060): trace có `sig` khi có khoá
        self.grounding: dict | None = None  # event `grounding` của runner (D-061), None = không ghi

    def __call__(self, argv: list[str], env: dict[str, str], log_path: Path) -> FakeProc:
        self.calls.append((argv, env))
        return FakeProc(self, argv)

    def write_run(self, argv: list[str]) -> None:
        arg = {argv[i]: argv[i + 1] for i in range(3, len(argv) - 1, 2)}
        run_dir = Path(arg["--out"]) / arg["--run-id"]
        (run_dir / "candidate").mkdir(parents=True)
        trace = Trace(run_dir / "trace.jsonl", actor="runner", key=self.trace_key)
        trace.write("case_start", intent=arg["--intent"], context_snapshot={"context_id": "campus-small-01"})
        cfg = CFG + (f"! {self.secret}\n" if self.leak else "")
        (run_dir / "candidate" / "SW2.cfg").write_text(cfg, encoding="utf-8", newline="\n")
        (run_dir / "change_package.md").write_text(f"# Change package\n{cfg}", encoding="utf-8", newline="\n")
        change = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
        (run_dir / "change_request.json").write_text(
            json.dumps({"status": "ready", "changes": [change], "rationale": XSS_DESC}), encoding="utf-8"
        )
        if self.grounding is not None:
            trace.write("grounding", **self.grounding)
        result = {
            "status": "ok", "verdict": "accept", "stopped_by": None, "error_code": None,
            "reason": "1 thao tác", "level": "B3", "controls": ["i", "ii", "iii", "v"],
            "leak_attempt_detected": False,
            "leak_in_package": self.leak, "leak_scan_complete": True, "leak_sources": [],
        }  # fmt: skip
        trace.write("case_end", result=result)
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")


class Ui:
    def __init__(self, app: UiApp, launcher: FakeLauncher):
        self.app, self.launcher = app, launcher
        self.auth = Auth(TOKEN)
        self.server = make_server(app, self.auth, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, path, *, body=None, headers=None, host=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        if cookie:
            conn.putheader("Cookie", f"{COOKIE}={self.session}")
        data = body.encode("utf-8") if body is not None else b""
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data if body is not None else None)
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace"))
        conn.close()
        return out

    def login(self) -> None:
        self.session = ""
        status, headers, _ = self.request("GET", f"/login?token={TOKEN}", cookie=False)
        assert status == 303
        self.session = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def post(self, path, fields, *, origin=True, csrf=True, **kw):
        if csrf:
            fields = {"csrf": self.auth.csrf(self.session), **fields}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        return self.request("POST", path, body=urlencode(fields), headers=headers, **kw)

    def wait_idle(self) -> None:
        deadline = time.time() + 10
        while self.app.active and time.time() < deadline:
            time.sleep(0.02)
        assert self.app.active is None

    def run(self, intent="Tạo VLAN 30 tên FINANCE trên SW2") -> str:
        status, headers, _ = self.post("/run", {"intent": intent})
        assert status == 303, status
        self.wait_idle()
        return headers["Location"].rsplit("/", 1)[1]


def _app(tmp_path, *, config=None, provenance=None, clock=NOW, launcher=None) -> tuple[UiApp, FakeLauncher]:
    launcher = launcher or FakeLauncher()
    app = UiApp(
        UiSettings(
            config=config or _real_config(tmp_path),
            context=_context(tmp_path, provenance),
            role="network_admin",
            level=Level.B3,
            reviewer="Reviewer Một",
            out=tmp_path / "out",
        ),
        clock=lambda: clock,
        launcher=launcher,
    )
    return app, launcher


@pytest.fixture(autouse=True)
def _api_key_present(monkeypatch):
    """`_real_config` khai `requires_api_key = true`; UI chặn chạy khi terminal không có key (hồi quy 16/10),
    nên mọi test ở đây giả lập một key đã nhập. Key giả này không đi đâu: launcher là hàm giả."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")


@pytest.fixture
def ui(tmp_path):
    app, launcher = _app(tmp_path)
    u = Ui(app, launcher)
    u.login()
    yield u
    u.server.shutdown()
    u.server.server_close()


# ------------------------------------------------------------------ token, Host, header
def test_token_is_required_and_traded_for_a_cookie(ui):
    assert ui.request("GET", "/", cookie=False)[0] == 401
    assert ui.request("GET", "/login?token=wrong", cookie=False)[0] == 403
    assert ui.request("GET", "/login", cookie=False)[0] == 403
    status, headers, _ = ui.request("GET", f"/login?token={TOKEN}", cookie=False)
    assert status == 303 and headers["Location"] == "/"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    # Đối chứng âm: cùng request, có cookie hợp lệ → vào được.
    assert ui.request("GET", "/")[0] == 200
    ui.session = "forged-session"
    assert ui.request("GET", "/")[0] == 401


def test_wrong_host_is_refused(ui):
    assert ui.request("GET", "/", host=f"evil.example:{ui.port}")[0] == 400
    assert ui.request("GET", "/", host="127.0.0.1:1")[0] == 400
    assert ui.post("/run", {"intent": "x"}, host=f"evil.example:{ui.port}")[0] == 400
    # Đối chứng âm: Host đúng (hai dạng loopback) thì vào được.
    assert ui.request("GET", "/", host=f"localhost:{ui.port}")[0] == 200
    assert ui.request("GET", "/")[0] == 200


def test_security_headers_and_no_script_on_any_page(ui):
    run_id = ui.run()
    for path in ("/", f"/run/{run_id}"):
        status, headers, body = ui.request("GET", path)
        assert status == 200
        assert "script-src" not in headers["Content-Security-Policy"]
        assert headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert "<script" not in body.replace("&lt;script", "")


# ------------------------------------------------------------------ CSRF, Origin, trường form
def test_csrf_and_origin_are_required_for_every_post(ui):
    assert ui.post("/run", {"intent": "Tạo VLAN 30"}, csrf=False)[0] == 400  # thiếu trường csrf
    assert ui.post("/run", {"intent": "Tạo VLAN 30", "csrf": "0" * 64}, csrf=False)[0] == 403
    assert ui.post("/run", {"intent": "Tạo VLAN 30"}, origin=False)[0] == 403
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil.example"}
    body = urlencode({"csrf": ui.auth.csrf(ui.session), "intent": "Tạo VLAN 30"})
    assert ui.request("POST", "/run", body=body, headers=headers)[0] == 403
    assert ui.launcher.calls == []
    # Đối chứng âm: đủ CSRF và Origin đúng → chạy.
    assert ui.post("/run", {"intent": "Tạo VLAN 30"})[0] == 303
    ui.wait_idle()


@pytest.mark.parametrize("extra", [{"role": "security_admin"}, {"level": "B1"}, {"context": "x.json"}])
def test_form_with_role_level_or_unknown_field_is_400(ui, extra):
    assert ui.post("/run", {"intent": "Tạo VLAN 30", **extra})[0] == 400
    assert ui.launcher.calls == []
    # Đối chứng âm: bỏ trường lạ → chạy, với role và mức của server.
    assert ui.post("/run", {"intent": "Tạo VLAN 30"})[0] == 303
    ui.wait_idle()
    argv = ui.launcher.calls[0][0]
    assert argv[argv.index("--role") + 1] == "network_admin" and argv[argv.index("--level") + 1] == "B3"


def test_runner_arguments_bind_the_context_and_the_ui_clock(ui):
    ui.run()
    argv = ui.launcher.calls[0][0]
    assert argv[argv.index("--context-sha256") + 1] == ui.app.context_sha256
    assert argv[argv.index("--evaluated-at") + 1] == "2026-10-15T09:00:00Z"
    assert "--intent" in argv and "shell" not in " ".join(argv)


@pytest.mark.parametrize("intent", ["", " ", "x" * 2001, "a\x00b", "a\x1bb"])
def test_bad_intent_is_refused(ui, intent):
    assert ui.post("/run", {"intent": intent})[0] == 400
    assert ui.launcher.calls == []


def test_intent_with_a_context_secret_is_refused(ui):
    secret = sorted(ui.app.secrets)[0]
    assert ui.post("/run", {"intent": f"đặt key {secret}"})[0] == 400
    assert ui.launcher.calls == []


# ------------------------------------------------------------------ XSS
def _pages(ui) -> str:
    run_id = ui.run(XSS_INTENT)
    return ui.request("GET", "/")[2] + ui.request("GET", f"/run/{run_id}")[2]


def test_untrusted_text_is_escaped_everywhere(ui):
    pages = _pages(ui)
    for payload in (XSS_DESC, XSS_BANNER, "<script>alert('intent')</script>"):
        assert payload not in pages
    assert "&lt;script&gt;alert(&quot;desc&quot;)&lt;/script&gt;" in pages
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in pages


def test_negative_control_without_escaping_the_payloads_reach_the_page(ui, monkeypatch):
    monkeypatch.setattr(views, "esc", lambda value: "" if value is None else str(value))
    pages = _pages(ui)
    assert XSS_DESC in pages and XSS_BANNER in pages


# ------------------------------------------------------------------ path traversal
@pytest.mark.parametrize(
    "path",
    [
        "/run/../out",
        "/run/..%2f..%2fcontext.json",
        "/run/ui-20261015t090000z-abcdef",  # đúng dạng nhưng server này không sinh
        "/run/_ui",
        "/run/ui-20261015t090000z-abcdef/cfg/..%2fresult",
    ],
)
def test_run_paths_outside_the_server_index_are_404(ui, path):
    ui.run()
    assert ui.request("GET", path)[0] == 404


def test_cfg_name_traversal_is_404_and_a_real_run_opens(ui):
    run_id = ui.run()
    assert ui.request("GET", f"/run/{run_id}/cfg/..%2f..%2fcontext")[0] == 404
    assert ui.request("GET", f"/run/{run_id}/cfg/SW2")[0] == 403  # chưa duyệt: chặn trước khi tìm file
    ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})
    assert ui.request("GET", f"/run/{run_id}/cfg/SW9")[0] == 404  # đã duyệt, thiết bị không có
    assert ui.request("GET", f"/run/{run_id}/cfg/..%2fSW2")[0] == 404
    with pytest.raises(UiError):
        ui.app.run_dir(f"../{run_id}")
    # Đối chứng âm: run_id do server sinh thì mở được.
    assert ui.request("GET", f"/run/{run_id}")[0] == 200


# ------------------------------------------------------------------ leak_in_package
def test_leak_in_package_hides_the_package_and_blocks_review(ui):
    ui.launcher.leak, ui.launcher.secret = True, sorted(ui.app.secrets)[0]
    run_id = ui.run()
    page = ui.request("GET", f"/run/{run_id}")[2]
    assert "vlan 30" not in page and ui.launcher.secret not in page
    assert "leak_in_package" in page
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 409
    assert ui.request("GET", f"/run/{run_id}/cfg/SW2")[0] == 403


def test_negative_control_without_leak_the_package_is_shown_and_reviewable(ui):
    run_id = ui.run()
    page = ui.request("GET", f"/run/{run_id}")[2]
    assert "vlan 30" in page
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 303


# ------------------------------------------------------------------ một lượt một lúc
def test_only_one_run_at_a_time(ui):
    ui.launcher.gate.clear()
    first = ui.post("/run", {"intent": "Tạo VLAN 30"})
    assert first[0] == 303
    second = ui.post("/run", {"intent": "Tạo VLAN 31"})
    assert second[0] == 409 and "đang chạy" in second[2]
    assert len(ui.launcher.calls) == 1
    ui.launcher.gate.set()
    ui.wait_idle()
    # Đối chứng âm: lượt trước xong thì chạy được lượt mới, với run_id mới.
    third = ui.post("/run", {"intent": "Tạo VLAN 31"})
    assert third[0] == 303 and third[1]["Location"] != first[1]["Location"]
    ui.wait_idle()


# ------------------------------------------------------------------ bản duyệt trong trace
def test_review_extends_the_trace_hash_chain(ui):
    run_id = ui.run()
    trace = ui.app.out / run_id / "trace.jsonl"
    before = trace.read_bytes()
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": "đúng intent"})[0] == 303
    after = trace.read_bytes()
    assert after.startswith(before) and verify_chain(trace) == "unsigned"
    last = json.loads(after.splitlines()[-1])
    assert (last["actor"], last["event"]) == ("reviewer", "human_review")
    data = last["data"]
    assert (data["decision"], data["reviewer"], data["deployed"]) == ("approve", "Reviewer Một", False)
    package = ui.app.out / run_id / "change_package.md"
    assert data["package_sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert (
        ui.post(f"/run/{run_id}/review", {"decision": "reject", "reason": "x"})[0] == 409
    )  # chỉ duyệt một lần
    status, headers, body = ui.request("GET", f"/run/{run_id}/cfg/SW2")
    assert status == 200 and body == CFG and "attachment" in headers["Content-Disposition"]


def test_negative_control_a_tampered_trace_is_detected_and_review_refused(ui):
    run_id = ui.run()
    trace = ui.app.out / run_id / "trace.jsonl"
    lines = trace.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("campus-small-01", "campus-small-02")
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(trace) == "broken"
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 409


def test_signed_trace_label_says_signature_not_checked(ui):
    """D-060: UI không giữ khoá, nên với trace có ký chỉ báo liên kết hash hợp lệ, không báo chữ ký hợp lệ."""
    ui.launcher.trace_key = bytes(range(32))
    run_id = ui.run()
    page = ui.request("GET", f"/run/{run_id}")[2]
    assert "liên kết hash hợp lệ; chữ ký không kiểm (UI không giữ khoá)" in page
    assert "chuỗi hash hợp lệ" not in page
    # Đối chứng: trace không khoá vẫn ghi "chuỗi hash hợp lệ" kèm "không khoá".
    ui.launcher.trace_key = None
    plain = ui.request("GET", f"/run/{ui.run()}")[2]
    assert "chuỗi hash hợp lệ" in plain and "không khoá" in plain


def test_negative_control_tampered_signed_trace_is_detected_and_review_refused(ui):
    """Trace có ký: sửa verdict trong `case_end`, giữ nguyên `prev_hash` và `hash` → chuỗi SHA-256 "broken"
    (UI kiểm được không cần khoá) → không duyệt được."""
    key = bytes(range(32))
    ui.launcher.trace_key = key
    run_id = ui.run()
    trace = ui.app.out / run_id / "trace.jsonl"
    assert verify_chain(trace) == "unsigned" and verify_chain(trace, key) == "ok"
    lines = trace.read_text(encoding="utf-8").splitlines()
    end = json.loads(lines[-1])
    assert end["event"] == "case_end" and end["signed"] is True and "sig" in end
    end["data"]["result"]["verdict"] = "reject"
    lines[-1] = json.dumps(end, ensure_ascii=False, sort_keys=True)
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(trace) == "broken" and verify_chain(trace, key) == "broken"
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 409
    assert "chuỗi hash không hợp lệ" in ui.request("GET", f"/run/{run_id}")[2]


def test_reject_needs_a_reason_and_export_needs_approval(ui):
    run_id = ui.run()
    assert ui.request("GET", f"/run/{run_id}/cfg/SW2")[0] == 403
    assert ui.post(f"/run/{run_id}/review", {"decision": "reject", "reason": ""})[0] == 400
    assert ui.post(f"/run/{run_id}/review", {"decision": "deploy", "reason": "x"})[0] == 400
    assert ui.post(f"/run/{run_id}/review", {"decision": "reject", "reason": "sai cổng"})[0] == 303
    assert ui.request("GET", f"/run/{run_id}/cfg/SW2")[0] == 403


def test_export_refuses_a_cfg_changed_after_approval(ui):
    run_id = ui.run()
    ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})
    (ui.app.out / run_id / "candidate" / "SW2.cfg").write_text(
        CFG + "username x privilege 15\n", encoding="utf-8"
    )
    assert ui.request("GET", f"/run/{run_id}/cfg/SW2")[0] == 409


# ------------------------------------------------------------------ freshness
COLLECTED = {"source": "collector", "collected_at": "2026-10-13T08:39:15Z", "ttl_seconds": 86400}


def test_stale_context_blocks_running(tmp_path):
    app, launcher = _app(tmp_path, provenance=COLLECTED, clock=datetime(2026, 10, 20, tzinfo=UTC))
    assert app.freshness_view()["status"] == "stale"
    with pytest.raises(UiError) as exc:
        app.start_run(intent="Tạo VLAN 30")
    assert exc.value.status == 409 and launcher.calls == []
    assert "disabled" in views.dashboard(app, "csrf")


def test_negative_control_fresh_and_expiring_context_runs(tmp_path):
    fresh_at = datetime(2026, 10, 13, 12, tzinfo=UTC)
    app, launcher = _app(tmp_path, provenance=COLLECTED, clock=fresh_at)
    assert app.freshness_view()["status"] == "fresh"
    app.start_run(intent="Tạo VLAN 30")
    deadline = time.time() + 10
    while app.active and time.time() < deadline:
        time.sleep(0.02)
    assert launcher.calls
    late = datetime(2026, 10, 14, 6, tzinfo=UTC)  # còn ~2,7 giờ trên 24 giờ
    assert _app(tmp_path / "b", provenance=COLLECTED, clock=late)[0].freshness_view()["status"] == "expiring"


def test_context_file_changed_after_start_blocks_running(tmp_path):
    app, launcher = _app(tmp_path)
    app.context_path.write_bytes(app.context_path.read_bytes() + b" ")
    with pytest.raises(UiError) as exc:
        app.start_run(intent="Tạo VLAN 30")
    assert exc.value.status == 409 and launcher.calls == []


# ------------------------------------------------------------------ chế độ fake
def test_fake_mode_takes_a_scenario_name_and_its_intent_read_only(tmp_path):
    app, launcher = _app(tmp_path, config=ROOT / "config" / "fake.toml")
    u = Ui(app, launcher)
    u.login()
    try:
        page = u.request("GET", "/?scenario=clean-vlan30")[2]
        assert "readonly" in page and "Kết quả do kịch bản quyết định, không do intent" in page
        assert u.post("/run", {"intent": "Xoá hết cấu hình"})[0] == 400  # không nhận intent tự do
        assert u.post("/run", {"scenario": "../../etc"})[0] == 400
        assert u.post("/run", {"scenario": "clean-vlan30"})[0] == 303
        u.wait_idle()
        argv, env = launcher.calls[0]
        assert argv[argv.index("--intent") + 1] == app.scenarios["clean-vlan30"].intent
        assert Path(env["FAKE_DSH_SCENARIO"]).name == "clean-vlan30.json"
    finally:
        u.server.shutdown()
        u.server.server_close()


def test_settings_are_validated_at_start(tmp_path):
    for kwargs in ({"role": "superuser"}, {"level": Level.B1}, {"reviewer": "a\x00b"}):
        settings = {
            "config": _real_config(tmp_path), "context": _context(tmp_path), "role": "network_admin",
            "level": Level.B3, "reviewer": "R", "out": tmp_path / "o", **kwargs,
        }  # fmt: skip
        with pytest.raises(UiError):
            UiApp(UiSettings(**settings), launcher=FakeLauncher())


def test_history_lists_runs_newest_first(ui):
    first = ui.run("Tạo VLAN 30")
    ui.app.clock = lambda: NOW + timedelta(seconds=5)
    second = ui.run("Tạo VLAN 31")
    assert [r["run_id"] for r in ui.app.history()] == [second, first]


def test_referrer_policy_keeps_the_same_origin_header_on_form_posts(ui):
    """Hồi quy (chạy thử bằng Chrome 15/10/2026): với `Referrer-Policy: no-referrer`, trình duyệt gửi
    `Origin: null` khi submit form, nên phép kiểm Origin chặn mọi form hợp lệ. `same-origin` giữ Origin đúng
    cho request cùng gốc và không gửi referrer ra trang khác."""
    headers = ui.request("GET", "/")[1]
    assert headers["Referrer-Policy"] == "same-origin"
    # Một POST mang `Origin: null` (cái trình duyệt gửi khi policy là no-referrer) vẫn bị chặn.
    body = urlencode({"csrf": ui.auth.csrf(ui.session), "intent": "Tạo VLAN 30"})
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"}
    assert ui.request("POST", "/run", body=body, headers=form)[0] == 403


# ------------------------------------------------------------------ hồi quy 16/10/2026 (đánh giá lần 3)
def test_localhost_host_accepts_the_matching_localhost_origin(ui):
    """Mở UI bằng http://localhost:<port> thì đăng nhập và xem trang được, nhưng mọi form bị 403 vì phép
    kiểm Origin chỉ nhận 127.0.0.1 trong khi phép kiểm Host nhận cả localhost. Origin phải khớp đúng Host
    đã được kiểm của chính request đó."""
    host = f"localhost:{ui.port}"
    body = urlencode({"csrf": ui.auth.csrf(ui.session), "intent": "Tạo VLAN 30"})
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://{host}"}
    assert ui.request("POST", "/run", body=body, headers=form, host=host)[0] == 303
    ui.wait_idle()
    # Đối chứng âm: Host và Origin lệch nhau (cookie bị mang sang trang khác) vẫn bị chặn.
    form["Origin"] = f"http://127.0.0.1:{ui.port}"
    assert ui.request("POST", "/run", body=body, headers=form, host=host)[0] == 403
    form["Origin"] = "http://localhost.evil.example"
    assert ui.request("POST", "/run", body=body, headers=form, host=host)[0] == 403


def test_internal_error_is_logged_to_stderr_without_the_request_url(ui, monkeypatch, capsys):
    """Trang 500 giấu chi tiết (đúng), nhưng server phải ghi traceback ra stderr để người vận hành chẩn đoán;
    không ghi URL (URL /login mang token)."""

    def boom(*_a, **_k):
        raise RuntimeError("vỡ-ở-views")

    monkeypatch.setattr(views, "dashboard", boom)
    status, _, page = ui.request("GET", "/?scenario=secret-in-url")
    assert status == 500 and "vỡ-ở-views" not in page
    err = capsys.readouterr().err
    assert "RuntimeError: vỡ-ở-views" in err and "secret-in-url" not in err


def test_real_mode_without_api_key_blocks_running_before_spawning(tmp_path, monkeypatch):
    """Chế độ thật mà terminal chưa có DEEPSEEK_API_KEY: runner sẽ thoát ngay với lỗi cấu hình và trang chỉ
    hiện "thoát mà không ghi result.json". Phải chặn từ UI với lý do rõ, trước khi sinh process."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app, launcher = _app(tmp_path)
    assert "DEEPSEEK_API_KEY" in (app.run_blocker() or "")
    with pytest.raises(UiError) as exc:
        app.start_run(intent="Tạo VLAN 30")
    assert exc.value.status == 409 and launcher.calls == []
    # Đối chứng âm: có key → chạy.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    assert app.run_blocker() is None


def test_runner_log_tail_is_shown_redacted_when_runner_exits_without_result(tmp_path):
    """Runner thoát không có result.json: trang phải hiện đuôi log của runner (đã che secret) để biết vì sao,
    thay vì chỉ một dòng chung chung."""
    secret = "S3cr3t-TACACS-KEY"  # noqa: S105 — giá trị giả để kiểm che secret

    class Crasher(FakeLauncher):
        def __call__(self, argv, env, log_path):
            self.calls.append((argv, env))
            log_path.write_text(f"Traceback...\nDshSetupError: thiếu file {secret}\n", encoding="utf-8")
            return _ExitOnly(3)

    class _ExitOnly:
        def __init__(self, code):
            self.code = code

        def wait(self, timeout=None):
            return self.code

        def kill(self):
            pass

    app, _ = _app(tmp_path, launcher=Crasher())
    app.secrets.add(secret)
    run_id = app.start_run(intent="Tạo VLAN 30")
    deadline = time.time() + 10
    while app.active and time.time() < deadline:
        time.sleep(0.02)
    view = app.run_view(run_id)
    assert view["exited_without_result"] and view["exit_code"] == 3
    assert "DshSetupError" in view["runner_log"] and secret not in view["runner_log"]
    page = views.run_page(app, view, "csrf")
    assert "DshSetupError" in page and secret not in page


def test_ui_server_reports_a_busy_port_instead_of_a_traceback(tmp_path, capsys):
    """Khởi động lần hai trên cùng cổng (server cũ còn chạy) trước đây in traceback OSError; nay thoát mã 2
    với lời chỉ dẫn."""
    import socket
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import ui_server

    hold = socket.socket()
    hold.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    hold.bind(("127.0.0.1", 0))
    hold.listen(1)
    port = hold.getsockname()[1]
    try:
        code = ui_server.main(
            [
                "--config",
                str(ROOT / "config" / "fake.toml"),
                "--context",
                str(CAMPUS),
                "--role",
                "network_admin",
                "--level",
                "B4",
                "--reviewer",
                "T",
                "--out",
                str(tmp_path / "o"),
                "--port",
                str(port),
            ]  # fmt: skip
        )
    finally:
        hold.close()
    assert code == 2 and "--port" in capsys.readouterr().err


# ------------------------------------------------------------------ bước intent grounding (D-061)
def _grounding_state(result: dict, grounding: dict | None) -> tuple[str, str]:
    view = {"running": False, "result": result, "grounding": grounding}
    steps = {name: (cls, state) for name, cls, state in views.pipeline_steps(view, tuple(result["controls"]))}
    return steps["Intent grounding"]


B3_OK = {
    "status": "ok",
    "verdict": "accept",
    "stopped_by": None,
    "level": "B3",
    "controls": ["i", "ii", "iii", "v"],
}


def test_grounding_step_reads_the_trace_event():
    assert _grounding_state(B3_OK, {"enforced": True, "violations": 0}) == ("is-pass", "đã qua")
    blocked = {**B3_OK, "verdict": "clarify", "stopped_by": "intent_grounding"}
    cls, state = _grounding_state(blocked, {"enforced": True, "violations": 2})
    assert cls == "is-block" and "2" in state
    b2 = {**B3_OK, "level": "B2", "controls": ["i", "ii", "v"]}
    cls, state = _grounding_state(b2, {"enforced": False, "violations": 1})
    assert cls == "is-off" and "shadow" in state
    b0 = {**B3_OK, "level": "B0", "controls": ["v"]}
    assert _grounding_state(b0, None) == ("is-off", "tắt ở mức này")
    # Dừng trước CBA ở mức có grounding: chưa tới, không phải "đã qua".
    early = {**B3_OK, "verdict": "reject", "stopped_by": "context_validation"}
    assert _grounding_state(early, None) == ("is-pending", "chưa tới")


def test_grounding_with_violations_never_shown_as_passed():
    """Event `grounding` cưỡng chế có vi phạm nhưng lượt chạy kết thúc ERROR (không dừng ở grounding):
    bước này KHÔNG được báo "đã qua"."""
    err = {**B3_OK, "status": "error", "verdict": None, "error_code": "HARNESS_INTERNAL_ERROR"}
    cls, state = _grounding_state(err, {"enforced": True, "violations": 3})
    assert cls == "is-block" and "3" in state and state != "đã qua"
    # Đối chứng: cùng lượt ERROR, event không vi phạm → bước grounding đã chạy và cho qua.
    assert _grounding_state(err, {"enforced": True, "violations": 0}) == ("is-pass", "đã qua")


def test_grounding_block_stops_the_steps_after_it():
    view = {
        "running": False,
        "result": {**B3_OK, "verdict": "clarify", "stopped_by": "intent_grounding"},
        "grounding": {"enforced": True, "violations": 1},
    }
    steps = {name: cls for name, cls, _ in views.pipeline_steps(view, ("i", "ii", "iii", "v"))}
    assert steps["(iii) Gate G1–G6"] == "is-pending" and steps["Change package"] == "is-warn"


def test_run_view_reads_only_enforced_and_count_from_the_grounding_event(tmp_path):
    trace = Trace(tmp_path / "trace.jsonl", actor="runner")
    trace.write(
        "grounding", enforced=True, violations=[{"index": 0, "field": "vlan_id", "value": "<b>10</b>"}]
    )
    records = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert UiApp._grounding(records) == {"enforced": True, "violations": 1}
    assert UiApp._grounding([]) is None


GROUNDING_PASSED = (
    "<li class='is-pass'><span class='step-name'>Intent grounding</span>"
    "<span class='step-state'>đã qua</span>"
)


def test_run_page_shows_the_grounding_step_when_the_trace_has_the_event(ui):
    ui.launcher.grounding = {"enforced": True, "violations": []}
    page = ui.request("GET", f"/run/{ui.run()}")[2]
    assert GROUNDING_PASSED in page
    # Đối chứng: cùng lượt chạy nhưng trace không có event `grounding` → bước không được báo "đã qua".
    ui.launcher.grounding = None
    plain = ui.request("GET", f"/run/{ui.run()}")[2]
    assert "Intent grounding" in plain and GROUNDING_PASSED not in plain


# ------------------------------------------------------------------ trạng thái chuỗi/chữ ký của trace (D-060)
@pytest.mark.parametrize(
    ("status", "review", "tag"),
    [
        ("bad_signature", 409, "chữ ký HMAC sai"),
        ("broken", 409, "chuỗi hash không hợp lệ"),
        ("ok", 303, "chữ ký hợp lệ"),  # đối chứng: chuỗi đã xác thực thì duyệt được
        ("unsigned", 303, "trace chưa ký"),
    ],
)
def test_chain_status_tag_and_review_follow_verify_chain(ui, monkeypatch, status, review, tag):
    """UI không giữ khoá nên thực tế chỉ thấy "unsigned"/"broken"; "bad_signature" vẫn phải chặn duyệt (fail-
    closed) nếu `verify_chain` trả về nó, và "ok" là đối chứng duyệt được."""
    from harness_agent.ui import app as app_module

    run_id = ui.run()
    monkeypatch.setattr(app_module, "verify_chain", lambda path, key=None: status)
    view = ui.app.run_view(run_id)
    assert view["chain_status"] == status
    page = ui.request("GET", f"/run/{run_id}")[2]
    assert tag in page
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == review


def test_chain_status_is_missing_without_a_trace(ui):
    run_id = ui.run()
    (ui.app.out / run_id / "trace.jsonl").unlink()
    assert ui.app.run_view(run_id)["chain_status"] == "missing"
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 409


# ------------------------------------------------------------------ lệnh cha-apply sau khi duyệt (D-064)
def test_approved_run_shows_the_dry_run_apply_command_and_no_apply_post(ui):
    run_id = ui.run()
    before = ui.request("GET", f"/run/{run_id}")[2]
    assert "cha-apply" not in before  # đối chứng: chưa duyệt thì chưa có lệnh
    assert ui.post(f"/run/{run_id}/review", {"decision": "approve", "reason": ""})[0] == 303
    page = ui.request("GET", f"/run/{run_id}")[2]
    run_dir = str(ui.app.out / run_id)
    arg = views.shell_arg(run_dir)
    assert arg in (run_dir, f'"{run_dir}"')  # đường dẫn tmp của máy test không cần placeholder
    expected = f"cha-apply --run-dir {views.esc(arg)} --device SW2 --inventory &lt;inventory.json&gt;"
    assert expected in page
    assert f"--commit --confirm {run_id}" in page  # chỉ là ghi chú để chạy ở terminal
    assert "<form" not in page  # sau khi duyệt, trang không còn form POST nào
    # UI không có đường POST nào chạy apply.
    for path in (f"/run/{run_id}/apply", "/apply"):
        assert ui.post(path, {"device": "SW2"})[0] == 404


def test_rejected_run_shows_no_apply_command(ui):
    run_id = ui.run()
    assert ui.post(f"/run/{run_id}/review", {"decision": "reject", "reason": "sai cổng"})[0] == 303
    assert "cha-apply" not in ui.request("GET", f"/run/{run_id}")[2]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/srv/runs/20261017-abc", "/srv/runs/20261017-abc"),
        (r"D:\repo\runs\ui\x", r"D:\repo\runs\ui\x"),
        ("C:/Users/Ho Le/runs/x", '"C:/Users/Ho Le/runs/x"'),
        ('/srv/a"b', "<run-dir>"),
        ("/srv/$HOME/x", "<run-dir>"),
        ("/srv/a`b", "<run-dir>"),
        ("/srv/a!b", "<run-dir>"),
        ("C:\\x y\\", "<run-dir>"),
        ("C:/x%PATH%/y", "<run-dir>"),
    ],
)
def test_apply_command_quotes_the_run_dir_or_uses_a_placeholder(path, expected):
    assert views.shell_arg(path) == expected
