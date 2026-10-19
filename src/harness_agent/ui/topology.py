"""Sơ đồ topology dạng SVG do server sinh (không JavaScript, D-057).

Thiết bị xếp theo tầng của `role` (router trên cùng, switch phân phối, switch truy cập), mỗi tầng một hàng,
căn
giữa; link là đoạn thẳng giữa hai thiết bị, nhãn interface đặt gần đầu mút của nó. Mọi chuỗi đi vào SVG được
escape; hostname và tên interface đã qua contract nhưng vẫn escape như mọi dữ liệu.
"""

from __future__ import annotations

from html import escape
from typing import Any

_TIERS = ("router", "distribution_switch", "core_switch", "access_switch")
BOX_W, BOX_H = 132, 44
GAP_X, ROW_H, PAD = 56, 118, 28


def _tier(role: str) -> int:
    return _TIERS.index(role) if role in _TIERS else len(_TIERS)


def _short(ifname: str) -> str:
    """GigabitEthernet0/1 → Gi0/1: nhãn ngắn để không đè lên nhau."""
    for long, short in (
        ("GigabitEthernet", "Gi"),
        ("FastEthernet", "Fa"),
        ("Ethernet", "Et"),
        ("Loopback", "Lo"),
    ):
        if ifname.startswith(long):
            return short + ifname[len(long) :]
    return ifname


def render_svg(topology: dict[str, Any]) -> str:
    devices = topology["devices"]
    rows: dict[int, list[dict[str, Any]]] = {}
    for d in devices:
        rows.setdefault(_tier(d["role"]), []).append(d)
    ordered = [rows[k] for k in sorted(rows)]
    widest = max((len(r) for r in ordered), default=1)
    width = PAD * 2 + widest * BOX_W + (widest - 1) * GAP_X
    height = PAD * 2 + len(ordered) * ROW_H - (ROW_H - BOX_H)
    pos: dict[str, tuple[float, float]] = {}
    for row_index, row in enumerate(ordered):
        span = len(row) * BOX_W + (len(row) - 1) * GAP_X
        x0 = (width - span) / 2
        y = PAD + row_index * ROW_H
        for i, d in enumerate(row):
            pos[d["hostname"]] = (x0 + i * (BOX_W + GAP_X), y)

    parts = [
        f'<svg class="topo" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
        f'role="img" aria-labelledby="topo-title"><title id="topo-title">'
        f"Sơ đồ topology của {escape(topology['context_id'])}: {len(devices)} thiết bị, "
        f"{len(topology['links'])} link</title>"
    ]
    for link in topology["links"]:
        if link["a"] not in pos or link["b"] not in pos:
            continue
        (ax, ay), (bx, by) = pos[link["a"]], pos[link["b"]]
        a = (ax + BOX_W / 2, ay + BOX_H / 2)
        b = (bx + BOX_W / 2, by + BOX_H / 2)
        parts.append(
            f'<line class="topo-link" x1="{a[0]:.0f}" y1="{a[1]:.0f}" x2="{b[0]:.0f}" y2="{b[1]:.0f}"/>'
        )
        for (px, py), (qx, qy), name in ((a, b, link["a_if"]), (b, a, link["b_if"])):
            # Nhãn đặt ở 28% đoạn thẳng tính từ thiết bị sở hữu interface, lệch sang phải 4px.
            lx, ly = px + (qx - px) * 0.28 + 4, py + (qy - py) * 0.28
            parts.append(f'<text class="topo-if" x="{lx:.0f}" y="{ly:.0f}">{escape(_short(name))}</text>')
    for d in devices:
        x, y = pos[d["hostname"]]
        protected = len(d["protected"])
        parts.append(
            f'<g class="topo-dev"><rect x="{x:.0f}" y="{y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="2"/>'
            f'<text class="topo-host" x="{x + 10:.0f}" y="{y + 19:.0f}">{escape(d["hostname"])}</text>'
            f'<text class="topo-meta" x="{x + 10:.0f}" y="{y + 35:.0f}">{escape(d["platform"])}, '
            f"{protected} cổng bảo vệ</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)
