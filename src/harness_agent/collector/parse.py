"""Dựng dữ liệu của `NetworkContext` từ output `show` của Cisco IOS. Chỉ là hàm thuần: không mạng, không file.

Nguyên tắc:
- Giá trị secret bị XOÁ ở bước đầu tiên (`sanitize`), trước mọi bước parse: enable secret, secret của
  username, key của TACACS/RADIUS, password, community... Phần còn lại của module chỉ thấy text đã xoá.
  Context ghi TÊN tham chiếu (`secret_ref`, `key_ref`) như contract; giá trị trong `Device.secrets` là
  chuỗi giữ chỗ `REDACTED`, không phải giá trị thật và không phải hash.
- Không đoán: thứ contract không mô tả được (VRF, banner login, tên route, `log` của ACL, subinterface...)
  thì KHÔNG đưa vào context, và được ĐẾM trong ghi chú của thiết bị. Ghi chú không bao giờ chép dòng cấu
  hình nguyên văn: chỉ có tên loại, tên interface, tên ACL và số đếm.
- Output của thiết bị là dữ liệu không tin cậy: contract (`NetworkContext`) validate lại toàn bộ sau đó.

Trạng thái: ĐỀ XUẤT (D-047).
"""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Giá trị giữ chỗ trong `Device.secrets`. Contract đòi mỗi `secret_ref` / `key_ref` trỏ tới một khoá có
# giá trị; collector không bao giờ giữ giá trị thật, nên mọi khoá mang chuỗi này.
REDACTED = "redacted-by-collector"
_REMOVED = "<removed>"

# Dòng mang giá trị bí mật → giữ phần đầu (nhóm 1), bỏ phần còn lại. Thứ tự có ý nghĩa: mẫu cụ thể trước.
_SECRET_LINES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(\s*enable (?:secret|password))\s+.*$"),
    re.compile(r"^(\s*username \S+.*?\b(?:secret|password))\s+.*$"),
    re.compile(r"^(\s+key)\s+.*$"),  # trong khối `tacacs server` / `radius server` / `key chain`
    re.compile(r"^(\s*key-string)\s+.*$"),
    re.compile(r"^(\s*(?:tacacs-server|radius-server)\b.*?\bkey)\s+.*$"),
    re.compile(r"^(\s*snmp-server community)\s+.*$"),
    re.compile(r"^(\s*crypto isakmp key)\s+.*$"),
    re.compile(r"^(.*\bpre-shared-key)\s+.*$"),
    re.compile(r"^(.*\b(?:password|secret))\s+.*$"),  # `password 7 …` ở line, neighbor, ppp...
)


def sanitize(text: str) -> tuple[str, int]:
    """(text đã xoá mọi giá trị bí mật, số dòng đã xoá). Dùng cho output của `show running-config` trước
    khi parse VÀ trước khi lưu ra đĩa."""
    out: list[str] = []
    removed = 0
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        for pattern in _SECRET_LINES:
            match = pattern.match(line)
            if match:
                line = f"{match.group(1)} {_REMOVED}"
                removed += 1
                break
        out.append(line)
    return "\n".join(out), removed


_IF_PREFIXES = (
    ("GigabitEthernet", ("gigabitethernet", "gi", "gig")),
    ("FastEthernet", ("fastethernet", "fa")),
    ("Ethernet", ("ethernet", "eth", "et")),
    ("Loopback", ("loopback", "lo")),
    ("Vlan", ("vlan", "vl")),
)


def interface_name(raw: str) -> str:
    """Tên interface đầy đủ: `Et0/1`, `Eth0/1` → `Ethernet0/1`. Tên không nhận ra được trả nguyên văn
    (contract sẽ từ chối nếu nó không hợp lệ)."""
    match = re.fullmatch(r"([A-Za-z-]+)\s*([\d/.:]+)", raw.strip())
    if not match:
        return raw.strip()
    head, tail = match.group(1).lower(), match.group(2)
    for full, shorts in _IF_PREFIXES:
        if head in shorts:
            return full + tail
    return raw.strip()


@dataclass
class DeviceNotes:
    """Những gì collector thấy trên một thiết bị mà KHÔNG đưa được vào context. Không có giá trị secret,
    không có dòng cấu hình nguyên văn."""

    secret_lines_removed: int = 0
    # interface → tên VRF (contract không có trường VRF).
    vrf_interfaces: dict[str, str] = field(default_factory=dict)
    # Tên interface bị bỏ vì contract không nhận tên đó (vd subinterface `Ethernet0/1.110`).
    skipped_interfaces: list[str] = field(default_factory=list)
    # Loại cấu hình không mô tả được → số lần gặp.
    not_represented: Counter[str] = field(default_factory=Counter)
    # Lệnh cấp gốc mà parser không xử lý, đếm theo TỪ ĐẦU TIÊN của dòng.
    unparsed: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "secret_lines_removed": self.secret_lines_removed,
            "vrf_interfaces": dict(sorted(self.vrf_interfaces.items())),
            "skipped_interfaces": sorted(self.skipped_interfaces),
            "not_represented": dict(sorted(self.not_represented.items())),
            "unparsed": dict(sorted(self.unparsed.items())),
        }


_VALID_IF = re.compile(r"(GigabitEthernet|FastEthernet|Ethernet|Loopback|Vlan)\d+(/\d+){0,2}")
_AUTH_METHODS = {"local": "local"}
_IGNORED_TOP = frozenset(
    {
        "!",
        "end",
        "version",
        "service",
        "boot-start-marker",
        "boot-end-marker",
        "building",
        "current",
        "spanning-tree",
        "crypto",
        "control-plane",
        "no",
        "vtp",
        "logging",
        "clock",
        "mmi",
        "redundancy",
        "ip",
    }
)


def parse_running_config(text: str, role: str) -> tuple[dict[str, Any], DeviceNotes]:
    """`show running-config` của MỘT thiết bị → (các trường lấy được từ thiết bị của `Device`, ghi chú).

    `role` (từ inventory) quyết định mặc định của cổng: trên router mọi cổng là routed. Kết quả CHƯA có
    `platform`, `role`, `site`: ba trường đó lấy từ inventory, không lấy từ thiết bị.
    """
    if len(text) > 4 * 1024 * 1024 or any(len(line) > 24000 for line in text.splitlines()):
        raise ValueError("running-config exceeds parser bounds")
    clean, removed = sanitize(text)
    notes = DeviceNotes(secret_lines_removed=removed)
    is_router = role == "router"
    device: dict[str, Any] = {
        "hostname": "",
        # IOS chỉ in dòng NGƯỢC với mặc định: `ip routing` đang bật thì running-config không có dòng nào,
        # tắt thì có `no ip routing`. Đã thấy trên output thật của image IOL L2 (lab smb-01, 13/10/2026):
        # CORE1 và ACC-BR1 định tuyến được mà không có dòng `ip routing`; ACC1–3 có `no ip routing`.
        # (Một số dòng Catalyst có mặc định ngược lại; collector này chỉ nhắm image IOL của lab.)
        "ip_routing": True,
        "interfaces": [],
        "vlans": [],
        "static_routes": [],
        "acls": {},
        "banner_motd": "",
        "secrets": {},
    }
    aaa: dict[str, Any] = {
        "new_model": False,
        "login_lists": {},
        "vty_login_list": None,
        "local_users": [],
        "tacacs_servers": [],
    }
    vlans: dict[int, str] = {}
    saw_aaa = False
    vty_list: str | None = None

    blocks = _blocks(clean)
    for head, body in blocks:
        words = head.split()
        key = words[0] if words else ""
        if key == "hostname" and len(words) == 2:
            device["hostname"] = words[1]
        elif head == "ip routing":
            device["ip_routing"] = True
        elif head == "no ip routing":
            device["ip_routing"] = False
        elif head.startswith("ip default-gateway ") and len(words) == 3:
            device["default_gateway"] = words[2]
        elif head.startswith("enable secret") or head.startswith("enable password"):
            device["secrets"]["enable_secret"] = REDACTED
        elif key == "username" and len(words) >= 2:
            saw_aaa = True
            user = words[1]
            privilege = int(words[words.index("privilege") + 1]) if "privilege" in words[:-1] else 1
            aaa["local_users"].append(
                {"username": user, "privilege": privilege, "secret_ref": f"local_{user}"}
            )
            device["secrets"][f"local_{user}"] = REDACTED
        elif head == "aaa new-model":
            saw_aaa = True
            aaa["new_model"] = True
        elif head.startswith("aaa authentication login ") and len(words) >= 5:
            saw_aaa = True
            methods = _auth_methods(words[4:])
            if methods is None:
                notes.not_represented["aaa_login_method"] += 1
            else:
                aaa["login_lists"][words[3]] = methods
        elif head.startswith("aaa "):
            notes.not_represented["aaa_" + words[1]] += 1
        elif head.startswith("tacacs server ") and len(words) == 3:
            saw_aaa = True
            _tacacs_server(words[2], body, aaa, device["secrets"], notes)
        elif head.startswith("vrf definition") or head.startswith("ip vrf "):
            notes.not_represented["vrf_definition"] += 1
        elif key == "vlan" and len(words) == 2:
            _vlan_block(words[1], body, vlans)
        elif key == "interface" and len(words) == 2:
            _interface(words[1], body, is_router, device, notes)
        elif head.startswith("ip route "):
            _static_route(words, device, notes)
        elif head.startswith("ip access-list ") and len(words) == 4:
            _acl(words[2], words[3], body, device, notes)
        elif key == "banner" and len(words) >= 2:
            if words[1] == "motd":
                device["banner_motd"] = " ".join(x.strip() for x in body if x.strip())[:240]
            else:
                notes.not_represented["banner_" + words[1]] += 1
        elif key == "line":
            if len(words) >= 2 and words[1] == "vty":
                for line in body:
                    parts = line.split()
                    if parts[:2] == ["login", "authentication"] and len(parts) == 3:
                        vty_list = parts[2]
                    elif parts[:1] == ["access-class"]:
                        notes.not_represented["vty_access_class"] += 1
        elif key.lower() not in _IGNORED_TOP and head:
            notes.unparsed[key] += 1

    device["vlans"] = [{"vlan_id": vid, "name": name} for vid, name in sorted(vlans.items())]
    security = {
        "vrf_interfaces": dict(notes.vrf_interfaces),
        "vrf_definitions": [],
        "vty": [],
        "aaa": [],
        "unsupported": sorted(notes.not_represented),
    }
    for head, body in blocks:
        if head.startswith(("vrf definition ", "ip vrf ")):
            security["vrf_definitions"].append(head[:240])
        elif head.startswith("line vty "):
            security["vty"].append(head[:240])
            security["vty"].extend(
                line[:240]
                for line in body
                if line.startswith(("access-class ", "login ", "transport input "))
            )
        elif head.startswith("aaa "):
            security["aaa"].append(head[:240])
    if any(security.values()):
        device["collected_security"] = security
    if saw_aaa:
        # VTY không khai `login authentication` thì dùng list `default` (hành vi của IOS khi có
        # aaa new-model).
        aaa["vty_login_list"] = vty_list or ("default" if "default" in aaa["login_lists"] else None)
        device["aaa"] = aaa
    return device, notes


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """Chia running-config thành các khối (dòng cấp gốc, các dòng con thụt vào). Banner được gom theo ký tự
    phân cách của nó."""
    blocks: list[tuple[str, list[str]]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line.strip() or line.lstrip().startswith("!"):
            continue
        banner = re.match(r"banner (\S+) (.)(.*)$", line)
        if banner and not line.startswith(" "):
            delim, rest = banner.group(2), banner.group(3)
            body: list[str] = []
            if delim in rest:  # banner một dòng: `banner motd #text#`
                body.append(rest.split(delim, 1)[0])
            else:
                if rest:
                    body.append(rest)
                while i < len(lines) and delim not in lines[i]:
                    body.append(lines[i])
                    i += 1
                if i < len(lines):
                    tail = lines[i].split(delim, 1)[0]
                    if tail:
                        body.append(tail)
                    i += 1
            blocks.append((f"banner {banner.group(1)}", body))
            continue
        if line.startswith(" "):
            if blocks:
                blocks[-1][1].append(line.strip())
            continue
        blocks.append((" ".join(line.split()), []))
    return blocks


def _auth_methods(tokens: list[str]) -> list[str] | None:
    """Danh sách phương thức theo contract (`group tacacs+`, `group radius`, `local`); None nếu có phương
    thức contract không có (`enable`, `none`, `line`, nhóm server tự đặt tên)."""
    methods: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "group" and i + 1 < len(tokens) and tokens[i + 1] in ("tacacs+", "radius"):
            methods.append(f"group {tokens[i + 1]}")
            i += 2
        elif tokens[i] in _AUTH_METHODS:
            methods.append(_AUTH_METHODS[tokens[i]])
            i += 1
        else:
            return None
    return methods or None


def _tacacs_server(
    name: str, body: list[str], aaa: dict[str, Any], secrets: dict[str, str], notes: DeviceNotes
) -> None:
    address = None
    has_key = False
    for line in body:
        parts = line.split()
        if parts[:2] == ["address", "ipv4"] and len(parts) == 3:
            address = parts[2]
        elif parts[:1] == ["key"]:
            has_key = True
        else:
            notes.not_represented["tacacs_server_option"] += 1
    if address is None or not has_key:
        notes.not_represented["tacacs_server_incomplete"] += 1
        return
    key_ref = "tacacs_key" if not aaa["tacacs_servers"] else f"tacacs_key_{name}"
    aaa["tacacs_servers"].append({"name": name, "address": address, "key_ref": key_ref})
    secrets[key_ref] = REDACTED


def _vlan_list(text: str) -> list[int]:
    """`10,20,30-32` → [10, 20, 30, 31, 32]. Raise ValueError nếu không phải danh sách số."""
    out: set[int] = set()
    if len(text) > 24000:
        raise ValueError("VLAN list too long")
    for part in text.split(","):
        if not re.fullmatch(r"[0-9]{1,4}(?:-[0-9]{1,4})?", part):
            raise ValueError("invalid VLAN list")
        low, _, high = part.partition("-")
        first, last = int(low), int(high or low)
        if not 1 <= first <= last <= 4094:
            raise ValueError("invalid VLAN range")
        out.update(range(first, last + 1))
    return sorted(out)


def _vlan_block(spec: str, body: list[str], vlans: dict[int, str]) -> None:
    ids = _vlan_list(spec)
    name = next((line.split(None, 1)[1] for line in body if line.startswith("name ")), None)
    for vid in ids:
        # Tên mặc định của IOS cho VLAN không đặt tên.
        vlans[vid] = name if name and len(ids) == 1 else vlans.get(vid, f"VLAN{vid:04d}")


def _interface(
    raw_name: str, body: list[str], is_router: bool, device: dict[str, Any], notes: DeviceNotes
) -> None:
    name = interface_name(raw_name)
    if not _VALID_IF.fullmatch(name):
        notes.skipped_interfaces.append(name[:60])
        notes.not_represented["interface_name"] += 1
        return
    shutdown = False
    no_switchport = False
    switch_mode: str | None = None
    itf: dict[str, Any] = {"name": name}
    for line in body:
        parts = line.split()
        if line == "shutdown":
            shutdown = True
        elif line == "no shutdown":
            shutdown = False
        elif line == "no switchport":
            no_switchport = True
        elif parts[:1] == ["description"]:
            itf["description"] = line.split(None, 1)[1] if len(parts) > 1 else ""
        elif parts[:2] == ["switchport", "mode"] and len(parts) == 3:
            switch_mode = parts[2]
        elif parts[:3] == ["switchport", "access", "vlan"] and len(parts) == 4:
            itf["access_vlan"] = int(parts[3])
        elif parts[:4] == ["switchport", "trunk", "native", "vlan"] and len(parts) == 5:
            itf["native_vlan"] = int(parts[4])
        elif parts[:4] == ["switchport", "trunk", "allowed", "vlan"] and len(parts) >= 5:
            universe = set(range(1, 4095))
            tokens = parts[4:]
            current = set(itf.get("trunk_allowed", universe))
            if tokens == ["all"]:
                current = universe
            elif tokens == ["none"]:
                current = set()
            elif len(tokens) == 1:
                current = set(_vlan_list(tokens[0]))
            elif len(tokens) == 2 and tokens[0] in {"add", "remove", "except"}:
                extra = set(_vlan_list(tokens[1]))
                if tokens[0] == "add":
                    current |= extra
                elif tokens[0] == "remove":
                    current -= extra
                else:
                    current = universe - extra
            else:
                raise ValueError("unsupported trunk VLAN expression")
            itf["trunk_allowed"] = sorted(current)
            itf["trunk_allowed_scope"] = "ios"
        elif line == "no switchport trunk allowed vlan":
            itf["trunk_allowed"] = list(range(1, 4095))
            itf["trunk_allowed_scope"] = "ios"
        elif parts[:2] == ["ip", "address"] and len(parts) == 4:
            itf["ipv4"] = str(ipaddress.IPv4Interface(f"{parts[2]}/{parts[3]}"))
        elif parts[:2] == ["ip", "address"] and len(parts) > 4:
            notes.not_represented["secondary_address"] += 1
        elif parts[:2] == ["ip", "access-group"] and len(parts) == 4 and parts[3] in ("in", "out"):
            itf["acl_in" if parts[3] == "in" else "acl_out"] = parts[2]
        elif parts[:2] == ["vrf", "forwarding"] or parts[:3] == ["ip", "vrf", "forwarding"]:
            notes.vrf_interfaces[name] = parts[-1][:32]
            notes.not_represented["vrf_forwarding"] += 1
    logical = name.startswith(("Vlan", "Loopback"))
    if is_router or logical or no_switchport:
        mode = "routed"
    elif switch_mode == "trunk":
        mode = "trunk"
    elif shutdown:
        mode = "unused"
    else:
        mode = "access"
        itf.setdefault("access_vlan", 1)  # mặc định của IOS
    itf["mode"] = mode
    itf["enabled"] = not shutdown
    if mode == "trunk":
        itf.setdefault("trunk_allowed", list(range(1, 4095)))
        itf.setdefault("trunk_allowed_scope", "ios")
    if mode != "trunk":
        itf.pop("trunk_allowed", None)
        itf.pop("trunk_allowed_scope", None)
        itf.pop("native_vlan", None)
    if mode in ("trunk", "routed"):
        itf.pop("access_vlan", None)
    device["interfaces"].append(itf)


def _static_route(words: list[str], device: dict[str, Any], notes: DeviceNotes) -> None:
    """`ip route <prefix> <mask> <next-hop> [name X]`. Route trong VRF và route theo cổng ra không mô tả
    được bằng contract (không có VRF; `StaticRoute` đòi `next_hop`)."""
    if len(words) >= 3 and words[2] == "vrf":
        notes.not_represented["vrf_route"] += 1
        return
    if len(words) < 5:
        notes.not_represented["route_other"] += 1
        return
    try:
        prefix = ipaddress.IPv4Network(f"{words[2]}/{words[3]}")
        next_hop = ipaddress.IPv4Address(words[4])
    except ValueError:
        notes.not_represented["route_exit_interface"] += 1
        return
    if "name" in words[5:]:
        notes.not_represented["route_name"] += 1
    if words[5:] and not (len(words[5:]) == 2 and words[5] == "name"):
        notes.not_represented["route_option"] += 1
    device["static_routes"].append({"prefix": str(prefix), "next_hop": str(next_hop)})


def _endpoint(tokens: list[str]) -> tuple[str, int] | None:
    """Đầu mút ACL ở đầu `tokens` → (dạng contract, số token đã dùng); None nếu không nhận ra."""
    if not tokens:
        return None
    if tokens[0] == "any":
        return "any", 1
    if tokens[0] == "host" and len(tokens) >= 2:
        return f"host {tokens[1]}", 2
    if len(tokens) >= 2 and _is_ipv4(tokens[0]) and _is_ipv4(tokens[1]):
        return f"{tokens[0]} {tokens[1]}", 2
    return None


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _acl(kind: str, name: str, body: list[str], device: dict[str, Any], notes: DeviceNotes) -> None:
    """ACL có tên. `show running-config` không in số thứ tự: dùng số mặc định của IOS (10, 20, 30...), trừ
    khi dòng có số ở đầu. ACL standard được ghi như `ip <nguồn> any` (cùng nghĩa); dòng có thứ contract
    không mô tả được (cổng nguồn, `range`, `established`...) thì bỏ CẢ dòng và đếm, không ghi sai nghĩa."""
    if kind not in ("extended", "standard"):
        notes.not_represented["acl_kind"] += 1
        return
    if kind == "standard":
        notes.not_represented["acl_standard_as_extended"] += 1
    entries: list[dict[str, Any]] = []
    remark: str | None = None
    seq = 0
    for line in body:
        tokens = line.split()
        seq += 10
        if tokens and tokens[0].isdigit():
            seq = int(tokens[0])
            tokens = tokens[1:]
        if tokens[:1] == ["remark"]:
            remark = " ".join(tokens[1:])[:240]
            seq -= 10
            continue
        if not tokens or tokens[0] not in ("permit", "deny"):
            notes.not_represented["acl_line"] += 1
            continue
        entry = _acl_entry(kind, tokens, notes)
        if entry is None:
            notes.not_represented["acl_entry_unsupported"] += 1
            continue
        entry["seq"] = seq
        if remark is not None:
            entry["remark"] = remark
            remark = None
        entries.append(entry)
    device["acls"][name] = entries


def _acl_entry(kind: str, tokens: list[str], notes: DeviceNotes) -> dict[str, Any] | None:
    action, rest = tokens[0], tokens[1:]
    if rest[-1:] == ["log"]:
        notes.not_represented["acl_log"] += 1
        rest = rest[:-1]
    if kind == "standard":
        if len(rest) == 1 and _is_ipv4(rest[0]):
            rest = ["host", rest[0]]  # địa chỉ trần trong ACL standard là một host
        src = _endpoint(rest)
        if src is None or src[1] != len(rest):
            return None
        return {"action": action, "protocol": "ip", "src": src[0], "dst": "any"}
    if not rest or rest[0] not in ("ip", "tcp", "udp", "icmp"):
        return None
    protocol, rest = rest[0], rest[1:]
    src = _endpoint(rest)
    if src is None:
        return None
    rest = rest[src[1] :]
    dst = _endpoint(rest)
    if dst is None:
        return None
    rest = rest[dst[1] :]
    entry: dict[str, Any] = {"action": action, "protocol": protocol, "src": src[0], "dst": dst[0]}
    if rest[:1] == ["eq"] and len(rest) == 2 and rest[1].isdigit():
        entry["dst_port"] = int(rest[1])
    elif rest:
        return None
    return entry


# ------------------------------------------------------------------ các lệnh show khác
def parse_cdp_neighbors(text: str) -> list[tuple[str, str, str]]:
    """`show cdp neighbors detail` → [(cổng của thiết bị này, hostname láng giềng, cổng của láng giềng)].
    Hostname bỏ phần tên miền (`CORE1.smb.lab` → `CORE1`)."""
    out: list[tuple[str, str, str]] = []
    neighbor: str | None = None
    for line in text.replace("\r", "").split("\n"):
        device = re.match(r"\s*Device ID:\s*(\S+)", line)
        if device:
            neighbor = device.group(1).split(".", 1)[0]
            continue
        ports = re.match(r"\s*Interface:\s*([^,]+),\s*Port ID \(outgoing port\):\s*(.+?)\s*$", line)
        if ports and neighbor:
            out.append((interface_name(ports.group(1)), neighbor, interface_name(ports.group(2))))
            neighbor = None
    return out


def parse_vlan_brief(text: str) -> dict[int, str]:
    """`show vlan brief` → {vlan_id: tên}. Dùng để đối chiếu với running-config."""
    vlans: dict[int, str] = {}
    for line in text.replace("\r", "").split("\n"):
        match = re.match(r"^(\d{1,4})\s+(\S+)\s+(?:active|act/unsup|suspended)", line)
        if match:
            vlans[int(match.group(1))] = match.group(2)
    return vlans


def parse_ip_interface_brief(text: str) -> dict[str, dict[str, str]]:
    """`show ip interface brief` → {interface: {"ip", "status", "protocol"}}. `administratively down`
    được giữ nguyên là một trạng thái."""
    out: dict[str, dict[str, str]] = {}
    for line in text.replace("\r", "").split("\n"):
        match = re.match(
            r"^(\S+)\s+(\S+)\s+\S+\s+\S+\s+(administratively down|up|down|deleted)\s+(up|down)\s*$", line
        )
        if match and match.group(1) != "Interface":
            out[interface_name(match.group(1))] = {
                "ip": match.group(2),
                "status": match.group(3),
                "protocol": match.group(4),
            }
    return out


def parse_interfaces_trunk(text: str) -> dict[str, dict[str, Any]]:
    """`show interfaces trunk` → {interface: {"native": int, "allowed": [vlan...]}}, lấy từ bảng đầu (native)
    và bảng `Vlans allowed on trunk`."""
    out: dict[str, dict[str, Any]] = {}
    section = ""
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("Port"):
            section = line
            continue
        parts = line.split()
        if not parts:
            continue
        name = interface_name(parts[0])
        if "Native vlan" in section and len(parts) >= 5 and parts[-1].isdigit():
            out.setdefault(name, {})["native"] = int(parts[-1])
        elif "Vlans allowed on trunk" in section and len(parts) == 2:
            try:
                out.setdefault(name, {})["allowed"] = _vlan_list(parts[1])
            except ValueError:
                continue
    return out


def identify_platform(text: str) -> str:
    """Identify OS family from show version; never infer IOSv/L2 capability from an SSH banner."""
    if re.search(r"Cisco.*(?:NX-OS|Nexus Operating System)", text, re.I):
        return "cisco_nxos"
    if re.search(r"Cisco (?:IOS(?: XE)? Software|Internetwork Operating System)", text, re.I):
        return "cisco_ios"
    if re.search(r"JUNOS", text, re.I):
        return "juniper_junos"
    return "unknown"
