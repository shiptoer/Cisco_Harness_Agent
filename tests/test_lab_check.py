"""`check_lab.py` của lab smb-01: bộ cấu hình nền trong repo phải qua được, và phép kiểm thứ tự lệnh quanh
`no switchport` bắt được đúng lỗi đã gặp trên lab (13/10/2026: `no cdp enable` đặt trước `no switchport`
bị IOS xoá)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

BASELINE = Path(__file__).resolve().parents[1] / "lab" / "smb-01" / "baseline-configs"

_spec = importlib.util.spec_from_file_location("check_lab", BASELINE / "check_lab.py")
check_lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_lab)

OLD_ORDER = " description OOB-MGMT\n no cdp enable\n no switchport\n"
NEW_ORDER = " description OOB-MGMT\n no switchport\n no cdp enable\n"


def test_repo_baseline_passes(capsys):
    assert check_lab.main(BASELINE) == 0
    out = capsys.readouterr().out
    assert "[THU-TU]" not in out and "Tong so van de: 0" in out


@pytest.mark.parametrize("device", ["ACC1", "ACC2", "ACC3", "ACC-BR1", "CORE1"])
def test_management_port_has_no_cdp_enable_after_no_switchport(device):
    text = (BASELINE / f"{device}.cfg").read_text()
    assert NEW_ORDER in text and OLD_ORDER not in text


def test_old_order_is_caught(tmp_path: Path, capsys):
    """Đối chứng âm: đặt lại thứ tự cũ ở ACC1 thì main báo đúng thiết bị, interface và lệnh."""
    for cfg in BASELINE.glob("*.cfg"):
        shutil.copy(cfg, tmp_path)
    acc1 = tmp_path / "ACC1.cfg"
    acc1.write_text(acc1.read_text().replace(NEW_ORDER, OLD_ORDER))
    assert check_lab.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "[THU-TU] ACC1 Ethernet1/3: 'no cdp enable' dung truoc 'no switchport'" in out


def test_before_no_switchport_rules():
    text = (
        "interface Ethernet1/3\n description X\n duplex full\n no cdp enable\n no switchport\n"
        " ip address 10.0.0.1 255.255.255.0\n!\n"
        "interface Ethernet0/0\n description Y\n switchport mode trunk\n!\n"
        "interface Ethernet0/1\n no switchport\n no cdp enable\n!\n"
    )
    # description được phép; lệnh sau `no switchport` và interface không có `no switchport` không bị báo
    assert check_lab.before_no_switchport(text) == [
        ("Ethernet1/3", "duplex full"),
        ("Ethernet1/3", "no cdp enable"),
    ]
