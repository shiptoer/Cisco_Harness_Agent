"""Benchmark v1 (Task 6b, D-065): bộ dataset chuẩn chấm ĐỘ CHÍNH XÁC đầu ra, không dùng LLM để gán nhãn.

Khác corpus v2 (đo an toàn: corrupted, adversarial, ambiguous), benchmark chỉ gồm yêu cầu HỢP LỆ và đo hệ
thống dịch intent thành cấu hình đúng tới đâu, ở hai mức:
- mức thao tác: tool call kỳ vọng (`expected_tool_calls`), so với thao tác evaluator phân tích ngược từ
  package;
- mức CLI: gold `gold/<case_id>/<device>.cfg` = `render_by_device(expected_tool_calls)` bằng CHÍNH renderer
  của hệ thống (byte LF, UTF-8), sha256 ghi trong manifest.

Cách sinh (tất định: cùng seed, cùng context → cùng từng byte):
- Mỗi tool trong 11 tool có 3 template intent (VI + EN, có slot). Slot (thiết bị, cổng, VLAN, tên, prefix...)
  chọn từ context bằng `random.Random(f"{seed}-{context_id}")`, sao cho thao tác hợp lệ với policy của context
  (gate ở role security_admin cho qua, giá trị neo được vào intent theo D-061). Case thứ k của một tool trên
  một context dùng template k mod 3, template dev xếp trước (`_specs`); trùng thao tác thì chọn lại.
- 5 template đa thao tác (2–3 thao tác tương thích trên cùng thiết bị, vd create_vlan + set_access_vlan), mỗi
  template `MULTI_PER_TEMPLATE` case trên mỗi context.
- Ngôn ngữ của `intent` (VI hay EN) chọn theo seed; manifest giữ cả hai bản (`intent_vi`, `intent_en`).
  `TestCase.intent_template` là bản EN.

Split dev/test: family = (template, context). Split chia theo TEMPLATE (nên mỗi family nằm trọn một split và
không template nào ở cả hai): phân tầng theo tool, mỗi tầng xáo bằng `random.Random(f"{seed}-split")` rồi lấy
`max(1, round(30% số template))` template cho test (single-op: 1/3 template mỗi tool; đa thao tác: 2/5).

Layout giống corpus (`write_corpus`) để `scripts/run_corpus.py --corpus data/benchmark/v1` chạy không cần
sửa: `<case_id>.json`, `scenarios/<case_id>.json` (oracle scenario cho `fake_dsh`: model nghe lời hoàn toàn,
chạy khô không cần API key, KHÔNG phải số đo model), `contexts/<sha256>.json`, `manifest.json` (cùng khoá
`cases.*.file`, `cases.*.fake_scenario`, thêm `split`, `gold`, `family`, `template`...). `split` và `gold`
nằm trong manifest, không trong `TestCase` (contract đã khoá).

Context smb-01 là bản dẫn xuất `data/contexts/smb-01-collected-ref-l2.json` (`derive_l2_context`):
snapshot SSH `smb-01-collected-ref.json` (giữ nguyên byte, D-047) với `platform` của năm switch IOL L2 sửa
thành `iosvl2`.
Snapshot gốc mang `ios` cho cả switch do inventory khai sai trước 17/10/2026 (D-055): sensor syntax coi `ios`
là router và chặn mọi lệnh `vlan`/`switchport` ở B4, nên dùng bản gốc thì 16 case L2 hợp lệ bị full harness từ
chối. Thao tác L2 chỉ chọn trên thiết bị `iosvl2`; sinh ra thao tác L2 trên thiết bị khác là lỗi.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import random
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.eval.benchmark_io import load_benchmark  # noqa: F401 — giữ chỗ gọi cũ
from harness_agent.harness.context_store import ContextError, ContextStore
from harness_agent.harness.gate import evaluate as gate_evaluate
from harness_agent.harness.grounding import ground
from harness_agent.models import (
    ConfigType,
    ContextRef,
    NetworkContext,
    Role,
    Subset,
    TestCase,
    ToolCall,
    Verdict,
    parse_tool_call,
)
from harness_agent.models.invariants import check_invariants
from harness_agent.tools.registry.ip_utils import prefix_to_netmask
from harness_agent.tools.registry.render import render_by_device

from .generator import Draft, _Ctx, _oracle_scenario, _tc, canonical_context_bytes

BENCHMARK_VERSION = "bm-1.0"
DEFAULT_SEED = 42
DEFAULT_PER_TOOL = 4
DEFAULT_CONTEXTS = (
    "data/contexts/campus-small-01.json",
    "data/contexts/smb-01-collected-ref-l2.json",
)
# Snapshot SSH gốc (D-047, không sửa) và bản dẫn xuất có platform đúng cho switch IOL L2 (D-065).
SMB01_REF = "data/contexts/smb-01-collected-ref.json"
SMB01_L2 = "data/contexts/smb-01-collected-ref-l2.json"
SMB01_IOL_L2 = ("CORE1", "ACC1", "ACC2", "ACC3", "ACC-BR1")
MULTI_PER_TEMPLATE = 2
TEST_FRACTION = 0.3
ROLE = Role.SECURITY_ADMIN
# Tool sinh lệnh chỉ có trên switch (vlan/switchport): chỉ được nhắm vào thiết bị iosvl2.
L2_TOOLS = frozenset({"create_vlan", "set_access_vlan", "set_trunk"})

Manifest = dict[str, Any]
Slots = dict[str, Any]
Calls = list[dict[str, Any]]
Picker = Callable[[random.Random, "_BenchCtx"], "tuple[Slots, Calls] | None"]

# Thứ tự cố định của 11 tool (test đối chiếu với TOOL_REGISTRY): quyết định thứ tự case và case_id.
TOOL_ORDER: tuple[str, ...] = (
    "create_vlan",
    "set_access_vlan",
    "set_trunk",
    "set_interface_ip",
    "set_interface_state",
    "set_interface_description",
    "add_static_route",
    "remove_static_route",
    "add_acl_entry",
    "apply_acl",
    "set_aaa_login",
)
CONFIG_TYPE: dict[str, ConfigType] = {
    "create_vlan": ConfigType.VLAN,
    "set_access_vlan": ConfigType.INTERFACE,
    "set_trunk": ConfigType.INTERFACE,
    "set_interface_ip": ConfigType.INTERFACE,
    "set_interface_state": ConfigType.INTERFACE,
    "set_interface_description": ConfigType.INTERFACE,
    "add_static_route": ConfigType.STATIC_ROUTE,
    "remove_static_route": ConfigType.STATIC_ROUTE,
    "add_acl_entry": ConfigType.ACL,
    "apply_acl": ConfigType.ACL,
    "set_aaa_login": ConfigType.AAA,
}


class BenchmarkError(ValueError):
    """Không sinh được benchmark hợp lệ từ context đã cho (thiếu loại thiết bị/giá trị mà một tool cần, hoặc
    nhãn không qua gate/grounding). Script bắt lỗi này và thoát mã 2, không in traceback."""


def _pick(rng: random.Random, pool: Sequence[Any], what: str) -> Any:
    """`rng.choice(pool)`, nhưng pool rỗng → BenchmarkError nêu tên pool (context thứ ba thiếu thứ tool cần).
    Tiêu thụ RNG y như `rng.choice`, nên dữ liệu sinh ra không đổi."""
    if not pool:
        raise BenchmarkError(f"context không có {what}")
    return rng.choice(pool)


# ----------------------------------------------------------------------------- truy vấn context


class _BenchCtx(_Ctx):
    """`_Ctx` của corpus (switch = platform `iosvl2`) cộng các truy vấn riêng của benchmark."""

    def parking(self) -> int | None:
        return self.raw.get("policy", {}).get("parking_vlan")

    def data_vlans(self, host: str) -> list[int]:
        """VLAN dữ liệu gán được cho cổng access: không reserved, không parking, không VLAN quản trị."""
        mgmt = {v["vlan_id"] for v in self.devices[host].get("vlans", []) if "MGMT" in v["name"].upper()}
        return sorted(self.vlan_ids(host) - self.reserved() - {self.parking()} - mgmt)

    def free_ports(self, host: str) -> list[dict[str, Any]]:
        """Cổng không bảo vệ, chưa có IP (đặt IP / bật được) — trên router."""
        return [i for i in self.ports(host) if i.get("ipv4") is None and i["mode"] in ("unused", "routed")]

    def acl_targets(self, host: str) -> list[dict[str, Any]]:
        """Interface áp ACL được: routed (SVI hoặc cổng router), không bảo vệ, không Loopback, không SVI của
        VLAN reserved. Cổng switchport (PACL) không dùng: hỗ trợ khác nhau giữa các image."""
        prot = self.protected(host)
        router = self.devices[host]["platform"] != "iosvl2"  # cùng cách phân loại với `_Ctx.routers()`
        out = []
        for i in self.devices[host]["interfaces"]:
            name = i["name"]
            if name in prot or name.startswith("Loopback"):
                continue
            if name.startswith("Vlan") and int(name[4:]) in self.reserved():
                continue
            if i["mode"] == "routed" or (router and i["mode"] == "unused"):
                out.append(i)
        return out


# ----------------------------------------------------------------------------- giá trị dùng chung

_VLAN_NAMES = ("GUEST2", "IOT", "PRINTERS", "VOICE", "CAMERA", "LAB", "DEV", "QA", "STAFF2", "WIFI", "BACKUP")
_DESCRIPTIONS = (
    "HR-PC-07",
    "Printer floor 2",
    "Meeting room B",
    "AP-03 (ceiling)",
    "Lab PC, temporary",
    "Camera lobby",
    "VoIP phone 12",
    "Spare port, rack 4",
)
_NEW_ACLS = ("WEB-IN", "VOIP-FILTER", "PRINT-ACL", "LAB-IN", "CAM-OUT")
_LIST_NAMES = ("VTY-AUTH", "NOC-LOGIN", "ADMIN-LOGIN", "SSH-LOGIN", "MGMT-LOGIN")
# (methods, vi, en): tacacs+ trong methods thì cần key ref (args_model của set_aaa_login).
_METHODS = (
    (["group tacacs+", "local"], "TACACS+ rồi local", "TACACS+ then local"),
    (
        ["group tacacs+", "group radius", "local"],
        "TACACS+, RADIUS rồi local",
        "TACACS+, then RADIUS, then local",
    ),
    (["group radius", "local"], "RADIUS rồi local", "RADIUS then local"),
    (["local"], "chỉ local", "local only"),
)
_PORTS = {"tcp": (22, 443, 3389, 8080), "udp": (53, 123, 514)}


def _free_vlan(rng: random.Random, ctx: _BenchCtx, host: str) -> int:
    used = ctx.vlan_ids(host) | ctx.reserved()
    while True:
        vid = rng.randint(2, 1001)
        if vid not in used:
            return vid


def _new_prefix(rng: random.Random, ctx: _BenchCtx, host: str) -> str:
    """Prefix mới cho static route: không trùng route có sẵn của thiết bị, không chồng mạng quản trị."""
    taken = {r["prefix"] for r in ctx.devices[host].get("static_routes") or []}
    mgmt = ctx.raw.get("policy", {}).get("mgmt_prefix")
    while True:
        if rng.random() < 0.5:
            prefix = f"10.{rng.randint(100, 199)}.0.0/16"
        else:
            prefix = f"172.{rng.randint(16, 31)}.{rng.randint(0, 255)}.0/24"
        net = ipaddress.IPv4Network(prefix)
        if prefix not in taken and not (mgmt and net.overlaps(ipaddress.IPv4Network(mgmt))):
            return prefix


def _acl_entry(rng: random.Random, device: str, acl: str, seq: int) -> tuple[Slots, dict[str, Any]]:
    action = rng.choice(("permit", "deny"))
    proto = rng.choice(("tcp", "udp"))
    port = rng.choice(_PORTS[proto])
    src = f"10.{rng.randint(10, 90)}.{rng.choice((0, 10, 20, 30))}.0/24"
    dst = f"10.10.50.{rng.randint(20, 60)}"
    slots = {
        "acl": acl,
        "dev": device,
        "seq": seq,
        "action": action,
        "action_vi": "cho phép" if action == "permit" else "chặn",
        "action_en": "permit" if action == "permit" else "deny",
        "proto": proto,
        "src": src,
        "dst": dst,
        "port": port,
    }
    call = _tc(
        "add_acl_entry",
        device=device,
        acl_name=acl,
        seq=seq,
        action=action,
        protocol=proto,
        src=src,
        dst=f"host {dst}",
        dst_port=f"eq {port}",
    )
    return slots, call


def _direction(rng: random.Random, itf: dict[str, Any]) -> str | None:
    free = [d for d in ("in", "out") if itf.get(f"acl_{d}") is None]
    return rng.choice(free) if free else None


def _dir_slots(direction: str) -> Slots:
    return {
        "dir": direction,
        "dir_vi": "chiều vào (in)" if direction == "in" else "chiều ra (out)",
        "dir_en": "inbound" if direction == "in" else "outbound",
    }


# ----------------------------------------------------------------------------- picker một thao tác


def pick_create_vlan(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, ctx.switches(), "switch (platform iosvl2)")
    vid = _free_vlan(rng, ctx, dev)
    name = _pick(rng, [n for n in _VLAN_NAMES if n not in ctx.vlan_names(dev)], f"tên VLAN mới cho {dev}")
    return {"dev": dev, "vid": vid, "name": name}, [_tc("create_vlan", device=dev, vlan_id=vid, name=name)]


def pick_set_access_vlan(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, ctx.switches(), "switch (platform iosvl2)")
    ports = ctx.ports(dev, "access") + ctx.ports(dev, "unused")
    if not ports:
        return None
    port = rng.choice(ports)
    pool = [v for v in ctx.data_vlans(dev) if v != port.get("access_vlan")]
    if not pool:
        return None
    vid = rng.choice(pool)
    slots = {"dev": dev, "itf": port["name"], "vid": vid}
    return slots, [_tc("set_access_vlan", device=dev, interface=port["name"], vlan_id=vid)]


def pick_set_trunk(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, ctx.switches(), "switch (platform iosvl2)")
    unused = ctx.ports(dev, "unused")
    pool = sorted(ctx.vlan_ids(dev) - ctx.reserved() - {ctx.parking()})
    # Trunk mang ít nhất hai VLAN; một VLAN duy nhất kiêm native thì cổng thực chất là access.
    if not unused or len(pool) < 2:
        return None
    port = rng.choice(unused)["name"]
    allowed = sorted(rng.sample(pool, k=min(len(pool), rng.randint(2, 3))))
    parking = ctx.parking()
    if parking in ctx.vlan_ids(dev) and parking not in ctx.reserved():
        native = parking
    else:
        # parking là reserved (smb-01): native là VLAN ngoài allowed nếu còn, không thì một VLAN của allowed.
        native = rng.choice([v for v in pool if v not in allowed] or allowed)
    slots = {"dev": dev, "itf": port, "vlist": ", ".join(map(str, allowed)), "native": native}
    return slots, [_tc("set_trunk", device=dev, interface=port, allowed_vlans=allowed, native_vlan=native)]


def _ip_slots(rng: random.Random) -> Slots:
    plen = rng.choice((24, 29, 30))
    ip = f"10.{rng.randint(100, 199)}.{rng.randint(0, 15)}.1"
    return {"ip": ip, "plen": plen, "mask": prefix_to_netmask(plen), "cidr": f"{ip}/{plen}"}


def pick_set_interface_ip(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    hosts = [h for h in ctx.routers() if ctx.free_ports(h)]
    dev = _pick(rng, hosts, "router có cổng không bảo vệ, chưa có IP")
    port = rng.choice(ctx.free_ports(dev))["name"]
    s = _ip_slots(rng) | {"dev": dev, "itf": port}
    return s, [_tc("set_interface_ip", device=dev, interface=port, ip=s["ip"], prefix_len=s["plen"])]


_STATE_WORDS = {
    True: {"verb_vi": "Bật", "verb_en": "Enable", "act_vi": "bật (no shutdown)", "act_en": "bring up"},
    False: {
        "verb_vi": "Tắt",
        "verb_en": "Shut down",
        "act_vi": "tắt (shutdown)",
        "act_en": "administratively shut",
    },
}


def pick_set_interface_state(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    hosts = [h for h in ctx.devices if ctx.ports(h)]
    dev = _pick(rng, hosts, "thiết bị có cổng không bảo vệ")
    port = rng.choice(ctx.ports(dev))
    enabled = not port.get("enabled", True)
    slots = {"dev": dev, "itf": port["name"], **_STATE_WORDS[enabled]}
    return slots, [_tc("set_interface_state", device=dev, interface=port["name"], enabled=enabled)]


def pick_set_interface_description(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, [h for h in ctx.devices if ctx.ports(h)], "thiết bị có cổng không bảo vệ")
    port = rng.choice(ctx.ports(dev))["name"]
    text = rng.choice(_DESCRIPTIONS)
    slots = {"dev": dev, "itf": port, "text": text}
    return slots, [_tc("set_interface_description", device=dev, interface=port, text=text)]


def pick_add_static_route(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    hosts = [h for h in ctx.devices if ctx.devices[h].get("static_routes")]
    dev = _pick(rng, hosts, "thiết bị có static route (để lấy next-hop)")
    nh = rng.choice(sorted({r["next_hop"] for r in ctx.devices[dev]["static_routes"]}))
    prefix = _new_prefix(rng, ctx, dev)
    net = ipaddress.IPv4Network(prefix)
    slots = {
        "dev": dev,
        "prefix": prefix,
        "nh": nh,
        "net": str(net.network_address),
        "mask": str(net.netmask),
    }
    return slots, [_tc("add_static_route", device=dev, prefix=prefix, next_hop=nh)]


def pick_remove_static_route(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    routes = [(h, r) for h in ctx.devices for r in ctx.devices[h].get("static_routes") or []]
    dev, r = _pick(rng, routes, "static route nào")
    slots = {"dev": dev, "prefix": r["prefix"], "nh": r["next_hop"]}
    return slots, [_tc("remove_static_route", device=dev, prefix=r["prefix"], next_hop=r["next_hop"])]


def pick_add_acl_entry(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, [h for h in ctx.devices if ctx.acls(h)], "thiết bị có ACL")
    acl = rng.choice(sorted(ctx.acls(dev)))
    seqs = {e["seq"] for e in ctx.acls(dev)[acl]}
    seq = rng.choice([s for s in range(50, 1000, 10) if s not in seqs])
    slots, call = _acl_entry(rng, dev, acl, seq)
    return slots, [call]


def pick_apply_acl(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    # ACL của line vty (tên chứa VTY) không áp lên interface.
    cands = [
        (h, acl, i)
        for h in ctx.devices
        for acl in sorted(ctx.acls(h))
        if "VTY" not in acl.upper()
        for i in ctx.acl_targets(h)
    ]
    dev, acl, itf = _pick(rng, cands, "interface routed áp được ACL có sẵn (không phải ACL VTY)")
    direction = _direction(rng, itf)
    if direction is None:
        return None
    slots = {"dev": dev, "itf": itf["name"], "acl": acl, **_dir_slots(direction)}
    return slots, [_tc("apply_acl", device=dev, interface=itf["name"], acl_name=acl, direction=direction)]


def pick_set_aaa_login(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, [h for h in ctx.devices if ctx.key_refs(h)], "thiết bị có tacacs key_ref")
    list_name = rng.choice(_LIST_NAMES)
    methods, m_vi, m_en = rng.choice(_METHODS)
    args: dict[str, Any] = {"device": dev, "list_name": list_name, "methods": list(methods)}
    key_vi = key_en = ""
    if "group tacacs+" in methods:
        key = ctx.key_refs(dev)[0]
        args["tacacs_key_ref"] = key
        key_vi, key_en = f", key tham chiếu {key}", f", key reference {key}"
    slots = {"dev": dev, "list": list_name, "m_vi": m_vi, "m_en": m_en, "key_vi": key_vi, "key_en": key_en}
    return slots, [_tc("set_aaa_login", **args)]


# ----------------------------------------------------------------------------- picker đa thao tác


def pick_multi_vlan_access(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    dev = _pick(rng, ctx.switches(), "switch (platform iosvl2)")
    ports = ctx.ports(dev, "access") + ctx.ports(dev, "unused")
    if not ports:
        return None
    port = rng.choice(ports)["name"]
    vid = _free_vlan(rng, ctx, dev)
    name = _pick(rng, [n for n in _VLAN_NAMES if n not in ctx.vlan_names(dev)], f"tên VLAN mới cho {dev}")
    slots = {"dev": dev, "itf": port, "vid": vid, "name": name}
    calls = [
        _tc("create_vlan", device=dev, vlan_id=vid, name=name),
        _tc("set_access_vlan", device=dev, interface=port, vlan_id=vid),
    ]
    return slots, calls


def pick_multi_vlan_access_desc(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    got = pick_multi_vlan_access(rng, ctx)
    if got is None:
        return None
    slots, calls = got
    text = rng.choice(_DESCRIPTIONS)
    calls.append(_tc("set_interface_description", device=slots["dev"], interface=slots["itf"], text=text))
    return slots | {"text": text}, calls


def pick_multi_acl_new_apply(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    cands = [(h, i) for h in ctx.devices for i in ctx.acl_targets(h)]
    dev, itf = _pick(rng, cands, "interface routed áp được ACL")
    direction = _direction(rng, itf)
    names = [n for n in _NEW_ACLS if n not in ctx.acls(dev)]
    if direction is None or not names:
        return None
    acl = rng.choice(names)
    slots, entry = _acl_entry(rng, dev, acl, 10)
    slots |= {"itf": itf["name"], **_dir_slots(direction)}
    apply = _tc("apply_acl", device=dev, interface=itf["name"], acl_name=acl, direction=direction)
    return slots, [entry, apply]


def pick_multi_ip_enable(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    cands = [(h, i) for h in ctx.routers() for i in ctx.free_ports(h) if i.get("enabled") is False]
    dev, itf = _pick(rng, cands, "cổng router trống đang tắt")
    s = _ip_slots(rng) | {"dev": dev, "itf": itf["name"]}
    calls = [
        _tc("set_interface_ip", device=dev, interface=itf["name"], ip=s["ip"], prefix_len=s["plen"]),
        _tc("set_interface_state", device=dev, interface=itf["name"], enabled=True),
    ]
    return s, calls


def pick_multi_route_replace(rng: random.Random, ctx: _BenchCtx) -> tuple[Slots, Calls] | None:
    routes = [
        (h, r)
        for h in ctx.devices
        for r in ctx.devices[h].get("static_routes") or []
        if r["prefix"] != "0.0.0.0/0"
    ]
    dev, old = _pick(rng, routes, "static route khác route mặc định")
    prefix = _new_prefix(rng, ctx, dev)
    slots = {"dev": dev, "old": old["prefix"], "nh": old["next_hop"], "prefix": prefix}
    calls = [
        _tc("remove_static_route", device=dev, prefix=old["prefix"], next_hop=old["next_hop"]),
        _tc("add_static_route", device=dev, prefix=prefix, next_hop=old["next_hop"]),
    ]
    return slots, calls


# ----------------------------------------------------------------------------- template


@dataclass(frozen=True)
class Template:
    template_id: str
    vi: str
    en: str


def _t(tid: str, vi: str, en: str) -> Template:
    return Template(tid, vi, en)


SINGLE_TEMPLATES: dict[str, tuple[Template, ...]] = {
    "create_vlan": (
        _t(
            "create_vlan.t1",
            "Tạo VLAN {vid} tên {name} trên {dev}",
            "Create VLAN {vid} named {name} on {dev}",
        ),
        _t(
            "create_vlan.t2",
            "Trên switch {dev}, thêm VLAN {vid} với tên {name}",
            "On switch {dev}, add VLAN {vid} with the name {name}",
        ),
        _t(
            "create_vlan.t3",
            "Khai báo VLAN mới {vid} ({name}) cho {dev}",
            "Define a new VLAN {vid} ({name}) on {dev}",
        ),
    ),
    "set_access_vlan": (
        _t(
            "set_access_vlan.t1",
            "Gán cổng {itf} của {dev} vào VLAN {vid}",
            "Assign interface {itf} on {dev} to VLAN {vid}",
        ),
        _t(
            "set_access_vlan.t2",
            "Chuyển {dev} {itf} sang chế độ access, VLAN {vid}",
            "Put {dev} {itf} in access mode on VLAN {vid}",
        ),
        _t(
            "set_access_vlan.t3",
            "Trên {dev}, cổng {itf} phải thuộc VLAN {vid}",
            "On {dev}, port {itf} must be an access port in VLAN {vid}",
        ),
    ),
    "set_trunk": (
        _t(
            "set_trunk.t1",
            "Cấu hình {dev} {itf} làm trunk cho VLAN {vlist}, native VLAN {native}",
            "Configure {dev} {itf} as a trunk allowing VLANs {vlist} with native VLAN {native}",
        ),
        _t(
            "set_trunk.t2",
            "Biến cổng {itf} trên {dev} thành trunk 802.1Q, chỉ cho phép VLAN {vlist}, native {native}",
            "Make port {itf} on {dev} an 802.1Q trunk that only allows VLANs {vlist}, native {native}",
        ),
        _t(
            "set_trunk.t3",
            "Trên {dev}, mở trunk ở {itf} mang VLAN {vlist} (native VLAN {native})",
            "On {dev}, enable trunking on {itf} carrying VLANs {vlist} (native VLAN {native})",
        ),
    ),
    "set_interface_ip": (
        _t("set_interface_ip.t1", "Đặt IP {cidr} cho {dev} {itf}", "Assign IP {cidr} to {dev} {itf}"),
        _t(
            "set_interface_ip.t2",
            "Cấu hình địa chỉ {ip} mask {mask} trên cổng {itf} của {dev}",
            "Configure address {ip} mask {mask} on interface {itf} of {dev}",
        ),
        _t(
            "set_interface_ip.t3",
            "Trên router {dev}, interface {itf} dùng địa chỉ {cidr}",
            "On router {dev}, interface {itf} should use address {cidr}",
        ),
    ),
    "set_interface_state": (
        _t("set_interface_state.t1", "{verb_vi} cổng {itf} trên {dev}", "{verb_en} interface {itf} on {dev}"),
        _t(
            "set_interface_state.t2",
            "Trên {dev}, {act_vi} interface {itf}",
            "On {dev}, {act_en} interface {itf}",
        ),
        _t(
            "set_interface_state.t3",
            "Yêu cầu: {verb_vi} {dev} {itf}",
            "Request: {verb_en} {dev} {itf}",
        ),
    ),
    "set_interface_description": (
        _t(
            "set_interface_description.t1",
            "Đặt description cho {dev} {itf} là '{text}'",
            "Set the description of {dev} {itf} to '{text}'",
        ),
        _t(
            "set_interface_description.t2",
            "Ghi chú cổng {itf} trên {dev}: '{text}'",
            "Label port {itf} on {dev} as '{text}'",
        ),
        _t(
            "set_interface_description.t3",
            "Trên {dev}, mô tả của interface {itf} cần là '{text}'",
            "On {dev}, the description of interface {itf} should read '{text}'",
        ),
    ),
    "add_static_route": (
        _t(
            "add_static_route.t1",
            "Thêm static route trên {dev} tới {prefix} qua {nh}",
            "Add a static route on {dev} to {prefix} via {nh}",
        ),
        _t(
            "add_static_route.t2",
            "Trên {dev}, định tuyến mạng {net} {mask} qua next-hop {nh}",
            "On {dev}, route network {net} {mask} through next hop {nh}",
        ),
        _t(
            "add_static_route.t3",
            "{dev} cần route tĩnh đến {prefix}, next-hop {nh}",
            "{dev} needs a static route to {prefix} with next hop {nh}",
        ),
    ),
    "remove_static_route": (
        _t(
            "remove_static_route.t1",
            "Xoá static route {prefix} qua {nh} trên {dev}",
            "Remove the static route {prefix} via {nh} on {dev}",
        ),
        _t(
            "remove_static_route.t2",
            "Trên {dev}, gỡ route tĩnh tới {prefix} (next-hop {nh})",
            "On {dev}, delete the static route to {prefix} (next hop {nh})",
        ),
        _t(
            "remove_static_route.t3",
            "Route {prefix} qua {nh} trên {dev} không còn dùng, hãy xoá",
            "The route {prefix} via {nh} on {dev} is no longer used, remove it",
        ),
    ),
    "add_acl_entry": (
        _t(
            "add_acl_entry.t1",
            "Thêm vào ACL {acl} trên {dev} dòng {seq}: {action} {proto} từ {src} tới host {dst} cổng {port}",
            "Add line {seq} to ACL {acl} on {dev}: {action} {proto} from {src} to host {dst} port {port}",
        ),
        _t(
            "add_acl_entry.t2",
            "Trên {dev}, ACL {acl} cần thêm dòng {seq} {action_vi} {proto} từ {src} "
            "đến host {dst} port {port}",
            "On {dev}, ACL {acl} needs line {seq} to {action_en} {proto} from {src} "
            "to host {dst} port {port}",
        ),
        _t(
            "add_acl_entry.t3",
            "ACL {acl} của {dev}: dòng {seq} {action_vi} {proto} {src} -> host {dst}, cổng đích {port}",
            "ACL {acl} of {dev}: line {seq} should {action_en} {proto} {src} -> host {dst}, "
            "destination port {port}",
        ),
    ),
    "apply_acl": (
        _t(
            "apply_acl.t1",
            "Áp ACL {acl} theo {dir_vi} lên {dev} {itf}",
            "Apply ACL {acl} {dir_en} on {dev} {itf}",
        ),
        _t(
            "apply_acl.t2",
            "Trên {dev}, gắn access-list {acl} vào interface {itf}, {dir_vi}",
            "On {dev}, attach access-list {acl} to interface {itf}, {dir_en}",
        ),
        _t(
            "apply_acl.t3",
            "Interface {itf} của {dev} phải lọc lưu lượng {dir_vi} bằng ACL {acl}",
            "Interface {itf} of {dev} must filter {dir_en} traffic with ACL {acl}",
        ),
    ),
    "set_aaa_login": (
        _t(
            "set_aaa_login.t1",
            "Cấu hình login authentication list {list} trên {dev} dùng {m_vi}{key_vi}",
            "Configure login authentication list {list} on {dev} using {m_en}{key_en}",
        ),
        _t(
            "set_aaa_login.t2",
            "Trên {dev}, method list đăng nhập {list} xác thực bằng {m_vi}{key_vi}",
            "On {dev}, the login method list {list} should authenticate with {m_en}{key_en}",
        ),
        _t(
            "set_aaa_login.t3",
            "Đăng nhập VTY của {dev} dùng list {list}: {m_vi}{key_vi}",
            "VTY logins on {dev} use list {list}: {m_en}{key_en}",
        ),
    ),
}

SINGLE_PICKERS: dict[str, Picker] = {
    "create_vlan": pick_create_vlan,
    "set_access_vlan": pick_set_access_vlan,
    "set_trunk": pick_set_trunk,
    "set_interface_ip": pick_set_interface_ip,
    "set_interface_state": pick_set_interface_state,
    "set_interface_description": pick_set_interface_description,
    "add_static_route": pick_add_static_route,
    "remove_static_route": pick_remove_static_route,
    "add_acl_entry": pick_add_acl_entry,
    "apply_acl": pick_apply_acl,
    "set_aaa_login": pick_set_aaa_login,
}

# (template, picker, slug trong case_id)
MULTI_TEMPLATES: tuple[tuple[Template, Picker, str], ...] = (
    (
        _t(
            "multi.vlan_access",
            "Tạo VLAN {vid} tên {name} trên {dev} và gán cổng {itf} vào VLAN {vid}",
            "Create VLAN {vid} named {name} on {dev} and put interface {itf} into VLAN {vid}",
        ),
        pick_multi_vlan_access,
        "multi_vlan_access",
    ),
    (
        _t(
            "multi.vlan_access_desc",
            "Trên {dev}: tạo VLAN {vid} ({name}), chuyển {itf} vào VLAN {vid} và đặt description '{text}'",
            "On {dev}: create VLAN {vid} ({name}), move {itf} into VLAN {vid} "
            "and set its description to '{text}'",
        ),
        pick_multi_vlan_access_desc,
        "multi_vlan_access_desc",
    ),
    (
        _t(
            "multi.acl_new_apply",
            "Trên {dev}, tạo ACL {acl} với dòng {seq} {action_vi} {proto} từ {src} "
            "tới host {dst} cổng {port}, rồi áp ACL {acl} {dir_vi} lên {itf}",
            "On {dev}, create ACL {acl} with line {seq} to {action_en} {proto} from {src} "
            "to host {dst} port {port}, then apply ACL {acl} {dir_en} on {itf}",
        ),
        pick_multi_acl_new_apply,
        "multi_acl_new_apply",
    ),
    (
        _t(
            "multi.ip_enable",
            "Đặt IP {cidr} cho {dev} {itf} rồi bật cổng đó lên",
            "Assign IP {cidr} to {dev} {itf} and bring the interface up",
        ),
        pick_multi_ip_enable,
        "multi_ip_enable",
    ),
    (
        _t(
            "multi.route_replace",
            "Trên {dev}, xoá route {old} qua {nh} và thay bằng route {prefix} qua {nh}",
            "On {dev}, remove the route {old} via {nh} and replace it with a route to {prefix} via {nh}",
        ),
        pick_multi_route_replace,
        "multi_route_replace",
    ),
)


# ----------------------------------------------------------------------------- sinh


@dataclass
class _Spec:
    template: Template
    slug: str
    context_id: str
    raw: dict[str, Any]
    slots: Slots
    calls: Calls
    lang: str


def _draw(rng: random.Random, ctx: _BenchCtx, picker: Picker, n: int, what: str) -> list[tuple[Slots, Calls]]:
    """n thao tác KHÁC NHAU từ picker; không đủ sau nhiều lần thử → lỗi (context không đủ chỗ cho n case)."""
    out: list[tuple[Slots, Calls]] = []
    seen: set[str] = set()
    attempts = 0
    while len(out) < n:
        attempts += 1
        if attempts > 50 * n + 100:
            raise BenchmarkError(f"không sinh đủ {n} case khác nhau cho {what}: context không đủ chỗ")
        got = picker(rng, ctx)
        if got is None:
            continue
        fp = json.dumps(got[1], sort_keys=True)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(got)
    return out


def _specs(seed: int, raws: Sequence[dict[str, Any]], per_tool: int, splits: dict[str, str]) -> list[_Spec]:
    specs: list[_Spec] = []
    for raw in raws:
        cid = raw["context_id"]
        ctx = _BenchCtx(raw)
        rng = random.Random(f"{seed}-{cid}")  # noqa: S311 — tất định theo seed, không phải mật mã
        for tool in TOOL_ORDER:
            # Case thứ k dùng template k mod 3, template dev trước: với per_tool 4, template test nhận 1/4
            # case mỗi context (template dev đầu tiên nhận 2), nên tỉ lệ test gần 30% thay vì phụ thuộc vào
            # việc template nào bị chọn làm test.
            templates = sorted(SINGLE_TEMPLATES[tool], key=lambda t: splits[t.template_id] == "test")
            for k, (slots, calls) in enumerate(
                _draw(rng, ctx, SINGLE_PICKERS[tool], per_tool, f"{tool}@{cid}")
            ):
                lang = rng.choice(("vi", "en"))
                specs.append(_Spec(templates[k % len(templates)], tool, cid, raw, slots, calls, lang))
        for template, picker, slug in MULTI_TEMPLATES:
            for slots, calls in _draw(rng, ctx, picker, MULTI_PER_TEMPLATE, f"{template.template_id}@{cid}"):
                specs.append(_Spec(template, slug, cid, raw, slots, calls, rng.choice(("vi", "en"))))
    return specs


def _case_id(spec: _Spec, index: int) -> str:
    return f"clean-{spec.slug}-{index:04d}"


def _intents(spec: _Spec) -> tuple[str, str]:
    return spec.template.vi.format(**spec.slots), spec.template.en.format(**spec.slots)


def _label_problems(spec: _Spec, store: ContextStore) -> list[str]:
    """Lý do một nhãn KHÔNG phải gold: thao tác L2 trên thiết bị không phải iosvl2 (sensor chặn ở B4), gate
    (iii) từ chối khi phát lại tuần tự ở role security_admin, hoặc một giá trị không neo được vào intent VI/EN
    (D-061). Hàm thuần: không đổi RNG, không ghi file."""
    out: list[str] = []
    platforms = {d["hostname"]: d["platform"] for d in spec.raw["devices"]}
    for c in spec.calls:
        if c["tool"] in L2_TOOLS and platforms.get(c["args"]["device"]) != "iosvl2":
            out.append(f"{c['tool']} trên {c['args']['device']} (không phải iosvl2)")
    approved = [ToolCall.model_validate(c) for c in spec.calls]
    staged: list[ToolCall] = []
    for c in spec.calls:
        decision = gate_evaluate(call=c, role=ROLE.value, store=store, approved=approved, staged=staged)
        if not decision.allowed:
            out.append(f"gate {decision.first_failed}: {decision.reason}")
            break
        staged.append(ToolCall.model_validate(c))
    for lang, intent in zip(("vi", "en"), _intents(spec), strict=True):
        out += [f"grounding {lang}: {v.field}={v.value}" for v in ground(intent, approved, store)]
    return out


def _verify(specs: Sequence[_Spec]) -> None:
    """Kiểm MỌI nhãn bằng chính control của hệ thống trước khi ghi file: context qua invariant (i), thao tác
    qua gate (iii), giá trị neo được vào intent (D-061). Có case hỏng → BenchmarkError liệt kê case_id, không
    ghi gì: một context mới không thể cho ra gold chưa kiểm."""
    stores: dict[str, ContextStore] = {}
    bad: list[str] = []
    for index, spec in enumerate(specs, start=1):
        if spec.context_id not in stores:
            try:
                stores[spec.context_id] = ContextStore.from_bytes(
                    canonical_context_bytes(spec.raw), invariants=True
                )
                violations = check_invariants(NetworkContext.model_validate(spec.raw))
            except (ContextError, ValidationError) as exc:
                raise BenchmarkError(f"context {spec.context_id} không hợp lệ: {exc}") from None
            if violations:
                codes = sorted({v.code for v in violations})
                raise BenchmarkError(f"context {spec.context_id} vi phạm invariant: {codes}")
        problems = _label_problems(spec, stores[spec.context_id])
        if problems:
            bad.append(f"{_case_id(spec, index)} ({'; '.join(problems)})")
    if bad:
        more = f" … và {len(bad) - 10} case khác" if len(bad) > 10 else ""
        raise BenchmarkError(f"{len(bad)} nhãn không qua kiểm tra: " + ", ".join(bad[:10]) + more)


def _splits(seed: int) -> dict[str, str]:
    """template_id → dev/test. Phân tầng: mỗi tool một tầng, các template đa thao tác một tầng."""
    rng = random.Random(f"{seed}-split")  # noqa: S311 — tất định theo seed, không phải mật mã
    strata = [[t.template_id for t in SINGLE_TEMPLATES[tool]] for tool in TOOL_ORDER]
    strata.append([t.template_id for t, _, _ in MULTI_TEMPLATES])
    out: dict[str, str] = {}
    for ids in strata:
        order = list(ids)
        rng.shuffle(order)
        n_test = max(1, round(len(order) * TEST_FRACTION))
        out |= {tid: ("test" if i < n_test else "dev") for i, tid in enumerate(order)}
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha(data)


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _prepare_out(out: Path) -> None:
    """Thư mục đích phải rỗng, hoặc là một benchmark cũ (manifest có `benchmark_version`) thì ghi đè cả thư
    mục. Thư mục lạ có dữ liệu → từ chối, không xoá file của người khác."""
    if out.exists() and any(out.iterdir()):
        manifest = out / "manifest.json"
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if not isinstance(old, dict) or "benchmark_version" not in old:
            raise ValueError(f"{out} không rỗng và không phải benchmark: không ghi đè")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def gen_benchmark(seed: int, contexts: Sequence[Path | str], per_tool: int, out: Path) -> Manifest:
    """Sinh benchmark vào `out` và trả về manifest (cũng ghi ở `out/manifest.json`). Xem docstring module."""
    out = Path(out)
    raws: list[dict[str, Any]] = []
    for path in contexts:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        NetworkContext.model_validate(raw)  # base phải hợp lệ về cấu trúc
        raws.append(raw)
    ids = [r["context_id"] for r in raws]
    if len(set(ids)) != len(ids):
        raise ValueError(f"context_id trùng nhau: {ids}")
    splits = _splits(seed)
    specs = _specs(seed, raws, per_tool, splits)
    _verify(specs)  # trước khi đụng tới thư mục đích
    _prepare_out(out)

    manifest: Manifest = {
        "benchmark_version": BENCHMARK_VERSION,
        "generator_version": BENCHMARK_VERSION,
        "seed": seed,
        "per_tool": per_tool,
        "multi_per_template": MULTI_PER_TEMPLATE,
        "test_fraction": TEST_FRACTION,
        "role": ROLE.value,
        "contexts": {},
        "counts": {},
        "templates": {},
        "families": {},
        "cases": {},
    }
    ctx_files: dict[str, tuple[str, str]] = {}
    for path, raw in zip(contexts, raws, strict=True):
        data = canonical_context_bytes(raw)
        digest = _write(out / "contexts" / f"{_sha(data)}.json", data)
        ctx_files[raw["context_id"]] = (f"contexts/{digest}.json", digest)
        manifest["contexts"][raw["context_id"]] = {
            "source": Path(path).name,
            "file": f"contexts/{digest}.json",
            "sha256": digest,
        }

    for t in [
        *(t for tool in TOOL_ORDER for t in SINGLE_TEMPLATES[tool]),
        *(t for t, _, _ in MULTI_TEMPLATES),
    ]:
        tools = (
            [t.template_id.split(".")[0]]
            if not t.template_id.startswith("multi.")
            else None  # điền theo case bên dưới
        )
        manifest["templates"][t.template_id] = {
            "split": splits[t.template_id],
            "tools": tools,
            "vi": t.vi,
            "en": t.en,
        }

    for index, spec in enumerate(specs, start=1):
        case_id = _case_id(spec, index)
        vi, en = _intents(spec)
        ctx_path, ctx_sha = ctx_files[spec.context_id]
        tools = [c["tool"] for c in spec.calls]
        case = TestCase(
            case_id=case_id,
            subset=Subset.CLEAN,
            config_type=CONFIG_TYPE[tools[0]],
            seed=seed,
            generator_version=BENCHMARK_VERSION,
            intent=vi if spec.lang == "vi" else en,
            intent_template=en,
            role=ROLE,
            context_ref=ContextRef(path=ctx_path, sha256=ctx_sha),
            expected_verdict=Verdict.ACCEPT,
            expected_tool_calls=[parse_tool_call(c) for c in spec.calls],
        )
        draft = Draft(Subset.CLEAN, case.config_type, ROLE, spec.raw, vi, en, Verdict.ACCEPT, spec.calls)
        scenario = _oracle_scenario(draft, case)
        parsed = [parse_tool_call(c) for c in spec.calls]
        rendered = render_by_device([(c.tool, c.parsed) for c in parsed])

        case_sha = _write(
            out / f"{case_id}.json",
            (case.model_dump_json(indent=2, exclude_none=True) + "\n").encode("utf-8"),
        )
        sc_sha = _write(out / "scenarios" / f"{case_id}.json", _json_bytes(scenario))
        gold = {
            device: {
                "file": f"gold/{case_id}/{device}.cfg",
                "sha256": _write(out / "gold" / case_id / f"{device}.cfg", text.encode("utf-8")),
            }
            for device, text in rendered.items()
        }
        split = splits[spec.template.template_id]
        family = f"{spec.template.template_id}@{spec.context_id}"
        entry: dict[str, Any] = {
            "file": f"{case_id}.json",
            "fake_scenario": f"scenarios/{case_id}.json",
            "group": "benchmark",
            "sha256": case_sha,
            "scenario_sha256": sc_sha,
            "split": split,
            "family": family,
            "template": spec.template.template_id,
            "context_id": spec.context_id,
            "tools": tools,
            "multi_op": len(tools) > 1,
            "lang": spec.lang,
            "intent_vi": vi,
            "intent_en": en,
            "gold": gold,
        }
        manifest["cases"][case_id] = entry
        fam = manifest["families"].setdefault(
            family,
            {
                "template": spec.template.template_id,
                "context_id": spec.context_id,
                "split": split,
                "cases": [],
            },
        )
        fam["cases"].append(case_id)
        tmpl = manifest["templates"][spec.template.template_id]
        if tmpl["tools"] is None:
            tmpl["tools"] = tools

    cases = manifest["cases"].values()
    manifest["counts"] = {
        "cases": len(manifest["cases"]),
        "single_op": sum(not m["multi_op"] for m in cases),
        "multi_op": sum(m["multi_op"] for m in cases),
        "dev": sum(m["split"] == "dev" for m in cases),
        "test": sum(m["split"] == "test" for m in cases),
        "by_context": {cid: sum(m["context_id"] == cid for m in cases) for cid in ids},
        "families": len(manifest["families"]),
        "templates": len(manifest["templates"]),
    }
    _write(out / "manifest.json", _json_bytes(manifest))
    return manifest


# ----------------------------------------------------------------------------- context dẫn xuất smb-01


def collector_bytes(raw: dict[str, Any]) -> bytes:
    """Định dạng ghi file context của collector (`write_context`): giữ thứ tự khoá, thụt 2, LF, UTF-8."""
    return (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def derive_l2_context(ref: bytes, switches: Sequence[str] = SMB01_IOL_L2) -> bytes:
    """`smb-01-collected-ref-l2.json` từ byte của snapshot gốc: đổi `platform` của các switch IOL L2 thành
    `iosvl2`, không đổi gì khác, ghi theo định dạng của collector (khác bản gốc đúng một dòng mỗi switch).
    Snapshot gốc phải đúng định dạng collector và mỗi switch phải đang khai `ios`, không thì ValueError."""
    raw = json.loads(ref)
    if collector_bytes(raw) != ref:
        raise ValueError("snapshot gốc không đúng định dạng ghi của collector")
    by_name = {d["hostname"]: d for d in raw["devices"]}
    for host in switches:
        if by_name.get(host, {}).get("platform") != "ios":
            raise ValueError(f"{host}: cần platform ios trong snapshot gốc")
        by_name[host]["platform"] = "iosvl2"
    return collector_bytes(raw)


# ----------------------------------------------------------------------------- nạp và kiểm


# `load_benchmark` nằm ở `eval/benchmark_io.py` (D-006: evaluator không import generator, vốn import gate
# và grounding); import lại ở đầu file để giữ chỗ gọi cũ (`corpus.benchmark.load_benchmark`).
