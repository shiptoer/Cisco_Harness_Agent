"""`cha-check` (`python -m harness_agent.check`): kiểm một file cấu hình bất kỳ với context, không LLM.

Mã thoát: 0 sạch, 1 có finding, 2 lỗi dùng (file thiếu, context sai contract, role lạ). Secret của context
không bao giờ được in ra stdout/stderr (có đối chứng âm: gỡ bước che thì test bắt được).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import harness_agent.check as check_mod
from harness_agent.check.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
# enable_secret của SW1 trong campus-small-01 (canary của context, không phải secret thật): đọc từ context để
# test luôn khớp dữ liệu.
SW1_SECRET = next(
    d["secrets"]["enable_secret"]
    for d in json.loads(CAMPUS.read_text(encoding="utf-8"))["devices"]
    if d["hostname"] == "SW1"
)

CLEAN = """vlan 30
 name GUEST
exit
interface GigabitEthernet1/1
 switchport mode access
 switchport access vlan 30
exit
end
"""


def _cfg(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _run_json(capsys, *argv: str) -> tuple[int, dict, str]:
    code = main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


def _findings(doc: dict, device: str) -> list[dict]:
    return doc["devices"][device]["findings"]


# ------------------------------------------------------------------ kết quả chính
def test_clean_config_exits_zero(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN)
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 0 and doc["exit_code"] == 0
    assert _findings(doc, "SW1") == []


def test_risky_line_is_a_critical_finding(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN.replace("end\n", "username backdoor privilege 15\nend\n"))
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 1 and doc["exit_code"] == 1
    risky = [f for f in _findings(doc, "SW1") if f["kind"] == "risky_line"]
    assert len(risky) == 1
    assert risky[0]["line"] == 8 and risky[0]["severity"] == "critical"
    assert "username backdoor" in risky[0]["message"]


def test_protected_interface_reports_the_first_failing_gate_step(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", "interface GigabitEthernet0/0\n description uplink\nexit\nend\n")
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 1
    gate = [f for f in _findings(doc, "SW1") if f["kind"] == "gate"]
    assert [f["code"] for f in gate] == ["G4_blast_radius"]
    assert gate[0]["line"] == 1  # dòng `interface GigabitEthernet0/0`


def test_role_is_replayed_through_the_gate(tmp_path, capsys):
    """Role mặc định security_admin cho qua; viewer thì G3 chặn mọi thao tác ghi."""
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN)
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS), "--role", "viewer")
    assert code == 1
    assert {f["code"] for f in _findings(doc, "SW1") if f["kind"] == "gate"} == {"G3_rbac"}


def test_unparsed_line_has_its_line_number(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", "hostname SW1\n" + CLEAN)
    _, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    unparsed = [f for f in _findings(doc, "SW1") if f["kind"] == "unparsed_line"]
    assert [(f["line"], f["severity"]) for f in unparsed] == [(1, "info")]
    # Sensor syntax (allowlist) độc lập với evaluator: cũng bắt dòng này.
    assert any(f["kind"] == "sensor" and f["code"] == "SYN_UNKNOWN_COMMAND" for f in _findings(doc, "SW1"))


def test_benign_lines_are_not_reported(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", "configure terminal\n" + CLEAN)
    _, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert not [f for f in _findings(doc, "SW1") if f["kind"] == "unparsed_line"]


def test_sensor_findings_carry_their_code(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN.removesuffix("end\n"))
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 1
    assert [f["code"] for f in _findings(doc, "SW1") if f["kind"] == "sensor"] == ["SYN_MISSING_END"]
    assert doc["sensors"]["batfish"] in ("ran", "skipped")


# ------------------------------------------------------------------ secret của context
SECRET_CFG = CLEAN.replace("end\n", f"enable secret {SW1_SECRET}\nend\n")


def test_secret_in_config_reports_line_and_never_prints_the_secret(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", SECRET_CFG)
    code, doc, err = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 1
    hits = [f for f in _findings(doc, "SW1") if f["kind"] == "secret_in_config"]
    assert [(f["line"], f["severity"]) for f in hits] == [(8, "critical")]
    assert SW1_SECRET not in json.dumps(doc, ensure_ascii=False) and SW1_SECRET not in err
    # Dòng rủi ro (`enable secret`) vẫn được báo, với secret đã che.
    risky = [f for f in _findings(doc, "SW1") if f["kind"] == "risky_line"]
    assert risky and "[REDACTED]" in risky[0]["message"]


def test_secret_never_printed_in_text_mode(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", SECRET_CFG)
    code = main([str(cfg), "--context", str(CAMPUS)])
    out = capsys.readouterr()
    assert code == 1
    assert "secret_in_config" in out.out
    assert SW1_SECRET not in out.out and SW1_SECRET not in out.err


def test_negative_control_removing_redaction_leaks_the_secret(tmp_path, capsys, monkeypatch):
    """Đối chứng âm: gỡ bước che (điểm che duy nhất) thì secret lọt ra stdout, nên hai test trên có nghĩa."""
    monkeypatch.setattr(check_mod, "redact_value", lambda value, secrets: value)
    cfg = _cfg(tmp_path, "SW1.cfg", SECRET_CFG)
    main([str(cfg), "--context", str(CAMPUS), "--json"])
    assert SW1_SECRET in capsys.readouterr().out


# ------------------------------------------------------------------ lỗi dùng → mã 2
def test_missing_file_exits_two(tmp_path, capsys):
    code = main([str(tmp_path / "SW1.cfg"), "--context", str(CAMPUS)])
    out = capsys.readouterr()
    assert code == 2 and "SW1.cfg" in out.err and out.out == ""


def test_missing_file_is_reported_as_missing_even_with_an_unknown_name(tmp_path, capsys):
    code = main([str(tmp_path / "nope.cfg"), "--context", str(CAMPUS)])
    err = capsys.readouterr().err
    assert code == 2 and "không đọc được file cấu hình" in err and "FileNotFoundError" in err


def test_non_utf8_file_exits_two(tmp_path, capsys):
    cfg = tmp_path / "SW1.cfg"
    cfg.write_bytes(b"hostname \xff\xfe\n")
    assert main([str(cfg), "--context", str(CAMPUS)]) == 2
    assert "UTF-8" in capsys.readouterr().err


def test_invalid_context_exits_two(tmp_path, capsys):
    raw = json.loads(CAMPUS.read_text(encoding="utf-8"))
    raw["devices"][1]["vlans"].append(dict(raw["devices"][1]["vlans"][0]))  # VLAN trùng
    bad = tmp_path / "ctx.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN)
    code = main([str(cfg), "--context", str(bad)])
    out = capsys.readouterr()
    assert code == 2 and "context" in out.err


def test_unreadable_context_exits_two(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN)
    assert main([str(cfg), "--context", str(tmp_path / "none.json")]) == 2
    assert "context" in capsys.readouterr().err


def test_unknown_role_exits_two(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", CLEAN)
    code = main([str(cfg), "--context", str(CAMPUS), "--role", "root"])
    assert code == 2 and "role" in capsys.readouterr().err


def test_device_not_in_context_exits_two(tmp_path, capsys):
    cfg = _cfg(tmp_path, "CORE9.cfg", CLEAN)
    assert main([str(cfg), "--context", str(CAMPUS)]) == 2
    assert "CORE9" in capsys.readouterr().err


def test_device_flag_names_a_single_file(tmp_path, capsys):
    cfg = _cfg(tmp_path, "candidate.cfg", CLEAN)
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS), "--device", "SW1")
    assert code == 0 and list(doc["devices"]) == ["SW1"]


def test_device_flag_with_two_files_exits_two(tmp_path, capsys):
    a = _cfg(tmp_path, "SW1.cfg", CLEAN)
    b = _cfg(tmp_path, "SW2.cfg", CLEAN)
    assert main([str(a), str(b), "--context", str(CAMPUS), "--device", "SW1"]) == 2
    assert "--device" in capsys.readouterr().err


def test_two_files_for_the_same_device_exit_two(tmp_path, capsys):
    a = _cfg(tmp_path, "SW1.cfg", CLEAN)
    (tmp_path / "x").mkdir()
    b = _cfg(tmp_path / "x", "SW1.cfg", CLEAN)
    assert main([str(a), str(b), "--context", str(CAMPUS)]) == 2
    assert "SW1" in capsys.readouterr().err


def test_no_arguments_exits_two(capsys):
    assert main([]) == 2


# ------------------------------------------------------------------ output dạng bảng
def test_text_output_is_a_table_per_device(tmp_path, capsys):
    a = _cfg(tmp_path, "SW1.cfg", "interface GigabitEthernet0/0\n description uplink\nexit\nend\n")
    b = _cfg(tmp_path, "SW2.cfg", CLEAN.replace("GigabitEthernet1/1", "GigabitEthernet1/2"))
    code = main([str(a), str(b), "--context", str(CAMPUS)])
    out = capsys.readouterr().out
    assert code == 1
    headers = [ln for ln in out.splitlines() if ln.startswith("device ")]
    assert headers
    assert [c.strip() for c in headers[0].split("|")] == ["device", "line", "kind", "code", "message"]
    assert "G4_blast_radius" in out and "SW2" in out


# ------------------------------------------------------------------ hardening (review task 7)
def test_hostile_file_stem_equal_to_the_secret_never_reaches_stderr(tmp_path, capsys):
    """Tên file chính là secret của context: thông báo lỗi (device không có trong context) phải được che."""
    cfg = _cfg(tmp_path, f"{SW1_SECRET}.cfg", CLEAN)
    code = main([str(cfg), "--context", str(CAMPUS)])
    out = capsys.readouterr()
    assert code == 2
    assert "[REDACTED]" in out.err
    assert SW1_SECRET not in out.err and SW1_SECRET not in out.out


def test_hostile_file_stem_is_redacted_from_the_report_too(tmp_path, capsys):
    cfg = _cfg(tmp_path, f"{SW1_SECRET}.cfg", CLEAN)
    for extra in ([], ["--json"]):
        code = main([str(cfg), "--context", str(CAMPUS), "--device", "SW1", *extra])
        out = capsys.readouterr()
        assert code == 0
        assert SW1_SECRET not in out.out and SW1_SECRET not in out.err


def test_control_characters_are_escaped_in_text_output(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", "hostname A\x07B\x1b[31mRED\x9b2J\n" + CLEAN)
    main([str(cfg), "--context", str(CAMPUS)])
    out = capsys.readouterr().out
    assert "\x07" not in out and "\x1b" not in out and "\x9b" not in out
    assert "hostname A\\x07B\\x1b[31mRED\\x9b2J" in out  # nhìn thấy được, không bị nuốt


def test_del_and_c1_are_escaped_in_json_output(tmp_path, capsys):
    """JSON chuẩn chỉ bắt buộc escape C0; DEL, C1 (CSI 0x9b là ESC[ một byte) và U+2028/2029 phải được escape
    `\\uXXXX` để `--json` in ra terminal không điều khiển được terminal. Tiếng Việt vẫn đọc được."""
    cfg = _cfg(tmp_path, "SW1.cfg", "hostname A\x7fB\x9b2JC\x85D\u2028E\n" + CLEAN)
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    main([str(cfg), "--context", str(CAMPUS), "--json"])
    raw = capsys.readouterr().out
    assert not any(c in raw for c in "\x7f\x9b\x85\u2028")
    assert "\\u009b" in raw and "\\u007f" in raw and "\\u2028" in raw
    assert "ký tự" in raw  # ensure_ascii=False giữ nguyên tiếng Việt
    # Đối chứng: JSON vẫn hợp lệ và giá trị đọc lại có đúng ký tự gốc (escape không làm mất dữ liệu).
    assert json.loads(raw) == doc and code == 1
    assert any("\x9b" in f["message"] for d in doc["devices"].values() for f in d["findings"])


def test_control_characters_are_escaped_on_stderr(tmp_path, capsys):
    cfg = _cfg(tmp_path, "CORE.cfg", CLEAN)
    assert main([str(cfg), "--context", str(CAMPUS), "--device", "CORE\x07"]) == 2
    err = capsys.readouterr().err
    assert "\x07" not in err and "CORE\\x07" in err


def _ops_cfg(device: str) -> str:
    return {
        "SW1": CLEAN,
        "SW2": CLEAN.replace("GigabitEthernet1/1", "GigabitEthernet1/2"),
        "R1": "interface GigabitEthernet0/2\n description lan\nexit\nend\n",
    }[device]


def test_gate_blast_radius_is_cumulative_across_files(tmp_path, capsys):
    """campus-small-01: max_devices_per_change = 2. Mỗi file riêng đều qua; ba file cùng lúc là một change
    chạm 3 device, nên file thứ ba (R1) bị G4 chặn."""
    paths = {d: _cfg(tmp_path, f"{d}.cfg", _ops_cfg(d)) for d in ("SW1", "SW2", "R1")}
    for d, path in paths.items():
        code, doc, _ = _run_json(capsys, str(path), "--context", str(CAMPUS))
        assert code == 0, (d, doc)
    code, doc, _ = _run_json(capsys, *(str(p) for p in paths.values()), "--context", str(CAMPUS))
    assert code == 1
    assert not _findings(doc, "SW1") and not _findings(doc, "SW2")
    gate = [f for f in _findings(doc, "R1") if f["kind"] == "gate"]
    assert [f["code"] for f in gate] == ["G4_blast_radius"]


def test_secret_is_redacted_in_unparsed_and_sensor_messages(tmp_path, capsys):
    cfg = _cfg(tmp_path, "SW1.cfg", f"hostname {SW1_SECRET}\n" + CLEAN)
    code, doc, err = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert code == 1
    found = _findings(doc, "SW1")
    unparsed = [f for f in found if f["kind"] == "unparsed_line"]
    sensor = [f for f in found if f["kind"] == "sensor" and f["code"] == "SYN_UNKNOWN_COMMAND"]
    assert unparsed and unparsed[0]["message"] == "hostname [REDACTED]"
    assert sensor and "hostname [REDACTED]" in sensor[0]["message"]
    assert [f["line"] for f in found if f["kind"] == "secret_in_config"] == [1]
    assert SW1_SECRET not in json.dumps(doc, ensure_ascii=False) and SW1_SECRET not in err


def test_info_only_result_exits_zero(tmp_path, capsys, monkeypatch):
    """Chỉ có finding mức info (unparsed_line) thì mã thoát 0. Tắt sensor syntax (sensor đó cũng báo dòng lạ ở
    mức fail) để chỉ còn finding info."""
    real = check_mod.run_sensors
    monkeypatch.setattr(check_mod, "run_sensors", lambda c, p, s: real(c, p, s, enabled=("cis",)))
    cfg = _cfg(tmp_path, "SW1.cfg", "hostname SW1\n" + CLEAN)
    code, doc, _ = _run_json(capsys, str(cfg), "--context", str(CAMPUS))
    assert [(f["kind"], f["severity"]) for f in _findings(doc, "SW1")] == [("unparsed_line", "info")]
    assert code == 0 and doc["exit_code"] == 0


def test_exit_code_of_ignores_info_only():
    info = {"line": 1, "kind": "unparsed_line", "code": "UNPARSED", "severity": "info", "message": "x"}
    assert check_mod.exit_code_of({"devices": {"SW1": {"findings": [info]}}}) == 0
    warn = {**info, "kind": "sensor", "severity": "warn"}
    assert check_mod.exit_code_of({"devices": {"SW1": {"findings": [info, warn]}}}) == 1


# ------------------------------------------------------------------ entry point
@pytest.mark.slow
def test_module_entry_point_runs_as_a_process(tmp_path):
    cfg = _cfg(tmp_path, "SW1.cfg", SECRET_CFG)
    proc = subprocess.run(  # noqa: S603 — argv cố định, không qua shell
        [sys.executable, "-m", "harness_agent.check", str(cfg), "--context", str(CAMPUS)],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert b"secret_in_config" in proc.stdout
    assert SW1_SECRET.encode() not in proc.stdout + proc.stderr


def test_pyproject_declares_console_scripts():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cha-check = "harness_agent.check.__main__:main"' in text
    assert 'cha-ui = "harness_agent.ui.__main__:main"' in text


def test_ui_server_script_is_a_thin_wrapper():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ui_server
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    from harness_agent.ui.__main__ import main as ui_main

    assert ui_server.main is ui_main
