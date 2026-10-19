"""Intent grounding tất định (D-061; hiện thực D-051, D-030).

Gate G5 chỉ so tool call của TSA với ChangeRequest do CBA nộp; ChangeRequest đó cũng do LLM sinh. CBA bịa một
giá trị (vd suy VLAN 438 từ tên GUEST_0438) thì qua mọi bước G1–G6. Module này đóng vòng tròn đó ở phía tin
cậy: MỌI giá trị thực thể trong ChangeRequest (hostname, interface, VLAN ID, IP/prefix, số dòng ACL, cổng,
tên VLAN/ACL/list, mô tả) phải NEO được vào intent gốc của người dùng, hoặc suy ra DUY NHẤT từ context. Không
neo được → vi phạm.

Quy tắc neo (đúng như D-061):
- Intent được chuẩn hoá NFC, casefold, tách token theo khoảng trắng và dấu câu; `/ . _ -` nằm GIỮA token thì
  giữ (`10.0.12.2`, `1/2`, `GUEST_0438`, `PC-07` là một token), ở hai đầu token thì bỏ (dấu chấm cuối câu).
- Số nguyên: chuỗi chữ số là cả một token, hoặc dính liền sau `vlan|seq|dòng|port|cổng` (`vlan20`). Chữ số
  NẰM TRONG một token khác (`GUEST_0438`, `GigabitEthernet1/2`, `SW2`) KHÔNG phải neo.
- Tên (VLAN, ACL, login list, key ref): bằng nguyên một token, không phân biệt hoa thường.
- Mô tả (free-text): dãy token của nó xuất hiện liên tiếp trong intent.
- IP: bằng nguyên một token, hoặc là phần IP của một token CIDR. Prefix/CIDR: bằng nguyên một token, hoặc địa
  chỉ mạng theo sau là netmask/wildcard. Endpoint ACL `host X` neo theo X; `any` không phải thực thể nên
  không kiểm.
- Interface: token (đã khai triển viết tắt Gi/Fa/Te/Lo/Vl/Po/Eth; `Gi 1/2` viết tách cũng được) bằng tên
  interface.
- Thiết bị: hostname là một token; HOẶC suy duy nhất từ context: context chỉ có một thiết bị, hoặc interface
  của thao tác đã neo được vào intent và đúng một thiết bị trong context có interface đó.
- `remove_static_route.next_hop`: suy duy nhất khi thiết bị có đúng một static route tới prefix đó, với
  next-hop đó.

Trường không có trong `GROUNDED_FIELDS` của tool (vd `enabled`, `direction`, `methods`, `action`, `protocol`)
mô tả LOẠI thao tác, không phải thực thể, nên không kiểm. Tool không có bảng → một vi phạm (fail-closed). Lỗi
khi kiểm một trường → vi phạm, không bao giờ âm thầm cho qua.

Giới hạn (nêu ở D-061): đây là phép kiểm ở mức thực thể, không phải ngữ nghĩa. Một giá trị có trong intent
nhưng ở vai trò khác (vd số trong mô tả được dùng làm VLAN ID) vẫn được coi là có neo; thao tác thừa mà mọi
giá trị đều có trong intent (vd đổi `enabled`) không bị bắt. Không giúp với tấn công trực tiếp (A1): kẻ tấn
công viết intent.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from harness_agent.harness.context_store import ContextStore
from harness_agent.models import ToolCall

# Trường được kiểm của từng tool, lấy đúng tên trường của args_model trong registry. Thêm tool vào registry
# mà quên bảng này thì `ground` báo vi phạm cho mọi thao tác của tool đó (test chốt: bảng phủ đủ registry).
GROUNDED_FIELDS: dict[str, tuple[str, ...]] = {
    "create_vlan": ("device", "vlan_id", "name"),
    "set_access_vlan": ("device", "interface", "vlan_id"),
    "set_trunk": ("device", "interface", "allowed_vlans", "native_vlan"),
    "set_interface_ip": ("device", "interface", "ip", "prefix_len"),
    "set_interface_state": ("device", "interface"),
    "set_interface_description": ("device", "interface", "text"),
    "add_static_route": ("device", "prefix", "next_hop", "exit_interface"),
    "remove_static_route": ("device", "prefix", "next_hop"),
    "add_acl_entry": ("device", "acl_name", "seq", "src", "dst", "dst_port"),
    "apply_acl": ("device", "interface", "acl_name"),
    "set_aaa_login": ("device", "list_name", "tacacs_key_ref"),
}

REASON_NO_TABLE = "tool chưa có bảng grounding"
REASON_UNANCHORED = "không neo được vào intent và không suy ra duy nhất từ context"


@dataclass(frozen=True)
class GroundingViolation:
    index: int  # vị trí thao tác trong ChangeRequest.changes (từ 0)
    field: str
    value: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ interface
_IF_NAMES: dict[str, str] = {
    "gigabitethernet": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "fastethernet": "FastEthernet",
    "fa": "FastEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "te": "TenGigabitEthernet",
    "loopback": "Loopback",
    "lo": "Loopback",
    "vlan": "Vlan",
    "vl": "Vlan",
    "port-channel": "Port-channel",
    "po": "Port-channel",
    "ethernet": "Ethernet",
    "eth": "Ethernet",
}
# Viết tách ("Gi 1/2") chỉ ghép với loại cổng vật lý/logic; KHÔNG ghép "VLAN 20" thành interface Vlan20, vì
# "VLAN 20" trong intent gần như luôn là VLAN ID.
_JOINABLE = frozenset(_IF_NAMES) - {"vlan", "vl"}
_IF_RE = re.compile(r"([a-z][a-z-]*?)(\d+(?:/\d+)*(?:\.\d+)?)")
_IF_NUM_RE = re.compile(r"\d+(?:/\d+)*(?:\.\d+)?")


def normalize_interface(name: str) -> str:
    """Tên interface đầy đủ, đúng hoa thường của IOS: "Gi0/1" → "GigabitEthernet0/1". Tên không nhận ra thì
    trả nguyên văn (so khớp sau đó sẽ không khớp, tức fail-closed)."""
    m = _IF_RE.fullmatch(name.strip().casefold())
    if m is None or m.group(1) not in _IF_NAMES:
        return name
    return _IF_NAMES[m.group(1)] + m.group(2)


# ------------------------------------------------------------------ intent
_TOKEN_RE = re.compile(r"[\w./-]+")
_EDGE = "./-"
_INT_KEYWORDS = tuple(unicodedata.normalize("NFC", k) for k in ("vlan", "seq", "dòng", "port", "cổng"))


def _tokens(text: str) -> list[str]:
    norm = unicodedata.normalize("NFC", text).casefold()
    return [t for t in (raw.strip(_EDGE) for raw in _TOKEN_RE.findall(norm)) if t]


@dataclass(frozen=True)
class _Intent:
    tokens: tuple[str, ...]
    words: frozenset[str]
    interfaces: frozenset[str]  # tên interface đầy đủ, casefold

    @classmethod
    def parse(cls, text: str) -> _Intent:
        toks = tuple(_tokens(text))
        itfs: set[str] = set()
        for i, tok in enumerate(toks):
            m = _IF_RE.fullmatch(tok)
            if m is not None and m.group(1) in _IF_NAMES:
                itfs.add((_IF_NAMES[m.group(1)] + m.group(2)).casefold())
            if tok in _JOINABLE and i + 1 < len(toks) and _IF_NUM_RE.fullmatch(toks[i + 1]):
                itfs.add((_IF_NAMES[tok] + toks[i + 1]).casefold())
        return cls(toks, frozenset(toks), frozenset(itfs))

    def pairs(self) -> zip[tuple[str, str]]:
        return zip(self.tokens, self.tokens[1:], strict=False)


# ------------------------------------------------------------------ neo theo loại giá trị
def _int_ok(n: Any, it: _Intent) -> bool:
    if not isinstance(n, int) or isinstance(n, bool):
        return False
    s = str(n)
    return s in it.words or any(kw + s in it.words for kw in _INT_KEYWORDS)


def _name_ok(value: Any, it: _Intent) -> bool:
    return isinstance(value, str) and unicodedata.normalize("NFC", value).casefold() in it.words


def _text_ok(value: Any, it: _Intent) -> bool:
    if not isinstance(value, str):
        return False
    seq = tuple(_tokens(value))
    if not seq:
        return True  # mô tả rỗng: không có thực thể nào để bịa
    n = len(seq)
    return any(it.tokens[i : i + n] == seq for i in range(len(it.tokens) - n + 1))


def _ip_ok(value: Any, it: _Intent) -> bool:
    if not isinstance(value, str):
        return False
    ip = str(ipaddress.IPv4Address(value))
    return ip in it.words or any(t.startswith(ip + "/") for t in it.tokens)


def _cidr_ok(value: Any, it: _Intent) -> bool:
    if not isinstance(value, str):
        return False
    net = ipaddress.IPv4Network(value, strict=False)
    if value.casefold() in it.words or str(net) in it.words:
        return True
    addr, masks = str(net.network_address), {str(net.netmask), str(net.hostmask)}
    return any(a == addr and b in masks for a, b in it.pairs())


def _endpoint_ok(value: Any, it: _Intent) -> bool:
    if not isinstance(value, str):
        return False
    if value == "any":
        return True  # không phải thực thể
    if value.startswith("host "):
        return _ip_ok(value.removeprefix("host "), it)
    return _cidr_ok(value, it)


def _port_ok(value: Any, it: _Intent) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split()
    return len(parts) == 2 and parts[1].isdigit() and _int_ok(int(parts[1]), it)


def _interface_ok(value: Any, it: _Intent) -> bool:
    return isinstance(value, str) and normalize_interface(value).casefold() in it.interfaces


@dataclass(frozen=True)
class _Check:
    tc: ToolCall
    it: _Intent
    store: ContextStore


def _device_ok(value: Any, c: _Check) -> bool:
    if not isinstance(value, str):
        return False
    if value.casefold() in c.it.words:
        return True
    hosts = c.store.hostnames
    if hosts == [value]:
        return True  # context chỉ có một thiết bị
    for key in ("interface", "exit_interface"):
        itf = c.tc.args.get(key)
        if isinstance(itf, str) and _interface_ok(itf, c.it):
            owners = [h for h in hosts if c.store.interface(h, normalize_interface(itf)) is not None]
            if owners == [value]:
                return True
    return False


def _prefix_len_ok(value: Any, c: _Check) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    ip = c.tc.args.get("ip")
    if isinstance(ip, str) and f"{ip}/{value}" in c.it.words:
        return True
    mask = str(ipaddress.IPv4Network(f"0.0.0.0/{value}").netmask)
    return mask in c.it.words or _int_ok(value, c.it)


def _next_hop_ok(value: Any, c: _Check) -> bool:
    if _ip_ok(value, c.it):
        return True
    if c.tc.tool != "remove_static_route":
        return False
    # Suy duy nhất: thiết bị có đúng một static route tới prefix này.
    dev = c.store.device(c.tc.args.get("device", "")) or {}
    prefix = c.tc.args.get("prefix")
    routes = [r for r in dev.get("static_routes") or [] if isinstance(r, dict) and r.get("prefix") == prefix]
    return len(routes) == 1 and routes[0].get("next_hop") == value


def _simple(fn: Callable[[Any, _Intent], bool]) -> Callable[[Any, _Check], bool]:
    return lambda value, c: fn(value, c.it)


_FIELD_CHECKS: dict[str, Callable[[Any, _Check], bool]] = {
    "device": _device_ok,
    "interface": _simple(_interface_ok),
    "exit_interface": _simple(_interface_ok),
    "vlan_id": _simple(_int_ok),
    "native_vlan": _simple(_int_ok),
    "seq": _simple(_int_ok),
    "prefix_len": _prefix_len_ok,
    "name": _simple(_name_ok),
    "acl_name": _simple(_name_ok),
    "list_name": _simple(_name_ok),
    "tacacs_key_ref": _simple(_name_ok),
    "text": _simple(_text_ok),
    "ip": _simple(_ip_ok),
    "next_hop": _next_hop_ok,
    "prefix": _simple(_cidr_ok),
    "src": _simple(_endpoint_ok),
    "dst": _simple(_endpoint_ok),
    "dst_port": _simple(_port_ok),
}
# Trường kiểu danh sách: kiểm từng phần tử bằng phép kiểm của phần tử.
_LIST_FIELDS: dict[str, Callable[[Any, _Check], bool]] = {"allowed_vlans": _simple(_int_ok)}


def _short(value: Any) -> str:
    # Giữ NGUYÊN giá trị: người gọi che secret rồi mới cắt ngắn (cắt trước thì một secret bị cắt đôi không còn
    # khớp để che).
    return str(value)


def _field_violations(index: int, name: str, c: _Check) -> list[GroundingViolation]:
    if name not in c.tc.args:
        return []  # trường tuỳ chọn không có mặt: không có giá trị nào để bịa
    value = c.tc.args[name]
    if name in _LIST_FIELDS:
        check = _LIST_FIELDS[name]
        if not isinstance(value, list):
            return [GroundingViolation(index, name, _short(value), "giá trị không phải danh sách")]
        return [
            GroundingViolation(index, name, _short(item), REASON_UNANCHORED)
            for item in value
            if not check(item, c)
        ]
    check = _FIELD_CHECKS.get(name)
    if check is None:
        return [GroundingViolation(index, name, _short(value), "trường chưa có phép kiểm grounding")]
    return [] if check(value, c) else [GroundingViolation(index, name, _short(value), REASON_UNANCHORED)]


def ground(intent: str, changes: list[ToolCall], store: ContextStore) -> list[GroundingViolation]:
    """Mọi vi phạm grounding của `changes` so với `intent` gốc và `store` (context của runner). Rỗng = mọi
    giá trị thực thể đều có neo. Không bao giờ raise vì dữ liệu: lỗi khi kiểm một trường thành vi phạm
    (fail-closed)."""
    it = _Intent.parse(intent if isinstance(intent, str) else "")
    out: list[GroundingViolation] = []
    for index, tc in enumerate(changes):
        names = GROUNDED_FIELDS.get(tc.tool)
        if names is None:
            out.append(GroundingViolation(index, "tool", _short(tc.tool), REASON_NO_TABLE))
            continue
        c = _Check(tc, it, store)
        for name in names:
            try:
                out.extend(_field_violations(index, name, c))
            except Exception as exc:  # noqa: BLE001 — fail-closed: không kiểm được là không neo được
                value = tc.args.get(name) if isinstance(tc.args, dict) else None
                reason = f"lỗi khi kiểm: {type(exc).__name__}"
                out.append(GroundingViolation(index, name, _short(value), reason))
    return out
