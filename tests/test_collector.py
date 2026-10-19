"""Collector (D-047): chạy OFFLINE trên output `show` giả dựng từ cấu hình nền của lab smb-01. Không SSH.

Ba nhóm:
1. Khớp với thiết kế của lab. Số liệu kỳ vọng được chép TAY từ `lab/smb-01/baseline-configs/README-smb-01.md`
   (mục 2–6), không sinh từ parser: tỷ lệ khớp theo từng loại phải là 100%, không thừa.
2. Giới hạn an toàn, mỗi cái có đối chứng âm: lệnh ngoài allowlist bị từ chối; không file output nào chứa giá
   trị secret (trong khi input có); mật khẩu chỉ lấy từ biến môi trường.
3. Contract: kết quả qua contract + invariants; thứ contract chưa mô tả được nằm ở `.meta.json`.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness_agent.collector import __main__ as collector_cli
from harness_agent.collector import fetch, parse, synth
from harness_agent.collector.__main__ import main
from harness_agent.collector.build import (
    DEFAULT_TTL_SECONDS,
    build_context,
    collector_provenance,
    load_policy,
    validate_only,
    with_provenance,
    write_context,
)
from harness_agent.collector.fetch import ALLOWED_COMMANDS, CollectorError, check_command, load_inventory
from harness_agent.models import NetworkContext
from harness_agent.models.context import MAX_TTL_SECONDS
from harness_agent.runner.context_check import context_violations

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "lab" / "smb-01" / "baseline-configs"
INVENTORY = ROOT / "lab" / "smb-01" / "inventory.json"
POLICY = ROOT / "data" / "contexts" / "smb-01.policy.json"
NOW = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
PROVENANCE = collector_provenance(NOW, 86400)

# ---- Kỳ vọng, chép tay từ README-smb-01.md ----
DEVICES = ["R-EDGE1", "R-BR1", "CORE1", "ACC1", "ACC2", "ACC3", "ACC-BR1"]  # mục 3; ISP không quản lý
VLANS = {  # mục 4 và phần "Ở đâu"
    "CORE1": {10: "STAFF", 20: "SALES", 30: "FINANCE", 40: "HR", 50: "SERVERS", 60: "GUEST", 99: "MGMT"},
    "ACC1": {10: "STAFF", 20: "SALES", 60: "GUEST", 99: "MGMT"},
    "ACC2": {30: "FINANCE", 40: "HR", 99: "MGMT"},
    "ACC3": {50: "SERVERS", 99: "MGMT"},
    "ACC-BR1": {110: "BR-STAFF", 199: "BR-MGMT"},
}
ROUTES = {  # mục 5
    ("R-EDGE1", "10.10.0.0/16", "10.255.0.2"),
    ("R-EDGE1", "10.20.0.0/16", "10.255.0.6"),
    ("R-EDGE1", "10.255.0.8/30", "10.255.0.6"),
    ("R-EDGE1", "0.0.0.0/0", "203.0.113.2"),
    ("CORE1", "0.0.0.0/0", "10.255.0.1"),
    ("R-BR1", "10.20.0.0/16", "10.255.0.10"),
    ("R-BR1", "0.0.0.0/0", "10.255.0.5"),
    ("ACC-BR1", "0.0.0.0/0", "10.255.0.9"),
}
ACLS = {("CORE1", "ACL-GUEST-IN"), ("CORE1", "ACL-SERVERS-OUT")} | {
    (d, "ACL-MGMT-VTY") for d in DEVICES
}  # mục 6
LINKS = {  # mục 2
    frozenset({("R-EDGE1", "Ethernet0/0"), ("CORE1", "Ethernet0/0")}),
    frozenset({("R-EDGE1", "Ethernet0/2"), ("R-BR1", "Ethernet0/0")}),
    frozenset({("R-BR1", "Ethernet0/1"), ("ACC-BR1", "Ethernet0/0")}),
    frozenset({("CORE1", "Ethernet0/1"), ("ACC1", "Ethernet0/0")}),
    frozenset({("CORE1", "Ethernet0/2"), ("ACC2", "Ethernet0/0")}),
    frozenset({("CORE1", "Ethernet0/3"), ("ACC3", "Ethernet0/0")}),
}
MGMT_IP = {  # mục 3
    "R-EDGE1": ("Ethernet0/3", "192.168.137.11/24"),
    "R-BR1": ("Ethernet0/3", "192.168.137.12/24"),
    "CORE1": ("Ethernet1/3", "192.168.137.21/24"),
    "ACC1": ("Ethernet1/3", "192.168.137.31/24"),
    "ACC2": ("Ethernet1/3", "192.168.137.32/24"),
    "ACC3": ("Ethernet1/3", "192.168.137.33/24"),
    "ACC-BR1": ("Ethernet1/3", "192.168.137.34/24"),
}
VRF_MGMT = {"R-EDGE1", "R-BR1", "CORE1", "ACC-BR1"}  # mục 3, cột "VRF MGMT"


@pytest.fixture(scope="module")
def inventory():
    return load_inventory(INVENTORY)


@pytest.fixture(scope="module")
def outputs(inventory):
    return synth.synthesize(BASELINE, inventory)


@pytest.fixture(scope="module")
def built(inventory, outputs):
    return build_context(inventory, outputs, load_policy(POLICY))


@pytest.fixture(scope="module")
def context(built) -> dict:
    return built[0]


@pytest.fixture(scope="module")
def notes(built) -> dict:
    return built[1]


def _device(context: dict, name: str) -> dict:
    return next(d for d in context["devices"] if d["hostname"] == name)


def _rate(found: set, expected: set) -> tuple[float, set, set]:
    """(tỷ lệ khớp = |found ∩ expected| / |expected|, phần thiếu, phần thừa)."""
    return len(found & expected) / len(expected), expected - found, found - expected


# ------------------------------------------------------------ 1. khớp với thiết kế của lab
def test_match_rate_per_kind_is_complete_with_nothing_extra(context: dict):
    found = {
        "device": {d["hostname"] for d in context["devices"]},
        # Tên interface kỳ vọng lấy bằng một regex độc lập trên file nền, không qua parser.
        "interface": {(d["hostname"], i["name"]) for d in context["devices"] for i in d["interfaces"]},
        "vlan": {(d["hostname"], v["vlan_id"], v["name"]) for d in context["devices"] for v in d["vlans"]},
        "route": {
            (d["hostname"], r["prefix"], r["next_hop"])
            for d in context["devices"]
            for r in d["static_routes"]
        },
        "acl": {(d["hostname"], name) for d in context["devices"] for name in d["acls"]},
        "aaa": {d["hostname"] for d in context["devices"] if d.get("aaa", {}).get("new_model")},
        "link": {
            frozenset({(x["a_device"], x["a_interface"]), (x["b_device"], x["b_interface"])})
            for x in context["links"]
        },
    }
    expected = {
        "device": set(DEVICES),
        "interface": {
            (name, m.group(1))
            for name in DEVICES
            for m in re.finditer(
                r"(?m)^interface (\S+)$", (BASELINE / f"{name}.cfg").read_text(encoding="utf-8")
            )
        },
        "vlan": {(dev, vid, name) for dev, vlans in VLANS.items() for vid, name in vlans.items()}
        | {(dev, 999, "NATIVE-UNUSED") for dev in VLANS},
        "route": ROUTES,
        "acl": ACLS,
        "aaa": set(DEVICES),
        "link": LINKS,
    }
    assert (len(expected["interface"]), len(expected["vlan"])) == (64, 23)
    report = {kind: _rate(found[kind], expected[kind]) for kind in expected}
    assert {kind: rate for kind, (rate, _, _) in report.items()} == dict.fromkeys(expected, 1.0), report
    assert all(not missing and not extra for _, missing, extra in report.values()), report


def test_device_fields_from_inventory_and_from_the_device(context: dict):
    by_name = {d["hostname"]: d for d in context["devices"]}
    assert {n: d["role"] for n, d in by_name.items()} == {
        "R-EDGE1": "router",
        "R-BR1": "router",
        "CORE1": "distribution_switch",
        "ACC1": "access_switch",
        "ACC2": "access_switch",
        "ACC3": "access_switch",
        "ACC-BR1": "access_switch",
    }
    # `ip routing`: router, CORE1 và ACC-BR1 (switch L3 chi nhánh) có; ACC1–3 không.
    assert {n for n, d in by_name.items() if d["ip_routing"]} == {"R-EDGE1", "R-BR1", "CORE1", "ACC-BR1"}
    assert {
        n: d.get("default_gateway") for n, d in by_name.items() if n.startswith("ACC") and n != "ACC-BR1"
    } == {
        "ACC1": "10.10.99.1",
        "ACC2": "10.10.99.1",
        "ACC3": "10.10.99.1",
    }
    assert (context["context_id"], context["domain_name"]) == ("smb-01", "smb.lab")


def test_interface_modes_addresses_and_acl_bindings(context: dict):
    core = {i["name"]: i for i in _device(context, "CORE1")["interfaces"]}
    assert core["Ethernet0/0"] == {
        "name": "Ethernet0/0",
        "description": "UPLINK-R-EDGE1-e0/0",
        "ipv4": "10.255.0.2/30",
        "mode": "routed",
        "enabled": True,
    }
    assert (core["Ethernet0/1"]["mode"], core["Ethernet0/1"]["native_vlan"]) == ("trunk", 999)
    assert core["Ethernet0/1"]["trunk_allowed"] == [10, 20, 60, 99]
    assert core["Ethernet1/0"] == {
        "name": "Ethernet1/0",
        "description": "UNUSED",
        "access_vlan": 999,
        "mode": "unused",
        "enabled": False,
    }
    assert (core["Vlan50"]["ipv4"], core["Vlan50"]["acl_out"]) == ("10.10.50.1/24", "ACL-SERVERS-OUT")
    assert (core["Vlan60"]["acl_in"], core["Vlan60"].get("acl_out")) == ("ACL-GUEST-IN", None)
    acc1 = {i["name"]: i for i in _device(context, "ACC1")["interfaces"]}
    assert (acc1["Ethernet0/1"]["mode"], acc1["Ethernet0/1"]["access_vlan"]) == ("access", 10)
    for name, (port, address) in MGMT_IP.items():
        itf = next(i for i in _device(context, name)["interfaces"] if i["name"] == port)
        assert (itf["mode"], itf["ipv4"]) == ("routed", address), name


def test_acl_entries_and_aaa(context: dict):
    core = _device(context, "CORE1")
    assert core["acls"]["ACL-SERVERS-OUT"][0] == {
        "action": "permit",
        "protocol": "tcp",
        "src": "10.10.30.0 0.0.0.255",
        "dst": "host 10.10.50.20",
        "dst_port": 443,
        "seq": 10,
        "remark": "May chu tai chinh chi cho VLAN FINANCE qua HTTPS",
    }
    assert [(e["seq"], e["action"]) for e in core["acls"]["ACL-GUEST-IN"]] == [
        (10, "deny"),
        (20, "permit"),
        (30, "deny"),
    ]
    # ACL standard được ghi thành `ip <nguồn> any`.
    assert core["acls"]["ACL-MGMT-VTY"] == [
        {"action": "permit", "protocol": "ip", "src": "host 192.168.137.1", "dst": "any", "seq": 10},
        {"action": "permit", "protocol": "ip", "src": "10.10.99.0 0.0.0.255", "dst": "any", "seq": 20},
        {"action": "permit", "protocol": "ip", "src": "10.20.99.0 0.0.0.255", "dst": "any", "seq": 30},
        {"action": "deny", "protocol": "ip", "src": "any", "dst": "any", "seq": 40},
    ]
    for name in DEVICES:
        assert _device(context, name)["aaa"] == {
            "new_model": True,
            "login_lists": {"default": ["group tacacs+", "local"], "CONSOLE": ["local"]},
            "vty_login_list": "default",
            "local_users": [{"username": "netadmin", "privilege": 15, "secret_ref": "local_netadmin"}],
            "tacacs_servers": [{"name": "TAC1", "address": "10.10.50.10", "key_ref": "tacacs_key"}],
        }, name


def test_links_through_management_ports_and_to_unmanaged_devices_are_dropped(notes: dict, outputs: dict):
    reasons = {(x["a_device"], x["b_device"]): x["reason"] for x in notes["links_dropped"]}
    assert reasons[("R-EDGE1", "ISP")] == "neighbor_not_in_inventory"
    assert {pair for pair, why in reasons.items() if why == "management_interface"} == {
        (name, "R-EDGE1") for name in DEVICES if name != "R-EDGE1"
    }
    # Đối chứng âm: các láng giềng đó CÓ trong output CDP mà collector đọc.
    assert (
        "Interface: Ethernet1/3,  Port ID (outgoing port): Ethernet0/3"
        in outputs["CORE1"]["show cdp neighbors detail"]
    )
    assert "Device ID: ISP" in outputs["R-EDGE1"]["show cdp neighbors detail"]


def test_crosscheck_against_the_other_show_commands_finds_no_difference(notes: dict):
    for name in DEVICES:
        check = notes["crosscheck"][name]
        assert check["ip_interface_brief"] is not None
        for part in check.values():
            assert part is None or not any(part.values()), (name, check)
    assert notes["crosscheck"]["CORE1"]["vlan_brief"] is not None
    assert notes["crosscheck"]["R-EDGE1"]["vlan_brief"] is None  # router không có `show vlan brief`


def test_crosscheck_reports_a_difference_without_changing_the_context(inventory, outputs):
    changed = {name: dict(by_command) for name, by_command in outputs.items()}
    changed["ACC1"]["show vlan brief"] += "70   EXTRA                            active    \n"
    context, notes = build_context(inventory, changed, load_policy(POLICY))
    assert notes["crosscheck"]["ACC1"]["vlan_brief"]["only_in_show_vlan"] == [70]
    assert 70 not in [v["vlan_id"] for v in _device(context, "ACC1")["vlans"]]


# ------------------------------------------------------------ 2a. allowlist lệnh
def test_the_allowlist_is_exactly_five_show_commands():
    assert ALLOWED_COMMANDS == (
        "show running-config",
        "show vlan brief",
        "show ip interface brief",
        "show cdp neighbors detail",
        "show interfaces trunk",
    )
    assert [check_command(c) for c in ALLOWED_COMMANDS] == list(ALLOWED_COMMANDS)


@pytest.mark.parametrize(
    "command",
    [
        "configure terminal",
        "write memory",
        "reload",
        "show run",
        "show version",
        "show running-config | include secret",
        "show running-config\nconfigure terminal",
        "show vlan brief; reload",
        " show vlan brief",
        "show vlan brief ",
        "SHOW VLAN BRIEF",
        "copy running-config tftp://192.0.2.1/x",
        "",
    ],
)
def test_a_command_outside_the_allowlist_is_refused(command: str, inventory, monkeypatch):
    with pytest.raises(CollectorError, match="ngoài allowlist"):
        check_command(command)
    # `fetch_ssh` từ chối TRƯỚC khi đọc mật khẩu hay mở kết nối nào.
    monkeypatch.delenv(fetch.USER_ENV, raising=False)
    monkeypatch.delenv(fetch.PASSWORD_ENV, raising=False)
    with pytest.raises(CollectorError, match="ngoài allowlist"):
        fetch.fetch_ssh(inventory, commands=("show vlan brief", command))


def test_credentials_come_only_from_the_environment(inventory, monkeypatch, capsys):
    monkeypatch.delenv(fetch.USER_ENV, raising=False)
    monkeypatch.delenv(fetch.PASSWORD_ENV, raising=False)
    with pytest.raises(CollectorError, match="COLLECTOR_USER"):
        fetch.credentials()
    monkeypatch.setenv(fetch.USER_ENV, "someone")
    with pytest.raises(CollectorError, match="COLLECTOR_PASSWORD"):
        fetch.fetch_ssh(inventory)  # thiếu mật khẩu: dừng trước khi kết nối
    monkeypatch.setenv(fetch.PASSWORD_ENV, "placeholder-not-a-real-password")
    assert fetch.credentials() == ("someone", "placeholder-not-a-real-password")
    # Không có tham số dòng lệnh nào nhận user hay mật khẩu.
    with pytest.raises(SystemExit):
        main(["--help"])
    usage = capsys.readouterr().out
    assert "--password" not in usage and "--user" not in usage


def test_hosts_come_only_from_the_inventory(inventory, tmp_path: Path, outputs):
    assert [d.host for d in inventory.devices] == [f"192.168.137.{n}" for n in (11, 12, 21, 31, 32, 33, 34)]
    with pytest.raises(CollectorError, match="không có trong inventory"):
        inventory.device("ISP")
    # Thư mục của một thiết bị ngoài inventory bị bỏ qua khi đọc offline.
    synth.write_raw(tmp_path, {**outputs, "ISP": outputs["R-EDGE1"]})
    assert sorted(fetch.read_saved(tmp_path, inventory)) == sorted(DEVICES)


def test_running_config_of_the_wrong_device_is_refused(inventory, outputs):
    swapped = {**outputs, "ACC1": outputs["ACC2"]}
    with pytest.raises(CollectorError, match="hostname trong running-config không khớp"):
        build_context(inventory, swapped, load_policy(POLICY))


# ------------------------------------------------------------ 2b. secret
def _all_text(*roots: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for root in roots for p in sorted(root.rglob("*")) if p.is_file()
    )


def test_no_output_file_contains_a_secret_value(inventory, outputs, built, tmp_path: Path):
    raw = "\n".join(by_command["show running-config"] for by_command in outputs.values())
    # Đối chứng âm: cả ba chuỗi secret giả CÓ trong input, ở cả 7 thiết bị.
    assert all(raw.count(value) == 7 for value in synth.FAKE_SECRET_VALUES)

    context, notes = built
    context_path, meta_path, _ = write_context(
        tmp_path / "out", context, notes, "test", {"inventory": INVENTORY}, NOW, provenance=PROVENANCE
    )
    removed = fetch.save_outputs(tmp_path / "saved", outputs)
    written = _all_text(tmp_path / "out", tmp_path / "saved")
    assert removed == 21  # 3 dòng secret x 7 thiết bị
    assert not [value for value in synth.FAKE_SECRET_VALUES if value in written]
    assert "$1$" not in written and "FAKE" not in written
    assert notes["secrets"]["lines_removed"] == 21
    # Context chỉ có TÊN tham chiếu và một chuỗi giữ chỗ.
    for device in context["devices"]:
        assert device["secrets"] == dict.fromkeys(
            ("enable_secret", "local_netadmin", "tacacs_key"), parse.REDACTED
        )
    assert (
        context_path.name == "smb-01-20261012T080000Z.json"
        and meta_path.name == "smb-01-20261012T080000Z.meta.json"
    )


def test_saved_outputs_can_be_read_back_and_give_the_same_context(
    inventory, outputs, context, tmp_path: Path
):
    fetch.save_outputs(tmp_path, outputs)
    again, _ = build_context(inventory, fetch.read_saved(tmp_path, inventory), load_policy(POLICY))
    assert again == context


@pytest.mark.parametrize(
    "line",
    [
        "enable secret 5 $1$abcd$SENSITIVEVALUE",
        "enable password SENSITIVEVALUE",
        "username netadmin privilege 15 secret 5 SENSITIVEVALUE",
        "username ops password 7 SENSITIVEVALUE",
        " key 7 SENSITIVEVALUE",
        " key SENSITIVEVALUE",
        "tacacs-server host 10.0.0.1 key SENSITIVEVALUE",
        "radius-server key 7 SENSITIVEVALUE",
        "snmp-server community SENSITIVEVALUE RO",
        " password 7 SENSITIVEVALUE",
        " neighbor 10.0.0.2 password SENSITIVEVALUE",
        "crypto isakmp key SENSITIVEVALUE address 0.0.0.0",
        " key-string SENSITIVEVALUE",
        " ppp chap password 0 SENSITIVEVALUE",
    ],
)
def test_sanitize_removes_the_value_and_keeps_the_keyword(line: str):
    clean, removed = parse.sanitize(f"hostname X\n{line}\ninterface Ethernet0/0\n")
    assert removed == 1 and "SENSITIVEVALUE" not in clean
    assert clean.splitlines()[0] == "hostname X" and clean.splitlines()[2] == "interface Ethernet0/0"
    assert line.split()[0] in clean


@pytest.mark.parametrize(
    "line",
    ["service password-encryption", "interface Ethernet0/0", " description UPLINK", "ip ssh version 2"],
)
def test_negative_control_ordinary_lines_are_left_alone(line: str):
    assert parse.sanitize(line) == (line, 0)


def test_notes_never_carry_configuration_lines():
    """Ghi chú chỉ có tên loại, tên interface và số đếm: một lệnh lạ mang secret không lọt qua đó."""
    text = "hostname X\nsome-new-feature token SENSITIVEVALUE\nenable secret 5 SENSITIVEVALUE\n"
    _, notes = parse.parse_running_config(text, "router")
    dumped = json.dumps(notes.as_dict())
    assert "SENSITIVEVALUE" not in dumped and notes.unparsed == {"some-new-feature": 1}


# ------------------------------------------------------------ 3. contract, invariants, meta
def test_the_collected_context_passes_the_contract_and_the_invariants(built, tmp_path: Path):
    context, notes = built
    assert validate_only(context) == 0
    context_path, meta_path, violations = write_context(
        tmp_path, context, notes, "test", {"policy": POLICY}, NOW, provenance=PROVENANCE
    )
    assert violations == []
    parsed = NetworkContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    # D-054: provenance nằm trong context (phần được hash), ngay sau context_id.
    assert list(json.loads(context_path.read_text(encoding="utf-8")))[:3] == [
        "schema_version",
        "context_id",
        "provenance",
    ]
    assert parsed.provenance is not None and parsed.provenance.collected_at == NOW
    assert parsed.provenance.ttl_seconds == 86400
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["provenance"] == PROVENANCE
    assert meta["validation"] == {"contract_and_invariants": "ok", "violations": []}
    assert (meta["collected_at"], meta["source"], meta["collector_version"]) == (
        "2026-10-12T08:00:00Z",
        "test",
        "0.1.0",
    )
    assert len(meta["sha256"]) == 64 and meta["context_file"] == context_path.name
    assert meta["counts"] == {
        "devices": 7,
        "interfaces": 64,
        "vlans": 23,
        "static_routes": 8,
        "acls": 9,
        "acl_entries": 34,
        "aaa": 7,
        "links": 6,
    }
    assert meta["commands"] == list(ALLOWED_COMMANDS)


def test_a_violation_is_reported_and_the_data_is_not_repaired(inventory, outputs, tmp_path: Path):
    """Đối chứng âm cho test trên: output có một ACL không tồn tại → invariant báo, file vẫn ghi đúng những
    gì thiết bị trả về."""
    broken = {name: dict(by_command) for name, by_command in outputs.items()}
    config = broken["CORE1"]["show running-config"]
    assert config.count(" ip access-group ACL-GUEST-IN in") == 1
    broken["CORE1"]["show running-config"] = config.replace("ACL-GUEST-IN in", "NO-SUCH-ACL in")
    context, notes = build_context(inventory, broken, load_policy(POLICY))
    context_path, meta_path, violations = write_context(
        tmp_path, context, notes, "test", {}, NOW, provenance=PROVENANCE
    )
    assert [v["code"] for v in violations] == ["CTX_ACL_REF"]
    vlan60 = next(
        i
        for i in _device(json.loads(context_path.read_text(encoding="utf-8")), "CORE1")["interfaces"]
        if i["name"] == "Vlan60"
    )
    assert vlan60["acl_in"] == "NO-SUCH-ACL"
    assert (
        json.loads(meta_path.read_text(encoding="utf-8"))["validation"]["contract_and_invariants"]
        == "violations"
    )


def test_contract_gaps_are_recorded_in_the_meta(notes: dict, context: dict):
    assert {n for n, d in notes["devices"].items() if d["vrf_interfaces"]} == VRF_MGMT
    assert notes["devices"]["CORE1"]["vrf_interfaces"] == {"Ethernet1/3": "MGMT"}
    gaps = {g["kind"]: g for g in notes["contract_gaps"]}
    assert gaps["vrf_forwarding"]["count"] == 4 and gaps["banner_login"]["count"] == 7
    assert gaps["acl_standard_as_extended"]["count"] == 7 and gaps["route_name"]["count"] == 8
    # Contract chỉ có MỘT `mgmt_prefix`; năm mạng quản trị còn lại của policy nằm ở meta.
    assert context["policy"]["mgmt_prefix"] == "192.168.137.0/24"
    assert gaps["policy_fields"]["fields"] == ["mgmt_prefixes_all"]
    assert len(notes["policy_not_in_contract"]["mgmt_prefixes_all"]) == 6
    assert "mgmt_prefixes_all" not in context["policy"]
    assert all(not d["unparsed"] and not d["skipped_interfaces"] for d in notes["devices"].values())


def test_a_subinterface_is_skipped_and_recorded_not_guessed():
    text = (
        "hostname R-BR1\ninterface Ethernet0/1\n no ip address\n"
        "interface Ethernet0/1.110\n encapsulation dot1Q 110\n ip address 10.20.10.1 255.255.255.0\n"
    )
    device, notes = parse.parse_running_config(text, "router")
    assert [i["name"] for i in device["interfaces"]] == ["Ethernet0/1"]
    assert notes.skipped_interfaces == ["Ethernet0/1.110"] and notes.not_represented["interface_name"] == 1


def test_policy_file_matches_the_lab_design(context: dict):
    policy = load_policy(POLICY)
    assert policy["reserved_vlans"] == [1, 99, 199, 999] and policy["parking_vlan"] == 999
    assert (policy["max_devices_per_change"], policy["max_interfaces_per_change"]) == (
        2,
        8,
    )  # như campus-small-01
    campus = json.loads((ROOT / "data" / "contexts" / "campus-small-01.json").read_text(encoding="utf-8"))[
        "policy"
    ]
    assert (campus["max_devices_per_change"], campus["max_interfaces_per_change"]) == (2, 8)
    protected = policy["protected_interfaces"]
    assert set(protected) == set(DEVICES)
    for name, (port, _) in MGMT_IP.items():
        assert port in protected[name], name  # mọi cổng quản lý
    for link in LINKS:
        for device, port in link:
            assert port in protected[device], (device, port)  # mọi uplink và trunk
    assert "Ethernet0/1" in protected["R-EDGE1"]  # uplink tới ISP
    trunks = {
        (d["hostname"], i["name"])
        for d in context["devices"]
        for i in d["interfaces"]
        if i["mode"] == "trunk"
    }
    assert all(port in protected[device] for device, port in trunks)


# ------------------------------------------------------------ dòng lệnh
def test_cli_offline_from_a_synthetic_directory(tmp_path: Path, capsys):
    code = main(
        [
            "--synth-from",
            str(BASELINE),
            "--out",
            str(tmp_path),
            "--inventory",
            str(INVENTORY),
            "--policy",
            str(POLICY),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "contract + invariants: 0 vi phạm" in out
    assert "7 thiết bị, 64 interface, 23 VLAN, 8 static route, 9 ACL, 6 link" in out
    assert len(list(tmp_path.glob("smb-01-*Z.json"))) == 1 and len(list(tmp_path.glob("*.meta.json"))) == 1
    assert not [value for value in synth.FAKE_SECRET_VALUES if value in out + _all_text(tmp_path)]


def test_cli_errors(tmp_path: Path, capsys):
    assert main(["--from-dir", str(tmp_path / "missing"), "--out", str(tmp_path / "o")]) == 2
    assert "thiếu output `show running-config`" in capsys.readouterr().err
    assert (
        main(
            [
                "--from-dir",
                str(tmp_path),
                "--inventory",
                str(tmp_path / "no.json"),
                "--out",
                str(tmp_path / "o"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "o").exists()


# ------------------------------------------------------------ SSH: thuật toán cũ, theo từng kết nối
def test_legacy_algorithms_are_enabled_on_one_transport_only():
    """Ba thuật toán cũ của image IOL được thêm vào ĐÚNG transport của kết nối tới lab; mặc định của lớp
    `paramiko.Transport` không đổi. Test không mở kết nối mạng nào (một cặp socket nội bộ)."""
    paramiko = pytest.importorskip("paramiko", reason="extras [demo] chưa cài")
    before = (
        tuple(paramiko.Transport._preferred_kex),
        tuple(paramiko.Transport._preferred_keys),
        tuple(paramiko.Transport._preferred_macs),
    )
    left, right = socket.socketpair()
    try:
        transport = fetch._lab_transport(left)
        options = transport.get_security_options()
        assert options.kex[-1] == fetch.LEGACY_KEX
        assert options.key_types[-1] == fetch.LEGACY_HOST_KEY
        assert fetch.LEGACY_MAC in options.digests
        # Thuật toán hiện đại vẫn đứng trước: thiết bị nào hỗ trợ thì vẫn dùng chúng.
        assert options.kex[0] != fetch.LEGACY_KEX and options.key_types[0] != fetch.LEGACY_HOST_KEY
        transport.close()
    finally:
        left.close()
        right.close()
    after = (
        tuple(paramiko.Transport._preferred_kex),
        tuple(paramiko.Transport._preferred_keys),
        tuple(paramiko.Transport._preferred_macs),
    )
    assert after == before


# ------------------------------------------------------------ SSH: lỗi kết nối (mock, không SSH thật)
PASSWORD = "placeholder-pw-for-error-path-test"  # noqa: S105 — chuỗi giả, chỉ để kiểm nó không bị in ra


class _FakeConnection:
    """Kết nối giả: trả output đã dựng sẵn, ghi lại lệnh nhận được."""

    def __init__(
        self, by_command: dict[str, str], fail_on: str | None = None, error: Exception | None = None
    ):
        self.by_command, self.fail_on, self.error = by_command, fail_on, error
        self.sent: list[str] = []
        self.closed = False

    def send_command(self, command: str, **_kwargs) -> str:
        self.sent.append(command)
        if command == self.fail_on and self.error is not None:
            raise self.error
        return self.by_command[command]

    def disconnect(self) -> None:
        self.closed = True


def _ssh_env(monkeypatch) -> None:
    monkeypatch.setenv(fetch.USER_ENV, "netadmin")
    monkeypatch.setenv(fetch.PASSWORD_ENV, PASSWORD)


def _connection_errors() -> list:
    netmiko_exc = pytest.importorskip("netmiko.exceptions", reason="extras [demo] chưa cài")
    paramiko = pytest.importorskip("paramiko", reason="extras [demo] chưa cài")
    text = f"Authentication to device failed. user netadmin password {PASSWORD}"
    return [
        netmiko_exc.NetmikoAuthenticationException(text),
        netmiko_exc.NetmikoTimeoutException(text),
        netmiko_exc.ConnectionException(text),
        paramiko.AuthenticationException(text),
        paramiko.SSHException(text),
        ConnectionRefusedError(text),
        TimeoutError(text),
        EOFError(text),
    ]


@pytest.mark.parametrize("index", range(8))
def test_a_connection_failure_is_one_line_with_device_and_error_type(index: int, inventory, monkeypatch):
    error = _connection_errors()[index]
    _ssh_env(monkeypatch)

    def refuse(device, user, password):
        raise error

    monkeypatch.setattr(fetch, "_connect", refuse)
    with pytest.raises(CollectorError) as caught:
        fetch.fetch_ssh(inventory)
    message = str(caught.value)
    assert message == f"R-EDGE1 192.168.137.11: {type(error).__name__}"
    assert "\n" not in message and PASSWORD not in message and "netadmin" not in message
    # Exception gốc (mang thông báo của thư viện) không được nối vào: không có gì để in thành traceback.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_root_cause_netmiko_auth_and_timeout_errors_are_not_netmiko_base_exceptions():
    """Vì sao lỗi này lọt: hai lớp dưới đây kế thừa `paramiko.SSHException`, không kế thừa
    `NetmikoBaseException`. Bản đầu của `fetch_ssh` chỉ bắt `(NetmikoBaseException, OSError)`."""
    netmiko_exc = pytest.importorskip("netmiko.exceptions", reason="extras [demo] chưa cài")
    paramiko = pytest.importorskip("paramiko", reason="extras [demo] chưa cài")
    old_tuple = (netmiko_exc.NetmikoBaseException, OSError)
    for cls in (netmiko_exc.NetmikoAuthenticationException, netmiko_exc.NetmikoTimeoutException):
        assert not issubclass(cls, old_tuple) and issubclass(cls, paramiko.SSHException)
        assert issubclass(cls, fetch._ssh_errors())


def test_cli_wrong_password_exits_2_with_one_line_and_no_traceback(tmp_path: Path, monkeypatch, capsys):
    netmiko_exc = pytest.importorskip("netmiko.exceptions", reason="extras [demo] chưa cài")
    _ssh_env(monkeypatch)

    def refuse(device, user, password):
        raise netmiko_exc.NetmikoAuthenticationException(f"Authentication failed, password {PASSWORD}")

    monkeypatch.setattr(fetch, "_connect", refuse)
    args = ["--ssh", "--out", str(tmp_path / "out"), "--inventory", str(INVENTORY), "--policy", str(POLICY)]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.err == "[LỖI COLLECTOR] R-EDGE1 192.168.137.11: NetmikoAuthenticationException\n"
    assert captured.out == ""
    assert "Traceback" not in captured.err and PASSWORD not in captured.err
    assert not (tmp_path / "out").exists()  # không file nào được ghi


def test_negative_control_the_same_mock_path_succeeds_when_nothing_fails(
    tmp_path: Path, inventory, outputs, monkeypatch, capsys
):
    """Đối chứng âm: cùng đường `--ssh` với kết nối giả không lỗi → mã 0. Vậy mã 2 ở test trên là do
    exception, không do cách dựng mock. Mọi lệnh gửi đi đều nằm trong allowlist."""
    pytest.importorskip("netmiko", reason="extras [demo] chưa cài")
    _ssh_env(monkeypatch)
    opened: list[_FakeConnection] = []

    def connect(device, user, password):
        assert (user, password) == ("netadmin", PASSWORD)
        opened.append(_FakeConnection(outputs[device.hostname]))
        return opened[-1]

    monkeypatch.setattr(fetch, "_connect", connect)
    args = ["--ssh", "--out", str(tmp_path / "out"), "--inventory", str(INVENTORY), "--policy", str(POLICY)]
    args += ["--save-raw", str(tmp_path / "raw")]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "contract + invariants: 0 vi phạm" in out and PASSWORD not in out
    assert len(opened) == 7 and all(c.closed for c in opened)
    assert all(c.sent == list(ALLOWED_COMMANDS) for c in opened)
    written = _all_text(tmp_path)
    assert PASSWORD not in written and not [v for v in synth.FAKE_SECRET_VALUES if v in written]


def test_a_failure_while_running_a_command_is_reported_the_same_way_and_the_connection_is_closed(
    inventory, outputs, monkeypatch
):
    netmiko_exc = pytest.importorskip("netmiko.exceptions", reason="extras [demo] chưa cài")
    _ssh_env(monkeypatch)
    opened: list[_FakeConnection] = []

    def connect(device, user, password):
        fail = "show vlan brief" if device.hostname == "CORE1" else None
        opened.append(
            _FakeConnection(outputs[device.hostname], fail, netmiko_exc.ReadTimeout("pattern not detected"))
        )
        return opened[-1]

    monkeypatch.setattr(fetch, "_connect", connect)
    with pytest.raises(CollectorError) as caught:
        fetch.fetch_ssh(inventory)
    assert str(caught.value) == "CORE1 192.168.137.21: ReadTimeout"
    assert [c.closed for c in opened] == [True, True, True]  # R-EDGE1, R-BR1, CORE1; dừng ở CORE1


# ------------------------------------------------------------ output THẬT của lab (13/10/2026)
# Trích (bỏ bớt dòng) từ `show_running_config.txt` của lượt SSH thật (13/10, đã xoá secret); mọi dòng dưới đây
# có trong raw lượt 4, `runs-real/smb-01-raw/20261013T083915Z/<thiết bị>/`. Không có dòng nào mang secret. Các
# dòng này là bằng chứng cho hai chỗ output thật khác output giả định.
REAL_CORE1_HEAD = """Building configuration...

Current configuration : 4644 bytes
!
version 15.2
hostname CORE1
vtp mode transparent
!
no ip domain-lookup
ip domain-name smb.lab
ip cef
no ipv6 cef
!
spanning-tree mode rapid-pvst
!
interface Serial2/0
 no ip address
 shutdown
 serial restart-delay 0
!
ip route 0.0.0.0 0.0.0.0 10.255.0.1 name DEFAULT-EDGE
!
end
"""
REAL_ACC1_HEAD = """version 15.2
hostname ACC1
no ip routing
"""


def test_real_iol_output_routing_on_has_no_ip_routing_line():
    """IOL L2 thật: CORE1 định tuyến được (ping 5/5) mà running-config KHÔNG có dòng `ip routing`; switch
    tắt định tuyến thì có `no ip routing`. Bản đầu của parser coi switch không có dòng `ip routing` là tắt."""
    core1, _ = parse.parse_running_config(REAL_CORE1_HEAD, "distribution_switch")
    acc1, _ = parse.parse_running_config(REAL_ACC1_HEAD, "access_switch")
    assert (core1["ip_routing"], acc1["ip_routing"]) == (True, False)
    assert core1["static_routes"] == [{"prefix": "0.0.0.0/0", "next_hop": "10.255.0.1"}]
    # Đối chứng: dòng `ip routing` tường minh vẫn được hiểu là bật.
    explicit, _ = parse.parse_running_config("hostname X\nip routing\n", "access_switch")
    assert explicit["ip_routing"] is True


def test_synthetic_running_config_hides_ip_routing_like_the_real_device(outputs):
    """Fixture giả phải giống output thật ở điểm này, nếu không bộ test offline lại xanh với parser sai."""
    for name in DEVICES:
        assert "\nip routing\n" not in outputs[name]["show running-config"]
    assert "\nno ip routing\n" in outputs["ACC1"]["show running-config"]


def test_skipped_interface_names_are_the_real_ones_not_an_example(inventory, outputs):
    """Lượt thật in "(vd subinterface `Ethernet0/1.110`) (8)" trong khi 8 interface bị bỏ là `Serial2/0` …
    `Serial3/3` của CORE1: câu ví dụ cứng trong code gây hiểu nhầm. Nay meta và dòng lệnh in tên thật."""
    _, notes = parse.parse_running_config(REAL_CORE1_HEAD, "distribution_switch")
    assert notes.skipped_interfaces == ["Serial2/0"]
    changed = {name: dict(by_command) for name, by_command in outputs.items()}
    serial = "interface Serial2/0\n no ip address\n shutdown\n serial restart-delay 0\n!\n"
    changed["CORE1"]["show running-config"] = changed["CORE1"]["show running-config"].replace(
        "\nend\n", "\n" + serial + "end\n"
    )
    _, meta_notes = build_context(inventory, changed, load_policy(POLICY))
    gap = next(g for g in meta_notes["contract_gaps"] if g["kind"] == "interface_name")
    assert gap["names"] == {"CORE1": ["Serial2/0"]} and gap["count"] == 1
    assert "Ethernet0/1.110" not in json.dumps(meta_notes["contract_gaps"])


# ------------------------------------------------------------------ --save-raw: một thư mục cho mỗi lượt
def _ssh_run(monkeypatch, outputs, when: datetime) -> None:
    """Đường `--ssh` không cần netmiko: thay `fetch_ssh` bằng output giả và cố định mốc thời gian của lượt."""
    monkeypatch.setattr(collector_cli, "fetch_ssh", lambda inventory: outputs)
    monkeypatch.setattr(collector_cli, "_now", lambda: when)


def _files(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def _ssh_args(tmp_path: Path, *save_raw: str) -> list[str]:
    return [
        "--ssh",
        "--out",
        str(tmp_path / "out"),
        "--inventory",
        str(INVENTORY),
        "--policy",
        str(POLICY),
    ] + [*save_raw]


def test_save_raw_writes_one_folder_per_run_and_never_touches_an_earlier_run(
    tmp_path: Path, outputs, monkeypatch, capsys
):
    """Bản đầu ghi mọi lượt vào cùng một thư mục, nên raw của lượt 1 và 2 thật bị lượt 3 ghi đè (10.19)."""
    raw_root = tmp_path / "runs-real" / "smb-01-raw"
    args = _ssh_args(tmp_path, "--save-raw", str(tmp_path / "runs-real"))
    _ssh_run(monkeypatch, outputs, NOW)
    assert main(args) == 0
    first = _files(raw_root / "20261012T080000Z")
    assert len(first) == sum(len(by_command) for by_command in outputs.values())

    _ssh_run(monkeypatch, outputs, NOW + timedelta(minutes=20))
    assert main(args) == 0
    assert sorted(p.name for p in raw_root.iterdir()) == ["20261012T080000Z", "20261012T082000Z"]
    assert _files(raw_root / "20261012T080000Z") == first  # lượt trước còn nguyên từng byte

    # Thư mục raw và file context cùng mốc; meta trỏ tới đúng thư mục raw của lượt đó.
    meta = json.loads((tmp_path / "out" / "smb-01-20261012T082000Z.meta.json").read_text(encoding="utf-8"))
    assert meta["collected_at"] == "2026-10-12T08:20:00Z"
    assert meta["raw_dir"] == (raw_root / "20261012T082000Z").as_posix()
    out = capsys.readouterr().out
    assert f"đã lưu output vào {(raw_root / '20261012T082000Z').as_posix()} (21 dòng secret" in out
    assert not [value for value in synth.FAKE_SECRET_VALUES if value in _all_text(raw_root)]


def test_save_raw_refuses_a_run_folder_that_already_exists(tmp_path: Path, outputs, monkeypatch, capsys):
    raw_dir = tmp_path / "runs-real" / "smb-01-raw" / "20261012T080000Z"
    args = _ssh_args(tmp_path, "--save-raw", str(tmp_path / "runs-real"))
    _ssh_run(monkeypatch, outputs, NOW)
    assert main(args) == 0
    before, contexts = _files(raw_dir), sorted((tmp_path / "out").iterdir())
    capsys.readouterr()

    assert main(args) == 2  # cùng mốc: lỗi, không ghi đè và không ghi context mới
    assert capsys.readouterr().err == (
        f"[LỖI COLLECTOR] thư mục raw đã có, không ghi đè: {raw_dir.as_posix()}\n"
    )
    assert _files(raw_dir) == before and sorted((tmp_path / "out").iterdir()) == contexts


def test_save_raw_without_a_path_uses_runs_real_and_no_flag_saves_nothing(
    tmp_path: Path, outputs, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _ssh_run(monkeypatch, outputs, NOW)
    assert main(_ssh_args(tmp_path)) == 0
    assert not (tmp_path / "runs-real").exists()
    meta = json.loads((tmp_path / "out" / "smb-01-20261012T080000Z.meta.json").read_text(encoding="utf-8"))
    assert "raw_dir" not in meta

    _ssh_run(monkeypatch, outputs, NOW + timedelta(seconds=1))
    assert main(_ssh_args(tmp_path, "--save-raw")) == 0
    assert (
        tmp_path / "runs-real" / "smb-01-raw" / "20261012T080001Z" / "ACC1" / "show_running_config.txt"
    ).is_file()


@pytest.mark.parametrize("context_id", ["../smb-01", "smb/01", "", ".hidden", "a" * 65])
def test_raw_dir_rejects_a_context_id_that_is_not_a_plain_name(tmp_path: Path, context_id: str):
    with pytest.raises(CollectorError, match="context_id không dùng làm tên thư mục"):
        fetch.new_raw_dir(tmp_path, context_id, NOW)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ snapshot tham chiếu (D-047)
REFERENCE = ROOT / "data" / "contexts" / "smb-01-collected-ref.json"
REFERENCE_SHA256 = (
    "52aef87d30649af585832af6a148ffe4b9d5c3699928de31da636359e7fa4971"  # lượt 4, 2026-10-13T08:39:15Z
)


def _secret_values(context: dict) -> set[str]:
    """Mọi giá trị trong `Device.secrets`: với context thu thập, chỉ được là chuỗi giữ chỗ."""
    return {value for device in context["devices"] for value in device["secrets"].values()}


def test_reference_snapshot_is_the_recorded_run_and_has_no_secret_values():
    """Chép nguyên byte từ lượt SSH thứ 4; hash ghim ở D-047, nên file không được sửa âm thầm."""
    raw = REFERENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REFERENCE_SHA256
    context = json.loads(raw)
    assert validate_only(context) == 0 and context_violations(REFERENCE) == []
    assert _secret_values(context) == {parse.REDACTED}
    # Đối chứng âm: một giá trị khác chuỗi giữ chỗ thì phép kiểm trên bắt được.
    context["devices"][0]["secrets"]["enable_secret"] = "$1$abcd$0123456789abcdefghij"  # noqa: S105 — chuỗi giả
    assert _secret_values(context) != {parse.REDACTED}


# ------------------------------------------------------------------ provenance (D-054)
REFERENCE_PROV = ROOT / "data" / "contexts" / "smb-01-collected-ref-prov.json"
REFERENCE_PROV_SHA256 = "42df80d5aa2065830ae777eb363611be8c13124d217074c20c9ac978d5985206"
REFERENCE_COLLECTED_AT = datetime(2026, 10, 13, 8, 39, 15, tzinfo=UTC)


def _dump(context: dict) -> bytes:
    """Cách collector ghi file context (`write_context`)."""
    return (json.dumps(context, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def test_provenance_snapshot_is_the_reference_plus_one_block():
    """Snapshot cho freshness: dẫn xuất từ snapshot tham chiếu (giữ nguyên byte và hash), chỉ thêm khối
    `provenance` với mốc của lượt SSH thứ 4 và TTL mặc định."""
    old = json.loads(REFERENCE.read_bytes())
    assert _dump(old) == REFERENCE.read_bytes(), "snapshot cũ phải đúng định dạng ghi của collector"
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256  # file cũ không đổi
    expected = with_provenance(old, collector_provenance(REFERENCE_COLLECTED_AT, DEFAULT_TTL_SECONDS))
    raw = REFERENCE_PROV.read_bytes()
    assert raw == _dump(expected)
    assert hashlib.sha256(raw).hexdigest() == REFERENCE_PROV_SHA256
    assert context_violations(REFERENCE_PROV) == []
    parsed = NetworkContext.model_validate_json(raw)
    assert parsed.provenance is not None
    assert (
        parsed.provenance.source.value,
        parsed.provenance.collected_at,
        parsed.provenance.ttl_seconds,
    ) == (
        "collector",
        REFERENCE_COLLECTED_AT,
        86400,
    )
    assert _secret_values(json.loads(raw)) == {parse.REDACTED}


def _written_context(out: Path) -> dict:
    [path] = [p for p in out.glob("smb-01-*.json") if not p.name.endswith(".meta.json")]
    return json.loads(path.read_text(encoding="utf-8"))


def test_ssh_takes_the_timestamp_before_reading_any_device(tmp_path: Path, outputs, monkeypatch):
    """Tuổi của context tính từ dữ liệu CŨ NHẤT của lượt: mốc lấy trước khi đọc thiết bị đầu tiên."""
    order: list[str] = []
    monkeypatch.setattr(collector_cli, "_now", lambda: order.append("now") or NOW)
    monkeypatch.setattr(collector_cli, "fetch_ssh", lambda _inventory: order.append("fetch") or outputs)
    assert main(_ssh_args(tmp_path)) == 0
    assert order == ["now", "fetch"]
    assert _written_context(tmp_path / "out")["provenance"] == {
        "source": "collector",
        "collected_at": "2026-10-12T08:00:00Z",
        "ttl_seconds": DEFAULT_TTL_SECONDS,
    }


def test_ttl_seconds_option(tmp_path: Path, outputs, monkeypatch):
    _ssh_run(monkeypatch, outputs, NOW)
    assert main(_ssh_args(tmp_path, "--ttl-seconds", "3600")) == 0
    assert _written_context(tmp_path / "out")["provenance"]["ttl_seconds"] == 3600


@pytest.mark.parametrize("ttl", ["0", "-5", "abc", "1.5", str(MAX_TTL_SECONDS + 1)])
def test_invalid_ttl_is_a_usage_error(tmp_path: Path, ttl: str):
    with pytest.raises(SystemExit) as exc:
        main(_ssh_args(tmp_path, "--ttl-seconds", ttl))
    assert exc.value.code == 2


def _from_dir_args(tmp_path: Path, folder: Path, *extra: str) -> list[str]:
    return ["--from-dir", str(folder), "--out", str(tmp_path / "out"), "--inventory", str(INVENTORY)] + [
        "--policy",
        str(POLICY),
        *extra,
    ]


def test_from_dir_uses_the_capture_time_not_the_processing_time(tmp_path: Path, outputs, monkeypatch):
    """Chạy lại trên output đã lưu từ lâu không được cho một context "mới": mốc là tên thư mục raw."""
    saved = tmp_path / "raw" / "20261013T083915Z"
    fetch.save_outputs(saved, outputs)
    monkeypatch.setattr(collector_cli, "_now", lambda: NOW + timedelta(days=30))  # giờ xử lý muộn hơn nhiều
    assert main(_from_dir_args(tmp_path, saved)) == 0
    assert _written_context(tmp_path / "out")["provenance"]["collected_at"] == "2026-10-13T08:39:15Z"
    assert (tmp_path / "out" / "smb-01-20261013T083915Z.json").is_file()


def test_from_dir_without_a_capture_time_is_refused(tmp_path: Path, outputs, capsys):
    saved = tmp_path / "raw" / "saved-output"
    fetch.save_outputs(saved, outputs)
    assert main(_from_dir_args(tmp_path, saved)) == 2
    assert "không biết mốc thu thập" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
    # Đối chứng âm: cùng thư mục, có --collected-at → chạy được và dùng đúng mốc đó.
    assert main(_from_dir_args(tmp_path, saved, "--collected-at", "2026-10-13T10:39:15+02:00")) == 0
    assert _written_context(tmp_path / "out")["provenance"]["collected_at"] == "2026-10-13T08:39:15Z"


def test_naive_collected_at_is_refused(tmp_path: Path, outputs, capsys):
    saved = tmp_path / "raw" / "saved-output"
    fetch.save_outputs(saved, outputs)
    assert main(_from_dir_args(tmp_path, saved, "--collected-at", "2026-10-13T08:39:15")) == 2
    assert "múi giờ" in capsys.readouterr().err


def test_collected_at_is_only_for_from_dir(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        main(_ssh_args(tmp_path, "--collected-at", "2026-10-13T08:39:15Z"))
    assert exc.value.code == 2


def test_synthetic_run_is_marked_synthetic(tmp_path: Path):
    args = ["--synth-from", str(BASELINE), "--out", str(tmp_path / "out"), "--inventory", str(INVENTORY)]
    assert main([*args, "--policy", str(POLICY)]) == 0
    assert _written_context(tmp_path / "out")["provenance"] == {"source": "synthetic"}
