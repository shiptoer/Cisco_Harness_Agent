"""Giao diện vận hành chạy runner THẬT bằng process con (fake_dsh, D-057): accept → duyệt → xuất .cfg;
reject ở gate được hiển thị đúng bước. Sinh process nên mang marker slow.
"""

from __future__ import annotations

import http.client
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from harness_agent.harness.trace import verify_chain
from harness_agent.levels import Level
from harness_agent.ui import views
from harness_agent.ui.app import UiApp, UiSettings
from harness_agent.ui.web import Auth, make_server

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]


def _app(tmp_path: Path, level: Level = Level.B4) -> UiApp:
    return UiApp(
        UiSettings(
            config=ROOT / "config" / "fake.toml",
            context=ROOT / "data" / "contexts" / "campus-small-01.json",
            role="network_admin",
            level=level,
            reviewer="E2E",
            out=tmp_path / "out",
        )
    )


def _wait(app: UiApp) -> None:
    deadline = time.time() + 240
    while app.active and time.time() < deadline:
        time.sleep(0.2)
    assert app.active is None, "lượt chạy không xong trong 240 giây"


def test_accept_review_export_with_a_real_runner_subprocess(tmp_path: Path):
    app = _app(tmp_path)
    run_id = app.start_run(scenario="clean-vlan30")
    _wait(app)
    view = app.run_view(run_id)
    assert (view["result"]["status"], view["result"]["verdict"]) == ("ok", "accept")
    assert view["chain_ok"] and view["reviewable"] and "SW2" in view["candidates"]
    assert view["snapshot"]["evaluated_at"] == app._index()[run_id]["evaluated_at"]
    assert [cls for _, cls, _ in views.pipeline_steps(view, tuple(view["result"]["controls"]))] == [
        "is-pass"
    ] * 6  # (i), (ii), intent grounding, (iii), (iv), package
    app.record_review(run_id, "approve", "")
    assert verify_chain(app.out / run_id / "trace.jsonl") == "unsigned"
    assert app.export_cfg(run_id, "SW2").startswith(b"vlan 30")


def test_whole_flow_over_http_with_a_real_runner_subprocess(tmp_path: Path):
    """Chuỗi request như trình duyệt: login → chọn kịch bản → chạy → trang tiến trình → duyệt → xuất."""
    app = _app(tmp_path)
    auth = Auth("e2e-token")
    server = make_server(app, auth, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cookie = ""

    def request(method: str, path: str, form: dict[str, str] | None = None) -> tuple[int, dict, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers = {"Host": f"127.0.0.1:{port}", "Cookie": cookie}
        body = None
        if form is not None:
            body = urlencode({"csrf": auth.csrf(cookie.split("=", 1)[1]), **form})
            headers |= {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
            }
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace"))
        conn.close()
        return out

    try:
        status, headers, _ = request("GET", "/login?token=e2e-token")
        assert status == 303
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, page = request("GET", "/?scenario=clean-vlan30")
        assert status == 200 and "readonly" in page and "không do intent" in page
        status, headers, _ = request("POST", "/run", {"scenario": "clean-vlan30"})
        assert status == 303
        path = headers["Location"]
        status, _, page = request("GET", path)
        assert status == 200 and 'http-equiv="refresh"' in page  # đang chạy: trang tự tải lại
        _wait(app)
        status, _, page = request("GET", path)
        assert status == 200 and 'http-equiv="refresh"' not in page
        assert "<script" not in page and "/review" in page
        status, _, _ = request("POST", f"{path}/review", {"decision": "approve", "reason": ""})
        assert status == 303
        status, _, page = request("GET", path)
        assert f"{path}/cfg/SW2" in page
        status, headers, body = request("GET", f"{path}/cfg/SW2")
        assert status == 200 and body.startswith("vlan 30") and "attachment" in headers["Content-Disposition"]
        assert verify_chain(app.out / path.rsplit("/", 1)[1] / "trace.jsonl") == "unsigned"
    finally:
        server.shutdown()
        server.server_close()


def test_gate_reject_is_shown_at_its_step(tmp_path: Path):
    app = _app(tmp_path, Level.B3)
    run_id = app.start_run(scenario="adv-protected-uplink")
    _wait(app)
    view = app.run_view(run_id)
    assert (view["result"]["verdict"], view["result"]["stopped_by"]) == ("reject", "G4_blast_radius")
    steps = {name: cls for name, cls, _ in views.pipeline_steps(view, tuple(view["result"]["controls"]))}
    assert steps["(iii) Gate G1–G6"] == "is-block" and steps["(iv) Sensors"] == "is-off"
    assert view["gate"]["first_failed"] == "G4_blast_radius"
    assert not view["reviewable"]
