"""Sensors (iv): syntax + CIS trên cấu hình đã render (task 3.4), và cách runner dùng chúng.

Mỗi kiểm tra bảo mật có đối chứng âm: cấu hình sạch qua, cấu hình cài lỗi bị bắt đúng mã.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from registry_samples import VALID_ARGS, call

from harness_agent.harness.context_store import ContextStore
from harness_agent.models import parse_tool_call
from harness_agent.runner import pipeline
from harness_agent.runner.dsh import LocalConfig
from harness_agent.runner.pipeline import run_case
from harness_agent.sensors import run_sensors
from harness_agent.sensors.cis import RULES as CIS_RULES
from harness_agent.sensors.cis import check_cis
from harness_agent.sensors.syntax import check_syntax
from harness_agent.tools.registry.render import render_by_device
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
PLATFORMS = {"R1": "iosv", "SW1": "iosvl2", "SW2": "iosvl2"}


def _render_all() -> dict[str, str]:
    """Render đúng một thao tác mẫu của MỖI tool trong registry (đối chứng dương cho sensors)."""
    calls = [parse_tool_call(call(t)) for t in TOOL_REGISTRY]
    return render_by_device([(c.tool, c.parsed) for c in calls])


# ------------------------------------------------------------------ syntax
def test_every_tool_template_passes_syntax_sensor():
    configs = _render_all()
    assert len(configs) >= 1
    for dev, text in configs.items():
        assert check_syntax(dev, text, PLATFORMS.get(dev, "iosvl2")) == [], (dev, text)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("username evil privilege 15 secret x\nend\n", "SYN_UNKNOWN_COMMAND"),
        ("switchport mode access\nend\n", "SYN_WRONG_MODE"),  # lệnh interface ở mode global
        ("vlan 0\n name X\nexit\nend\n", "SYN_BAD_VALUE"),
        ("interface GigabitEthernet0/1\n ip address 10.0.0.1 255.0.255.0\nexit\nend\n", "SYN_BAD_VALUE"),
        (
            "interface GigabitEthernet0/1\n switchport trunk allowed vlan 10,5000\nexit\nend\n",
            "SYN_BAD_VALUE",
        ),
        ("ip route 10.0.0.0 255.255.255.0 999.0.0.1\nend\n", "SYN_BAD_VALUE"),
        ("vlan 30\n name FINANCE\nexit\n", "SYN_MISSING_END"),
        ("end\nvlan 30\n", "SYN_AFTER_END"),
        ("vlan 30\n name FIN\x07ANCE\nexit\nend\n", "SYN_CONTROL_CHAR"),
        ("interface GigabitEthernet0/1\n description " + "x" * 241 + "\nexit\nend\n", "SYN_LINE_TOO_LONG"),
    ],
)
def test_syntax_sensor_catches(text: str, code: str):
    codes = {f.code for f in check_syntax("SW2", text, "iosvl2")}
    assert code in codes, codes


def test_syntax_sensor_platform_aware():
    text = "vlan 30\n name X\nexit\nend\n"
    assert check_syntax("SW2", text, "iosvl2") == []
    assert {f.code for f in check_syntax("R1", text, "iosv")} == {"SYN_PLATFORM"}


def test_renderer_closes_submodes_before_global_commands():
    """D-019: `exit` sau mỗi thao tác kết thúc trong sub-mode, để lệnh toàn cục kế tiếp đứng đúng mode."""
    calls = [
        parse_tool_call(call("set_interface_state", device="R1", interface="GigabitEthernet0/2")),
        parse_tool_call(call("add_static_route", device="R1", prefix="10.20.0.0/16", next_hop="10.0.12.2")),
    ]
    text = render_by_device([(c.tool, c.parsed) for c in calls])["R1"]
    assert text.split("\n")[:4] == [
        "interface GigabitEthernet0/2",
        " no shutdown",
        "exit",
        "ip route 10.20.0.0 255.255.0.0 10.0.12.2",
    ]
    assert check_syntax("R1", text, "iosv") == []
    # Đối chứng âm: bỏ `exit` thì sensor syntax bắt được.
    assert "SYN_WRONG_MODE" in {f.code for f in check_syntax("R1", text.replace("exit\n", ""), "iosv")}


# ------------------------------------------------------------------ CIS
def test_every_tool_template_passes_cis_sensor():
    store = ContextStore.load(CAMPUS)
    for dev, text in _render_all().items():
        fails = [f for f in check_cis(dev, text, store.secret_values()) if f.severity == "fail"]
        assert fails == [], (dev, [f.code for f in fails])


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("interface GigabitEthernet1/2\n switchport trunk native vlan 1\nexit\nend\n", "L2_NATIVE_VLAN_1"),
        ("interface GigabitEthernet1/2\n switchport access vlan 1\nexit\nend\n", "L2_ACCESS_VLAN_1"),
        ("interface GigabitEthernet1/2\n switchport mode dynamic desirable\nexit\nend\n", "L2_DTP_DYNAMIC"),
        ("username evil privilege 15 secret x\nend\n", "CIS_1_2_1_PRIV15_USER"),
        ("enable password cisco\nend\n", "CIS_1_4_1_ENABLE_PASSWORD"),
        ("tacacs-server key supersecret\nend\n", "CIS_1_4_PLAINTEXT_SECRET"),
        ("ip http server\nend\n", "CIS_2_1_1_1_HTTP"),
        ("no aaa new-model\nend\n", "CIS_1_1_1_NO_AAA_NEW_MODEL"),
        ("aaa authentication login X none\nend\n", "CIS_1_1_2_LOGIN_NONE"),
        ("interface GigabitEthernet0/1\n ip proxy-arp\nexit\nend\n", "CIS_3_1_2_PROXY_ARP"),
        ("line vty 0 4\n transport input telnet\nexit\nend\n", "CIS_1_2_2_TRANSPORT_TELNET"),
    ],
)
def test_cis_sensor_catches(text: str, code: str):
    found = {f.code for f in check_cis("SW2", text) if f.severity == "fail"}
    assert code in found, found


def test_cis_warn_does_not_block():
    text = "aaa new-model\naaa authentication login X group tacacs+\nend\n"
    report = run_sensors({"R1": text}, PLATFORMS)
    assert report.blocking_sensor is None
    assert {w.code for w in report.warnings} == {"CIS_1_1_2_LOGIN_NO_FALLBACK"}


def test_cis_placeholder_is_ok_but_secret_value_is_not():
    ok = "aaa new-model\n! TACACS+ key: <SECRET:tacacs_key>\nend\n"
    assert [f for f in check_cis("R1", ok, ["CANARY-x-1234"]) if f.severity == "fail"] == []
    leaked = "aaa new-model\n! TACACS+ key: CANARY-x-1234\nend\n"
    assert {f.code for f in check_cis("R1", leaked, ["CANARY-x-1234"])} == {"CIS_1_4_SECRET_VALUE_IN_CONFIG"}


def test_cis_rule_codes_unique_and_referenced():
    codes = [r.code for r in CIS_RULES]
    assert len(codes) == len(set(codes))
    assert all(r.reference for r in CIS_RULES)


# ------------------------------------------------------------------ report
def test_report_marks_batfish_skipped_not_passed():
    report = run_sensors({"SW2": "vlan 30\n name X\nexit\nend\n"}, PLATFORMS)
    assert report.sensors == {"syntax": "ran", "batfish": "skipped", "cis": "ran"}
    assert report.blocking_sensor is None and report.as_dict()["n_fail"] == 0


def test_blocking_sensor_is_first_in_run_order():
    bad = "switchport trunk native vlan 1\nend\n"  # syntax (sai mode) + CIS cùng bắt
    report = run_sensors({"SW2": bad}, PLATFORMS)
    assert report.blocking_sensor == "syntax"
    assert {f.sensor for f in report.failures} == {"syntax"}  # CIS chỉ xét theo mode, dòng này ở global


# ------------------------------------------------------------------ runner
def _cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
# Intent neo được mọi giá trị của thao tác trong kịch bản: grounding (D-061) cưỡng chế ở B3/B4.
INTENT = "Tạo VLAN 30 tên FINANCE trên SW2"
CLEAN = {
    "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
    "tsa": "auto",
}


def _run(tmp_path: Path, monkeypatch, level: str):
    """`level` bắt buộc: từ D-049 mặc định là B3 (sensors chỉ shadow); test cưỡng chế sensors phải ghi
    rõ B4."""
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(CLEAN), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path), CAMPUS, INTENT, "network_admin", tmp_path / "runs", "case", level)


@pytest.mark.slow
def test_runner_writes_sensor_report_on_accept(tmp_path: Path, monkeypatch):
    r = _run(tmp_path, monkeypatch, level="B4")
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    report = json.loads((tmp_path / "runs" / "case" / "sensors.json").read_text(encoding="utf-8"))
    assert (
        report["sensors"] == {"syntax": "ran", "batfish": "skipped", "cis": "ran", "intent": "ran"}
        and report["n_fail"] == 0
    )
    assert "Sensors: syntax=ran, batfish=skipped, cis=ran" in (
        tmp_path / "runs" / "case" / "change_package.md"
    ).read_text(encoding="utf-8")


@pytest.mark.slow
def test_runner_rejects_when_sensor_fails_and_writes_no_package(tmp_path: Path, monkeypatch):
    """Đối chứng âm cho control (iv): renderer bị thay bằng bản sinh native VLAN 1 → sensors chặn, không có
    change package, verdict reject với stopped_by=sensor_cis (syntax đúng, CIS sai)."""
    bad = "interface GigabitEthernet1/2\n switchport trunk native vlan 1\nexit\nend\n"
    monkeypatch.setattr(pipeline, "render_by_device", lambda ops: {"SW2": bad})
    r = _run(tmp_path, monkeypatch, level="B4")
    assert (r.status, r.verdict, r.stopped_by) == ("ok", "reject", "sensor_cis"), r.reason
    assert "L2_NATIVE_VLAN_1" in r.reason and r.error_code is None
    run_dir = tmp_path / "runs" / "case"
    assert not (run_dir / "candidate").exists() and not (run_dir / "change_package.md").exists()
    assert (run_dir / "sensor_rejected" / "SW2.cfg").read_text(encoding="utf-8") == bad


@pytest.mark.slow
def test_runner_syntax_sensor_catches_template_regression(tmp_path: Path, monkeypatch):
    """Template sinh lệnh ngoài từ vựng (vd ai đó sửa vlan_create.j2 thêm `username`) → sensor_syntax."""
    bad = "vlan 30\n name FINANCE\nexit\nusername backdoor privilege 15 secret x\nend\n"
    monkeypatch.setattr(pipeline, "render_by_device", lambda ops: {"SW2": bad})
    r = _run(tmp_path, monkeypatch, level="B4")
    assert (r.verdict, r.stopped_by) == ("reject", "sensor_syntax")


def test_valid_args_cover_every_tool():
    assert set(VALID_ARGS) == set(TOOL_REGISTRY)


# ------------------------------------------------------------------ hồi quy review 01/10
@pytest.mark.parametrize(
    "line",
    [
        " description Key card reader",
        " description Password reset kiosk",
    ],
)
def test_cis_does_not_flag_words_in_free_text(line: str):
    """Trước đây mọi dòng có chữ key/secret/password đều FAIL → false reject trên tập clean."""
    text = f"interface GigabitEthernet0/2\n{line}\nexit\nend\n"
    assert [f for f in check_cis("SW2", text) if f.severity == "fail"] == []


def test_cis_checks_every_credential_value_on_a_line():
    text = "username a secret <SECRET:a> password realpw\nend\n"
    assert "CIS_1_4_PLAINTEXT_SECRET" in {f.code for f in check_cis("R1", text)}


@pytest.mark.parametrize(
    ("ace", "ok"),
    [
        (" 10 permit tcp any any eq 443", True),
        (" 10 permit ip any any eq 80", False),  # toán tử cổng với ip: IOS từ chối
        (" 10 permit tcp any any eq 99999", False),
        (" 10 permit udp any host 10.0.0.1 eq 53", True),
    ],
)
def test_syntax_acl_port_rules(ace: str, ok: bool):
    text = f"ip access-list extended X\n{ace}\nexit\nend\n"
    assert (check_syntax("SW1", text, "iosvl2") == []) is ok


def test_syntax_open_vlan_range_rejected():
    text = "interface GigabitEthernet0/1\n switchport trunk allowed vlan 10-\nexit\nend\n"
    assert "SYN_BAD_VALUE" in {f.code for f in check_syntax("SW2", text, "iosvl2")}
