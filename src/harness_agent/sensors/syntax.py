"""Sensor syntax (iv-1): kiểm tra cú pháp Cisco IOS của cấu hình đã render, tất định, không cần thiết bị.

Cách làm: máy trạng thái theo MODE (global → vlan / interface / ip access-list / line), mỗi mode có một
bảng lệnh hợp lệ (regex + kiểm tra ngữ nghĩa nhỏ: dải VLAN, IP, netmask, danh sách VLAN). Lệnh không có
trong bảng của mode hiện tại là lỗi. Đây là ALLOWLIST: chỉ các lệnh mà tool registry có thể sinh ra, cộng
vài lệnh "vô hại" hay gặp (`!`, `exit`, `end`). Mục đích không phải parse toàn bộ IOS, mà là:

1. Bắt template bị sửa sai (vd thiếu `interface` trước `switchport ...`, VLAN 0, mask không liên tục).
2. Bắt mọi lệnh ngoài từ vựng của tool (vd `username`, `enable secret`) nếu có đường nào đó lọt tới
   change package — lớp phòng thủ sau gate, độc lập với gate.

Độc lập với template: không import registry; bảng lệnh viết tay từ tài liệu lệnh IOS 15.
Cú pháp thật trên EVE-NG (task 3.5) là bước kiểm chứng tiếp theo cho bảng này.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass

from .report import Finding

SENSOR = "syntax"

_IP = r"(\d{1,3}\.){3}\d{1,3}"
_IFNAME = r"(GigabitEthernet|FastEthernet|Ethernet|Loopback|Vlan)\d+(/\d+){0,2}"
_NAME = r"[A-Za-z0-9_-]{1,32}"
_ACL_EP = rf"(any|host {_IP}|{_IP} {_IP})"
_AUTH = r"(group tacacs\+|group radius|local|enable)"

Check = Callable[[re.Match[str]], str | None]  # trả None nếu đạt, hoặc lý do


def _vlan_ok(value: str) -> str | None:
    if not 1 <= int(value) <= 4094:
        return f"VLAN {value} ngoài dải 1–4094"
    return None


def _ip_ok(value: str) -> str | None:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return f"địa chỉ IPv4 không hợp lệ: {value}"
    return None


def _mask_ok(value: str) -> str | None:
    bad = _ip_ok(value)
    if bad:
        return bad
    bits = int(ipaddress.IPv4Address(value))
    # Mask hợp lệ: các bit 1 liền nhau từ trái. (~bits & 0xFFFFFFFF) + 1 phải là luỹ thừa của 2.
    inv = (~bits) & 0xFFFFFFFF
    if (inv + 1) & inv:
        return f"netmask không liên tục: {value}"
    return None


def _wildcard_ok(value: str) -> str | None:
    bad = _ip_ok(value)
    if bad:
        return bad
    bits = int(ipaddress.IPv4Address(value))
    if (bits + 1) & bits:
        return f"wildcard không liên tục: {value}"
    return None


def _vlan_list_ok(value: str) -> str | None:
    for part in value.split(","):
        lo, dash, hi = part.partition("-")
        if dash and not hi:
            return f"dải VLAN thiếu đầu cuối: {part}"
        for v in (lo, hi) if hi else (lo,):
            if not v.isdigit():
                return f"danh sách VLAN sai: {value}"
            bad = _vlan_ok(v)
            if bad:
                return bad
        if hi and int(lo) >= int(hi):
            return f"dải VLAN sai: {part}"
    return None


def _port_ok(proto: str, port: str | None) -> str | None:
    """Toán tử cổng chỉ hợp lệ với tcp/udp, và cổng phải trong 0–65535 (IOS từ chối `permit ip ... eq 80`)."""
    if port is None:
        return None
    if proto not in ("tcp", "udp"):
        return f"toán tử cổng chỉ dùng với tcp/udp, không dùng với {proto}"
    if not 0 <= int(port) <= 65535:
        return f"cổng ngoài 0–65535: {port}"
    return None


def _endpoint_ok(ep: str) -> str | None:
    if ep == "any":
        return None
    if ep.startswith("host "):
        return _ip_ok(ep[5:])
    addr, _, wild = ep.partition(" ")
    return _ip_ok(addr) or _wildcard_ok(wild)


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    enter: str | None = None  # mode mới sau lệnh này (None = giữ mode)
    check: Check | None = None


def _rule(pattern: str, enter: str | None = None, check: Check | None = None) -> Rule:
    return Rule(re.compile(rf"^{pattern}$"), enter, check)


_COMMON: tuple[Rule, ...] = (
    _rule(r"!.*"),
    _rule(r"exit", enter="global"),
)

# Bảng lệnh theo mode. Mỗi mode: lệnh cho phép và mode đích (nếu đổi).
RULES: dict[str, tuple[Rule, ...]] = {
    "global": (
        *_COMMON,
        _rule(r"end", enter="end"),
        _rule(r"vlan (?P<vid>\d+)", enter="vlan", check=lambda m: _vlan_ok(m["vid"])),
        _rule(rf"interface (?P<if>{_IFNAME})", enter="interface"),
        _rule(
            rf"(no )?ip route (?P<net>{_IP}) (?P<mask>{_IP}) (?P<nh>{_IP}|{_IFNAME})",
            check=lambda m: (
                _ip_ok(m["net"]) or _mask_ok(m["mask"]) or (_ip_ok(m["nh"]) if m["nh"][0].isdigit() else None)
            ),
        ),
        _rule(rf"ip access-list extended (?P<name>{_NAME})", enter="acl"),
        _rule(r"aaa new-model"),
        _rule(rf"aaa authentication login (?P<list>{_NAME}) (?P<methods>{_AUTH}( {_AUTH}){{0,3}})"),
        _rule(
            r"line vty (?P<a>\d{1,2}) (?P<b>\d{1,2})",
            enter="line",
            check=lambda m: None if int(m["a"]) <= int(m["b"]) else "line vty: đầu > cuối",
        ),
    ),
    "vlan": (
        *_COMMON,
        _rule(rf"name {_NAME}"),
        _rule(r"end", enter="end"),
        _rule(r"vlan (?P<vid>\d+)", enter="vlan", check=lambda m: _vlan_ok(m["vid"])),
        _rule(rf"interface (?P<if>{_IFNAME})", enter="interface"),
    ),
    "interface": (
        *_COMMON,
        _rule(r"description [^\x00-\x1f\x7f]{0,200}"),
        _rule(r"switchport mode (access|trunk)"),
        _rule(r"switchport access vlan (?P<vid>\d+)", check=lambda m: _vlan_ok(m["vid"])),
        _rule(r"switchport trunk encapsulation dot1q"),
        _rule(r"switchport trunk allowed vlan (?P<list>[0-9,-]+)", check=lambda m: _vlan_list_ok(m["list"])),
        _rule(r"switchport trunk native vlan (?P<vid>\d+)", check=lambda m: _vlan_ok(m["vid"])),
        _rule(
            rf"ip address (?P<ip>{_IP}) (?P<mask>{_IP})",
            check=lambda m: _ip_ok(m["ip"]) or _mask_ok(m["mask"]),
        ),
        _rule(r"(no )?shutdown"),
        _rule(rf"ip access-group (?P<name>{_NAME}) (in|out)"),
        _rule(r"end", enter="end"),
        _rule(rf"interface (?P<if>{_IFNAME})", enter="interface"),
        _rule(r"vlan (?P<vid>\d+)", enter="vlan", check=lambda m: _vlan_ok(m["vid"])),
    ),
    "acl": (
        *_COMMON,
        _rule(
            rf"(?P<seq>\d+ )?(permit|deny) (?P<proto>ip|tcp|udp|icmp) (?P<src>{_ACL_EP}) (?P<dst>{_ACL_EP})"
            r"( (eq|gt|lt|neq) (?P<port>\d{1,5}))?",
            check=lambda m: (
                _endpoint_ok(m["src"]) or _endpoint_ok(m["dst"]) or _port_ok(m["proto"], m["port"])
            ),
        ),
        _rule(r"remark [^\x00-\x1f\x7f]{0,100}"),
        _rule(r"end", enter="end"),
        _rule(rf"interface (?P<if>{_IFNAME})", enter="interface"),
        _rule(rf"ip access-list extended (?P<name>{_NAME})", enter="acl"),
    ),
    "line": (
        *_COMMON,
        _rule(rf"login authentication {_NAME}"),
        _rule(r"end", enter="end"),
    ),
}

# Lệnh chỉ có nghĩa trên switch (iosvl2). Trên router (ios/iosv) là lỗi cú pháp.
_SWITCH_ONLY = re.compile(r"^(vlan \d+|switchport .*)$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def check_syntax(device: str, text: str, platform: str | None = None) -> list[Finding]:
    """Kiểm tra một file cấu hình của một thiết bị. Trả về danh sách finding (rỗng = đạt)."""
    out: list[Finding] = []
    mode = "global"
    lines = text.split("\n")
    saw_end = False
    for no, raw in enumerate(lines, start=1):
        if _CONTROL.search(raw):
            out.append(_f("SYN_CONTROL_CHAR", device, no, "ký tự điều khiển trong cấu hình"))
            continue
        line = raw.strip()
        if not line:
            continue
        if saw_end:
            out.append(_f("SYN_AFTER_END", device, no, f"lệnh sau `end`: {line[:60]}"))
            continue
        if len(line) > 240:
            out.append(_f("SYN_LINE_TOO_LONG", device, no, "dòng dài hơn 240 ký tự"))
            continue
        if platform in ("ios", "iosv") and _SWITCH_ONLY.match(line):
            # Ghi lỗi nhưng vẫn chuyển mode như switch, để không báo dây chuyền các dòng sau.
            out.append(_f("SYN_PLATFORM", device, no, f"lệnh switch trên router {platform}: {line[:60]}"))
        matched = None
        for rule in RULES[mode]:
            m = rule.pattern.match(line)
            if m:
                matched = (rule, m)
                break
        if matched is None:
            known_elsewhere = any(r.pattern.match(line) for rs in RULES.values() for r in rs)
            code = "SYN_WRONG_MODE" if known_elsewhere else "SYN_UNKNOWN_COMMAND"
            out.append(_f(code, device, no, f"ở mode {mode}: {line[:60]}"))
            continue
        rule, m = matched
        if rule.check is not None:
            problem = rule.check(m)
            if problem:
                out.append(_f("SYN_BAD_VALUE", device, no, problem))
        if rule.enter == "end":
            saw_end = True
        elif rule.enter is not None:
            mode = rule.enter
    if not saw_end:
        out.append(_f("SYN_MISSING_END", device, None, "cấu hình không kết thúc bằng `end`"))
    return out


def _f(code: str, device: str, line_no: int | None, message: str) -> Finding:
    return Finding(sensor=SENSOR, code=code, severity="fail", device=device, line_no=line_no, message=message)
