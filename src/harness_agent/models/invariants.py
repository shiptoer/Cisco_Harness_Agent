"""Domain invariants cho NetworkContext: control (i), phần domain (D-005, task 2.2).

`context.py` kiểm từng trường (kiểu, enum, dải giá trị, trùng lặp trong một danh sách). File này kiểm
các trường có KHỚP NHAU trong một context hay không: tham chiếu trỏ vào thứ có thật, VLAN và địa chỉ
nhất quán. Danh sách 16 mã và ví dụ vi phạm: `docs/design/invariants-plan.md` mục 3.

Hợp đồng hàm:
    check_invariants(ctx) -> list[Violation]   # danh sách rỗng = hợp lệ
Trả về danh sách thay vì raise, để control (i) ghi được MỌI vi phạm vào trace và để tập corrupted đo
được hệ thống có phát hiện đúng vi phạm được cài vào hay không (RQ2). Thứ tự ổn định: theo thứ tự trong
file (mỗi thiết bị: interfaces, static routes, gateway, AAA, mạng quản trị; rồi `links`; rồi `policy`).

`detail` chỉ chứa tên, mã, số VLAN và đường dẫn: KHÔNG chứa free-text (description, banner_motd, remark)
và KHÔNG chứa giá trị secret (với tham chiếu secret chỉ ghi TÊN khoá). Vi phạm đi vào trace và `reason`
của kết cục.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from ipaddress import IPv4Network

from .common import InterfaceMode, StrictModel
from .context import Device, NetworkContext


class Violation(StrictModel):
    code: str  # một mã trong INVARIANT_CODES, hoặc "CTX_SCHEMA" cho lỗi contract
    device: str  # hostname, hoặc "-" khi vi phạm không thuộc một thiết bị
    path: str  # đường dẫn tới trường vi phạm, vd "devices[1].interfaces[3].acl_in"
    detail: str


INVARIANT_CODES: tuple[str, ...] = (
    "CTX_DANGLING_VLAN",
    "CTX_TRUNK_MISMATCH",
    "CTX_NATIVE_VLAN_MISSING",
    "CTX_ACL_REF",
    "CTX_SVI_VLAN_MISSING",
    "CTX_UNUSED_NOT_PARKED",
    "CTX_RESERVED_VLAN_IN_USE",
    "CTX_DUP_IP",
    "CTX_SUBNET_OVERLAP",
    "CTX_NEXT_HOP_UNREACHABLE",
    "CTX_GATEWAY_UNREACHABLE",
    "CTX_SECRET_REF",
    "CTX_LOGIN_LIST_REF",
    "CTX_MGMT_UNREACHABLE",
    "CTX_LINK_REF",
    "CTX_PROTECTED_REF",
)


REDACTED_DETAIL = "[REDACTED: detail chứa giá trị secret của context]"
_SVI = re.compile(r"Vlan(\d+)")  # SVI: interface VLAN N, cần VLAN N trên thiết bị


def check_invariants(ctx: NetworkContext) -> list[Violation]:
    """Mọi vi phạm invariant của `ctx`, theo thứ tự trong file. Không raise vì dữ liệu."""
    found: list[Violation] = []
    first_ip: dict[str, str] = {}  # địa chỉ IPv4 → đường dẫn của interface dùng nó đầu tiên
    for d_idx, device in enumerate(ctx.devices):
        found += _device(ctx, d_idx, device, first_ip)
    found += _links(ctx)
    found += _protected(ctx)
    # Tên trong `detail` lấy từ context (tên khoá, tên ACL...). Một context sai có thể đặt chính giá trị
    # secret vào chỗ của tên (vd `key_ref` = giá trị của key): khi đó che cả `detail`.
    secrets = ctx.secret_values()
    return [
        v if not any(s in v.detail for s in secrets) else v.model_copy(update={"detail": REDACTED_DETAIL})
        for v in found
    ]


def _v(code: str, device: str, path: str, detail: str) -> Violation:
    return Violation(code=code, device=device, path=path, detail=detail)


def _device(ctx: NetworkContext, d_idx: int, device: Device, first_ip: dict[str, str]) -> Iterator[Violation]:
    host = device.hostname
    base = f"devices[{d_idx}]"
    vlans = {v.vlan_id for v in device.vlans}
    policy = ctx.policy
    networks: list[tuple[str, IPv4Network]] = []  # (đường dẫn ipv4, subnet) của các interface có IP

    for i_idx, itf in enumerate(device.interfaces):
        at = f"{base}.interfaces[{i_idx}]"
        if itf.access_vlan is not None and itf.access_vlan not in vlans:
            yield _v(
                "CTX_DANGLING_VLAN", host, f"{at}.access_vlan", f"VLAN {itf.access_vlan} không có trên {host}"
            )
        for k, vlan in enumerate(itf.trunk_allowed or []):
            if vlan not in vlans:
                yield _v(
                    "CTX_TRUNK_MISMATCH",
                    host,
                    f"{at}.trunk_allowed[{k}]",
                    f"VLAN {vlan} không có trên {host}",
                )
        if itf.native_vlan is not None and itf.native_vlan not in vlans:
            yield _v(
                "CTX_NATIVE_VLAN_MISSING",
                host,
                f"{at}.native_vlan",
                f"VLAN {itf.native_vlan} không có trên {host}",
            )
        for side in ("acl_in", "acl_out"):
            name = getattr(itf, side)
            if name is not None and name not in device.acls:
                yield _v("CTX_ACL_REF", host, f"{at}.{side}", f"ACL {name} không định nghĩa trên {host}")
        svi = _SVI.fullmatch(itf.name)
        if svi and int(svi.group(1)) not in vlans:
            yield _v(
                "CTX_SVI_VLAN_MISSING",
                host,
                f"{at}.name",
                f"{itf.name} không có VLAN {svi.group(1)} trên {host}",
            )
        if (
            itf.mode is InterfaceMode.UNUSED
            and itf.access_vlan is not None
            and policy.parking_vlan is not None
            and itf.access_vlan != policy.parking_vlan
        ):
            yield _v(
                "CTX_UNUSED_NOT_PARKED",
                host,
                f"{at}.access_vlan",
                f"cổng unused ở VLAN {itf.access_vlan}, không phải VLAN parking {policy.parking_vlan}",
            )
        if itf.mode is InterfaceMode.ACCESS and itf.access_vlan in policy.reserved_vlans:
            yield _v(
                "CTX_RESERVED_VLAN_IN_USE",
                host,
                f"{at}.access_vlan",
                f"VLAN {itf.access_vlan} là VLAN dành riêng",
            )
        if itf.ipv4 is not None:
            ip_path = f"{at}.ipv4"
            address = str(itf.ipv4.ip)
            if address in first_ip:
                yield _v("CTX_DUP_IP", host, ip_path, f"địa chỉ trùng với {first_ip[address]}")
            else:
                first_ip[address] = ip_path
            # Chỉ xét trong CÙNG một thiết bị: hai đầu của một link dùng chung subnet là hợp lệ.
            for other_path, other_net in networks:
                if itf.ipv4.network.overlaps(other_net):
                    yield _v("CTX_SUBNET_OVERLAP", host, ip_path, f"subnet chồng với {other_path}")
                    break
            networks.append((ip_path, itf.ipv4.network))

    connected = [net for _, net in networks]
    for r_idx, route in enumerate(device.static_routes):
        if not any(route.next_hop in net for net in connected):
            yield _v(
                "CTX_NEXT_HOP_UNREACHABLE",
                host,
                f"{base}.static_routes[{r_idx}].next_hop",
                f"next-hop không nằm trong subnet kết nối trực tiếp nào của {host}",
            )
    if device.default_gateway is not None and not any(device.default_gateway in net for net in connected):
        yield _v(
            "CTX_GATEWAY_UNREACHABLE",
            host,
            f"{base}.default_gateway",
            f"default gateway không nằm trong subnet nào của {host}",
        )

    if device.aaa is not None:
        for u_idx, user in enumerate(device.aaa.local_users):
            if user.secret_ref not in device.secrets:
                yield _v(
                    "CTX_SECRET_REF",
                    host,
                    f"{base}.aaa.local_users[{u_idx}].secret_ref",
                    f"không có khoá {user.secret_ref} trong secrets của {host}",
                )
        for s_idx, server in enumerate(device.aaa.tacacs_servers):
            if server.key_ref not in device.secrets:
                yield _v(
                    "CTX_SECRET_REF",
                    host,
                    f"{base}.aaa.tacacs_servers[{s_idx}].key_ref",
                    f"không có khoá {server.key_ref} trong secrets của {host}",
                )
        vty = device.aaa.vty_login_list
        if vty is not None and vty not in device.aaa.login_lists:
            yield _v(
                "CTX_LOGIN_LIST_REF",
                host,
                f"{base}.aaa.vty_login_list",
                f"không có login list {vty} trên {host}",
            )

    # Thiết bị quản trị được khi có interface nằm trong mgmt_prefix, HOẶC có static route cụ thể
    # (không phải default)
    # bao trùm mgmt_prefix: vd router quản trị qua Loopback, đi tới mạng quản trị bằng route.
    mgmt = policy.mgmt_prefix
    if mgmt is not None:
        on_mgmt = any(net.subnet_of(mgmt) for net in connected)
        routed = any(r.prefix.prefixlen > 0 and mgmt.subnet_of(r.prefix) for r in device.static_routes)
        if not (on_mgmt or routed):
            yield _v(
                "CTX_MGMT_UNREACHABLE",
                host,
                base,
                f"{host} không có interface trong mgmt_prefix và không có route tới mgmt_prefix",
            )


def _links(ctx: NetworkContext) -> Iterator[Violation]:
    interfaces = {d.hostname: {i.name for i in d.interfaces} for d in ctx.devices}
    for l_idx, link in enumerate(ctx.links):
        for side in ("a", "b"):
            host = getattr(link, f"{side}_device")
            name = getattr(link, f"{side}_interface")
            if host not in interfaces:
                yield _v("CTX_LINK_REF", "-", f"links[{l_idx}].{side}_device", f"không có thiết bị {host}")
            elif name not in interfaces[host]:
                yield _v(
                    "CTX_LINK_REF", host, f"links[{l_idx}].{side}_interface", f"không có {name} trên {host}"
                )


def _protected(ctx: NetworkContext) -> Iterator[Violation]:
    interfaces = {d.hostname: {i.name for i in d.interfaces} for d in ctx.devices}
    for host, names in ctx.policy.protected_interfaces.items():
        at = f"policy.protected_interfaces.{host}"
        if host not in interfaces:
            yield _v("CTX_PROTECTED_REF", "-", at, f"không có thiết bị {host}")
            continue
        for k, name in enumerate(names):
            if name not in interfaces[host]:
                yield _v("CTX_PROTECTED_REF", host, f"{at}[{k}]", f"không có {name} trên {host}")
