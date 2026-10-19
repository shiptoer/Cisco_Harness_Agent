"""Phân tích ngược CLI Cisco IOS → thao tác `{tool, args}`: "kiểm tra chạy lại độc lập" của evaluator (D-006).

Evaluator KHÔNG đọc `staged_ops.jsonl`, `change_request.json` hay sensors.json của harness: nó chỉ nhìn
thứ gửi người duyệt (`candidate/<host>.cfg`) và tự suy ra thao tác. Nhờ vậy:
- chấm được cả B0 (text do model gõ) lẫn B1–B4 (render từ template) bằng cùng một thước;
- harness hay template có lỗi (vd render sai VLAN) thì evaluator phát hiện, thay vì tin file của harness.

Parser này viết tay từ tài liệu lệnh IOS, KHÔNG import template hay registry khi parse. Chỉ ở bước cuối
nó gọi `parse_tool_call` để chuẩn hoá args (cùng dạng với nhãn corpus) — thao tác không chuẩn hoá được
(VLAN 0, tool không tồn tại trong registry) giữ nguyên với cờ `invalid`.

Dòng không hiểu được → `unparsed` (đếm riêng; với B0 là thước đo "model gõ gì ngoài từ vựng").

Task 6 (D-063): tên interface viết tắt (`Gi1/1`, `int fa0/5`) được khai triển về tên đầy đủ; `interface range`
(`Gi1/1 - 2, Gi1/5`) thành một khối áp cho từng interface; `shut` / `no shut`. Dòng vô hại (`BENIGN_LINE`:
`configure terminal`, `enable`, `show …`) VẪN nằm trong `unparsed` (luật chấm strict đã đăng ký không đổi);
evaluator dùng `BENIGN_LINE` để tính thêm cột lenient. `touched` ghi mọi interface có ít nhất một dòng
cấu hình trong khối (kể cả dòng không phân tích được), cho predicate `touches_protected_interface`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from harness_agent.models import parse_tool_call

_IP = r"(?:\d{1,3}\.){3}\d{1,3}"
_IFNAME = r"(?:GigabitEthernet|FastEthernet|Ethernet|Loopback|Vlan)\d+(?:/\d+){0,2}"
_EP = rf"any|host {_IP}|{_IP} {_IP}"
MAX_LINE = 240
# Số interface tối đa một dòng `interface range` được khai triển (text B0 untrusted: không khai triển dải
# khổng lồ).
MAX_RANGE = 64

# Viết tắt tên interface của IOS → tên đầy đủ (cùng tập tên với `InterfaceName` của contract).
_IF_PREFIX = {
    "gigabitethernet": "GigabitEthernet",
    "gigabit": "GigabitEthernet",
    "gig": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "fastethernet": "FastEthernet",
    "fast": "FastEthernet",
    "fa": "FastEthernet",
    "ethernet": "Ethernet",
    "eth": "Ethernet",
    "loopback": "Loopback",
    "loop": "Loopback",
    "lo": "Loopback",
    "vlan": "Vlan",
    "vl": "Vlan",
}
IF_ANY = r"(?:" + "|".join(sorted(_IF_PREFIX, key=len, reverse=True)) + r") ?\d+(?:/\d+){0,2}"
_IF_ITEM = re.compile(rf"^\s*({IF_ANY})\s*(?:-\s*(\d{{1,5}}))?\s*$", re.IGNORECASE)

_RE = {
    "vlan": re.compile(r"^vlan (\d{1,5})$"),
    "vlan_name": re.compile(r"^name (\S+)$"),
    "interface": re.compile(rf"^(?:interface|int) ({IF_ANY})$", re.IGNORECASE),
    "interface_range": re.compile(r"^(?:interface|int) range (.+)$", re.IGNORECASE),
    "sw_mode": re.compile(r"^switchport mode (access|trunk)$"),
    "sw_access": re.compile(r"^switchport access vlan (\d{1,5})$"),
    "sw_allowed": re.compile(r"^switchport trunk allowed vlan ([0-9,-]+)$"),
    "sw_native": re.compile(r"^switchport trunk native vlan (\d{1,5})$"),
    "ip_addr": re.compile(rf"^ip address ({_IP}) ({_IP})$"),
    "shut": re.compile(r"^(no )?shut(?:down)?$"),
    "desc": re.compile(r"^description (.*)$"),
    "acl_group": re.compile(r"^ip access-group (\S+) (in|out)$"),
    "route": re.compile(rf"^(no )?ip route ({_IP}) ({_IP}) ({_IP}|{_IFNAME})$"),
    "acl": re.compile(r"^ip access-list extended (\S+)$"),
    "ace": re.compile(
        rf"^(?:(\d{{1,10}}) )?(permit|deny) (ip|tcp|udp|icmp) ({_EP}) ({_EP})"
        r"(?: ((?:eq|gt|lt|neq) \d{1,5}))?$"
    ),
    "aaa_login": re.compile(r"^aaa authentication login (\S+) (.+)$"),
    "key_ref": re.compile(r"^! TACACS\+ key: <SECRET:([A-Za-z0-9_.-]+)>$"),
}
_SKIP = re.compile(
    r"^(!.*|exit|end|aaa new-model|switchport trunk encapsulation dot1q|line vty \d+ \d+"
    r"|login authentication \S+|remark .*)$"
)
# Dòng vô hại: đổi mode hoặc chỉ đọc, không đổi cấu hình. Vẫn là `unparsed` (strict); chỉ cột lenient bỏ qua.
# KHÔNG gồm lưu cấu hình (`write memory` nằm trong RISKY_LINE của evaluator, D-052).
BENIGN_LINE = re.compile(
    r"^(configure terminal|conf(?:ig(?:ure)?)? t(?:erm(?:inal)?)?|enable|end|exit|!.*|(?:do )?show .*"
    r"|terminal length \d+)$",
    re.IGNORECASE,
)


def normalize_interface(name: str) -> str:
    """`gi1/1`, `Gi 1/1`, `GigabitEthernet1/1` → `GigabitEthernet1/1`. Tiền tố lạ: giữ nguyên tên."""
    m = re.match(r"^([A-Za-z]+) ?(\d.*)$", name.strip())
    if not m:
        return name
    full = _IF_PREFIX.get(m.group(1).lower())
    return f"{full}{m.group(2)}" if full else name


def expand_interface_range(spec: str) -> list[str] | None:
    """`Gi1/1 - 2, Fa0/5` → [GigabitEthernet1/1, GigabitEthernet1/2, FastEthernet0/5]. Không hợp lệ hoặc quá
    `MAX_RANGE` interface → None (dòng vào `unparsed`)."""
    out: list[str] = []
    for item in spec.split(","):
        m = _IF_ITEM.match(item)
        if not m:
            return None
        first = normalize_interface(m.group(1))
        if m.group(2) is None:
            out.append(first)
            continue
        # Dải đổi số CUỐI của tên: `GigabitEthernet1/1 - 3` → 1/1, 1/2, 1/3.
        head, last = re.match(r"^(.*?)(\d+)$", first).groups()
        start, end = int(last), int(m.group(2))
        if end < start or end - start + 1 > MAX_RANGE:
            return None
        out += [f"{head}{n}" for n in range(start, end + 1)]
        if len(out) > MAX_RANGE:
            return None
    return out


@dataclass
class ParsedConfig:
    device: str
    ops: list[dict[str, Any]] = field(default_factory=list)  # {tool, args[, invalid: str]}
    unparsed: list[str] = field(default_factory=list)
    # Interface có ít nhất một dòng cấu hình trong khối (phân tích được hay không, trừ `BENIGN_LINE`),
    # theo thứ tự gặp.
    touched: list[str] = field(default_factory=list)


def parse_config(device: str, text: str) -> ParsedConfig:
    out = ParsedConfig(device)
    mode: str | None = None
    cur: dict[str, Any] = {}  # trạng thái khối hiện tại (interface/vlan/acl)
    key_ref: str | None = None

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        if cur["kind"] == "interface" and cur["lines"]:
            out.touched += [i for i in cur["interfaces"] if i not in out.touched]
        for op in _ops_from_block(device, cur):
            out.ops.append(_normalise(op))
        cur = {}

    def unparsed(text_: str) -> None:
        if mode == "interface":
            cur["lines"] += 1
        out.unparsed.append(text_)

    for raw in text.split("\n"):
        line = raw.strip()
        if len(line) > MAX_LINE:
            # Text B0 do model gõ là untrusted: dòng quá dài (IOS giới hạn ~240) không phân tích, tránh DoS.
            unparsed(line[:MAX_LINE] + "…")
            continue
        if not line:
            continue
        m = _RE["key_ref"].match(line)
        if m:
            key_ref = m.group(1)
            continue
        if _SKIP.match(line):
            if line in ("exit", "end"):
                flush()
                mode = None
            elif mode == "interface" and not line.startswith("!"):
                cur["lines"] += 1  # vd `switchport trunk encapsulation dot1q`: vẫn là cấu hình của interface
            continue
        m = _RE["vlan"].match(line)
        if m:
            flush()
            mode, cur = "vlan", {"kind": "vlan", "vlan_id": int(m.group(1))}
            continue
        m = _RE["interface_range"].match(line)
        if m:
            flush()
            names = expand_interface_range(m.group(1))
            if names is None:
                mode = None
                out.unparsed.append(line)
            else:
                mode, cur = "interface", {"kind": "interface", "interfaces": names, "lines": 0}
            continue
        m = _RE["interface"].match(line)
        if m:
            flush()
            mode = "interface"
            cur = {"kind": "interface", "interfaces": [normalize_interface(m.group(1))], "lines": 0}
            continue
        m = _RE["acl"].match(line)
        if m:
            flush()
            mode, cur = "acl", {"kind": "acl", "acl_name": m.group(1), "entries": []}
            continue
        m = _RE["route"].match(line)
        if m:
            flush()
            mode = None
            out.ops.append(_normalise(_route_op(device, m)))
            continue
        m = _RE["aaa_login"].match(line)
        if m:
            flush()
            mode = None
            methods = _split_methods(m.group(2))
            args: dict[str, Any] = {"device": device, "list_name": m.group(1), "methods": methods}
            if "group tacacs+" in methods and key_ref:
                args["tacacs_key_ref"] = key_ref
            out.ops.append(_normalise({"tool": "set_aaa_login", "args": args}))
            continue
        if mode == "vlan" and (m := _RE["vlan_name"].match(line)):
            cur["name"] = m.group(1)
            continue
        if mode == "interface":
            # Dòng vô hại (`do show …`) trong khối không phải cấu hình của interface: không tính là "chạm".
            if not BENIGN_LINE.match(line):
                cur["lines"] += 1
            if m := _RE["sw_mode"].match(line):
                cur["sw_mode"] = m.group(1)
                continue
            if m := _RE["sw_access"].match(line):
                cur["access_vlan"] = int(m.group(1))
                continue
            if m := _RE["sw_allowed"].match(line):
                cur["allowed"] = m.group(1)
                continue
            if m := _RE["sw_native"].match(line):
                cur["native"] = int(m.group(1))
                continue
            if m := _RE["ip_addr"].match(line):
                cur["ip"], cur["mask"] = m.group(1), m.group(2)
                continue
            if m := _RE["shut"].match(line):
                cur["enabled"] = bool(m.group(1))
                continue
            if m := _RE["desc"].match(line):
                cur["text"] = m.group(1)
                continue
            if m := _RE["acl_group"].match(line):
                cur["acl_group"] = (m.group(1), m.group(2))
                continue
        if mode == "acl" and (m := _RE["ace"].match(line)):
            cur["entries"].append(m.groups())
            continue
        out.unparsed.append(line)
    flush()
    return out


def _route_op(device: str, m: re.Match[str]) -> dict[str, Any]:
    negate, net, mask, nh = m.groups()
    try:
        prefix = str(ipaddress.IPv4Network(f"{net}/{mask}", strict=False))
    except ValueError:
        prefix = f"{net}/{mask}"
    if negate:
        return {"tool": "remove_static_route", "args": {"device": device, "prefix": prefix, "next_hop": nh}}
    key = "next_hop" if nh[0].isdigit() else "exit_interface"
    return {"tool": "add_static_route", "args": {"device": device, "prefix": prefix, key: nh}}


def _ops_from_block(device: str, cur: dict[str, Any]) -> list[dict[str, Any]]:
    kind = cur["kind"]
    if kind == "vlan":
        return [
            {
                "tool": "create_vlan",
                "args": {"device": device, "vlan_id": cur["vlan_id"], "name": cur.get("name", "")},
            }
        ]
    if kind == "acl":
        ops = []
        for seq, action, proto, src, dst, port in cur["entries"]:
            args = {
                "device": device,
                "acl_name": cur["acl_name"],
                "seq": int(seq) if seq else 0,
                "action": action,
                "protocol": proto,
                "src": _endpoint_to_cidr(src),
                "dst": _endpoint_to_cidr(dst),
            }
            if port:
                args["dst_port"] = port
            ops.append({"tool": "add_acl_entry", "args": args})
        return ops
    # interface (một interface, hoặc từng interface của `interface range`)
    return [op for itf in cur["interfaces"] for op in _interface_ops(device, itf, cur)]


def _interface_ops(device: str, itf: str, cur: dict[str, Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    if cur.get("sw_mode") == "access" or ("access_vlan" in cur and cur.get("sw_mode") != "trunk"):
        ops.append(
            {
                "tool": "set_access_vlan",
                "args": {"device": device, "interface": itf, "vlan_id": cur.get("access_vlan", 0)},
            }
        )
    if cur.get("sw_mode") == "trunk" or "allowed" in cur or "native" in cur:
        ops.append(
            {
                "tool": "set_trunk",
                "args": {
                    "device": device,
                    "interface": itf,
                    "allowed_vlans": _expand_vlan_list(cur.get("allowed", "")),
                    "native_vlan": cur.get("native", 1),
                },
            }
        )
    if "ip" in cur:
        try:
            plen = ipaddress.IPv4Network(f"0.0.0.0/{cur['mask']}").prefixlen
        except ValueError:
            plen = 0
        ops.append(
            {
                "tool": "set_interface_ip",
                "args": {"device": device, "interface": itf, "ip": cur["ip"], "prefix_len": plen},
            }
        )
    if "enabled" in cur:
        ops.append(
            {
                "tool": "set_interface_state",
                "args": {"device": device, "interface": itf, "enabled": cur["enabled"]},
            }
        )
    if "text" in cur:
        ops.append(
            {
                "tool": "set_interface_description",
                "args": {"device": device, "interface": itf, "text": cur["text"]},
            }
        )
    if "acl_group" in cur:
        name, direction = cur["acl_group"]
        ops.append(
            {
                "tool": "apply_acl",
                "args": {"device": device, "interface": itf, "acl_name": name, "direction": direction},
            }
        )
    return ops


def _endpoint_to_cidr(ep: str) -> str:
    if ep == "any" or ep.startswith("host "):
        return ep
    addr, wild = ep.split(" ")
    try:
        mask = (~int(ipaddress.IPv4Address(wild))) & 0xFFFFFFFF
        return str(ipaddress.IPv4Network((addr, str(ipaddress.IPv4Address(mask))), strict=False))
    except ValueError:
        return ep


def _expand_vlan_list(value: str) -> list[int]:
    """Danh sách VLAN IOS → list. Mọi giá trị kẹp trong 0–4095: số ngoài dải vẫn giữ (để registry đánh dấu
    `invalid`) nhưng một dải như `1-999999999` không bao giờ được khai triển thành tỉ phần tử."""
    out: set[int] = set()
    for part in value.split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()) or len(lo) > 5 or len(hi) > 5:
            out.add(0)  # không hợp lệ → registry sẽ từ chối (VLAN 0)
            continue
        a = min(int(lo), 4095)
        b = min(int(hi), 4095) if hi else a
        out.update(range(a, b + 1) if a <= b else (0,))
    return sorted(out)


def _split_methods(text: str) -> list[str]:
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "group" and i + 1 < len(tokens):
            out.append(f"group {tokens[i + 1]}")
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def _normalise(op: dict[str, Any]) -> dict[str, Any]:
    """Chuẩn hoá args qua registry (cùng dạng với nhãn). Không hợp lệ thì giữ nguyên + cờ `invalid`."""
    try:
        return parse_tool_call(op).model_dump(mode="json")
    except ValidationError as exc:
        err = exc.errors()[0]
        return {**op, "invalid": f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}"}
