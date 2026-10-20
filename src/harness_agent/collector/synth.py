"""Dựng output `show` GIẢ từ các file cấu hình nền của lab, để collector chạy và được test khi không có
thiết bị (`--from-dir`).

Đây là fixture, không phải dữ liệu thu thập: nó mô phỏng DẠNG của output IOS (tiêu đề, cột, tên cổng viết
tắt), không mô phỏng hành vi của thiết bị. Output thật có thể khác; lượt SSH thật đầu tiên phải được đối
chiếu lại.

Trong `show running-config` giả, placeholder secret của file nền được thay bằng các chuỗi GIẢ có dạng như
IOS in ra (hash loại 5, key loại 7). Chúng không phải secret của lab nào; test dùng chúng làm đối chứng:
không chuỗi nào trong số này được xuất hiện ở output của collector.

Trạng thái: ĐỀ XUẤT (D-047).
"""

from __future__ import annotations

import re
from pathlib import Path

from .fetch import ALLOWED_COMMANDS, Inventory, command_file
from .parse import interface_name

# Chuỗi giả, chỉ để kiểm rằng collector xoá sạch. Không phải secret thật.
FAKE_SECRETS: dict[str, str] = {
    "{{ENABLE_SECRET}}": "5 $1$fAkE$FixtureEnableHashNotReal0001",
    "{{ADMIN_PASSWORD}}": "5 $1$fAkE$FixtureAdminHashNotReal00002",
    "{{TACACS_KEY}}": "7 0F1E2D3C4B5A69788796A5B4C3D2E1F00FAKE",
}
FAKE_SECRET_VALUES: tuple[str, ...] = tuple(value.split(" ", 1)[1] for value in FAKE_SECRETS.values())

# Link dữ liệu của lab smb-01 (README-smb-01.md mục 2), chép tay từ sơ đồ: (thiết bị, cổng, thiết bị, cổng).
SMB01_LINKS: tuple[tuple[str, str, str, str], ...] = (
    ("R-EDGE1", "Ethernet0/0", "CORE1", "Ethernet0/0"),
    ("R-EDGE1", "Ethernet0/2", "R-BR1", "Ethernet0/0"),
    ("R-BR1", "Ethernet0/1", "ACC-BR1", "Ethernet0/0"),
    ("CORE1", "Ethernet0/1", "ACC1", "Ethernet0/0"),
    ("CORE1", "Ethernet0/2", "ACC2", "Ethernet0/0"),
    ("CORE1", "Ethernet0/3", "ACC3", "Ethernet0/0"),
    # Link tới ISP: ISP không nằm trong inventory. Trên lab thật cổng này tắt CDP; fixture vẫn đưa vào để
    # kiểm rằng collector bỏ láng giềng ngoài inventory.
    ("R-EDGE1", "Ethernet0/1", "ISP", "Ethernet0/0"),
)


def _short(name: str) -> str:
    return re.sub(r"^Ethernet", "Et", name)


def running_config(baseline: str) -> str:
    """File cấu hình nền → dạng `show running-config`: bỏ chú thích và các dòng IOS không in ra (`no
    shutdown`, `crypto key generate`, và `ip routing` vì đó là mặc định), thay placeholder secret bằng
    chuỗi giả."""
    lines = ["Building configuration...", "", "Current configuration : 4096 bytes", "!"]
    for line in baseline.splitlines():
        if (
            line.startswith("! ")
            or line.strip() in ("no shutdown", "ip routing")
            or line.startswith("crypto key generate")
        ):
            continue
        for placeholder, fake in FAKE_SECRETS.items():
            line = line.replace(placeholder, fake)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _interfaces(baseline: str) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for line in baseline.splitlines():
        if line.startswith("interface "):
            found.append((line.split()[1], []))
        elif line.startswith(" ") and found:
            found[-1][1].append(line.strip())
        elif not line.startswith(" ") and line.strip() != "!" and found and not line.startswith("interface"):
            found.append(("", []))  # kết thúc khối interface
    return [(name, body) for name, body in found if name]


def vlan_brief(baseline: str) -> str:
    vlans: dict[int, str] = {1: "default"}
    current: int | None = None
    for line in baseline.splitlines():
        match = re.fullmatch(r"vlan (\d+)", line)
        if match:
            current = int(match.group(1))
            vlans[current] = f"VLAN{current:04d}"
        elif current is not None and line.startswith(" name "):
            vlans[current] = line.split(None, 1)[1]
        elif not line.startswith(" "):
            current = None
    ports: dict[int, list[str]] = {}
    for name, body in _interfaces(baseline):
        access = next((int(x.split()[-1]) for x in body if x.startswith("switchport access vlan ")), None)
        if access is not None:
            ports.setdefault(access, []).append(_short(name))
    lines = [
        "",
        "VLAN Name                             Status    Ports",
        "---- " + "-" * 32 + " --------- " + "-" * 31,
    ]
    for vid in sorted(vlans):
        lines.append(f"{vid:<4} {vlans[vid]:<32} active    {', '.join(ports.get(vid, []))}".rstrip())
    for vid, name in (
        (1002, "fddi-default"),
        (1003, "token-ring-default"),
        (1004, "fddinet-default"),
        (1005, "trnet-default"),
    ):
        lines.append(f"{vid:<4} {name:<32} act/unsup")
    return "\n".join(lines) + "\n"


def ip_interface_brief(baseline: str) -> str:
    lines = ["Interface              IP-Address      OK? Method Status                Protocol"]
    for name, body in _interfaces(baseline):
        address = next((x.split()[2] for x in body if x.startswith("ip address ")), "unassigned")
        down = "shutdown" in body
        status, protocol = ("administratively down", "down") if down else ("up", "up")
        method = "NVRAM" if address != "unassigned" else "unset"
        lines.append(f"{name:<22} {address:<15} YES {method:<6} {status:<21} {protocol}")
    return "\n".join(lines) + "\n"


def interfaces_trunk(baseline: str) -> str:
    trunks = [(n, b) for n, b in _interfaces(baseline) if "switchport mode trunk" in b]
    if not trunks:
        return "\n"
    native = {n: next((x.split()[-1] for x in b if "native vlan" in x), "1") for n, b in trunks}
    allowed = {n: next((x.split()[-1] for x in b if "allowed vlan" in x), "1-4094") for n, b in trunks}
    lines = ["", "Port        Mode             Encapsulation  Status        Native vlan"]
    lines += [f"{_short(n):<11} on               802.1q         trunking      {native[n]}" for n, _ in trunks]
    for title in ("Vlans allowed on trunk", "Vlans allowed and active in management domain"):
        lines += ["", f"Port        {title}"]
        lines += [f"{_short(n):<11} {allowed[n]}" for n, _ in trunks]
    return "\n".join(lines) + "\n"


def cdp_neighbors_detail(
    hostname: str,
    links: tuple[tuple[str, str, str, str], ...],
    domain: str | None,
    mgmt_noise: tuple[str, str, str] | None,
) -> str:
    """`mgmt_noise` = (cổng quản lý của thiết bị này, láng giềng, cổng quản lý của láng giềng): thêm một
    láng giềng thấy qua mạng quản lý, như khi cổng đó CHƯA tắt CDP, để kiểm rằng collector bỏ nó."""
    entries = [(a_if, b, b_if) for a, a_if, b, b_if in links if a == hostname]
    entries += [(b_if, a, a_if) for a, a_if, b, b_if in links if b == hostname]
    if mgmt_noise is not None:
        entries.append(mgmt_noise)
    blocks: list[str] = []
    for local_if, neighbor, neighbor_if in entries:
        fqdn = f"{neighbor}.{domain}" if domain else neighbor
        blocks.append(
            "-------------------------\n"
            f"Device ID: {fqdn}\n"
            "Entry address(es): \n"
            "Platform: Linux Unix,  Capabilities: Router Switch IGMP \n"
            f"Interface: {local_if},  Port ID (outgoing port): {neighbor_if}\n"
            "Holdtime : 150 sec\n"
        )
    return "\n".join(blocks) + ("\n" if blocks else "") + f"\nTotal cdp entries displayed : {len(blocks)}\n"


def synthesize(
    baseline_dir: Path,
    inventory: Inventory,
    links: tuple[tuple[str, str, str, str], ...] = SMB01_LINKS,
    mgmt_noise: bool = True,
) -> dict[str, dict[str, str]]:
    """Output giả của mọi lệnh trong allowlist, cho mọi thiết bị của inventory, từ
    `<baseline_dir>/<host>.cfg`."""
    outputs: dict[str, dict[str, str]] = {}
    first = inventory.devices[0]
    for device in inventory.devices:
        baseline = (baseline_dir / f"{device.hostname}.cfg").read_text(encoding="utf-8")
        noise = None
        if mgmt_noise and device is not first:
            noise = (device.mgmt_interface, first.hostname, first.mgmt_interface)
        outputs[device.hostname] = {
            "show running-config": running_config(baseline),
            "show vlan brief": vlan_brief(baseline)
            if device.role != "router"
            else "% Invalid input detected\n",
            "show ip interface brief": ip_interface_brief(baseline),
            "show cdp neighbors detail": cdp_neighbors_detail(
                device.hostname, links, inventory.domain_name, noise
            ),
            "show interfaces trunk": interfaces_trunk(baseline),
            "show version": "Cisco IOS Software (SYNTHETIC FIXTURE)\n",
        }
    return outputs


def write_raw(directory: Path, outputs: dict[str, dict[str, str]]) -> None:
    """Ghi output giả NGUYÊN VĂN (còn chuỗi secret giả) ra `<directory>/<host>/<lệnh>.txt`. Chỉ dùng cho
    fixture của test: output thật phải đi qua `fetch.save_outputs`, nơi giá trị bí mật bị xoá."""
    for hostname, by_command in outputs.items():
        folder = directory / hostname
        folder.mkdir(parents=True, exist_ok=True)
        for command in ALLOWED_COMMANDS:
            (folder / command_file(command)).write_text(by_command[command], encoding="utf-8", newline="\n")


__all__ = ["FAKE_SECRET_VALUES", "SMB01_LINKS", "interface_name", "synthesize", "write_raw"]
