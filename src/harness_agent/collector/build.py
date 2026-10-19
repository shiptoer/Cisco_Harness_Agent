"""Từ output `show` của các thiết bị + policy → file network context và file `.meta.json` đi kèm.

Policy KHÔNG lấy từ thiết bị: nó là quyết định của tổ chức (interface nào được bảo vệ, mạng quản trị, VLAN
dành riêng), nằm ở một file riêng do người viết, và được trộn vào context ở đây.

Collector không sửa contract. Thứ contract chưa mô tả được (VRF, nhiều hơn một mạng quản trị, banner
login, ACL standard...) không vào context; chúng được ghi ở `.meta.json`, mục `contract_gaps`.

Freshness (D-054): context do collector đọc từ thiết bị mang khối `provenance` (`source: collector`,
`collected_at`, `ttl_seconds`), nằm ngay sau `context_id` và thuộc phần được hash. Runner so `collected_at`
với `evaluated_at` của case ở B2+.

Trạng thái: ĐỀ XUẤT (D-047).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.models import NetworkContext
from harness_agent.models.context import Policy
from harness_agent.runner.context_check import context_violations

from .fetch import ALLOWED_COMMANDS, STAMP_FORMAT, CollectorError, Inventory
from .parse import (
    REDACTED,
    DeviceNotes,
    parse_cdp_neighbors,
    parse_interfaces_trunk,
    parse_ip_interface_brief,
    parse_running_config,
    parse_vlan_brief,
)

COLLECTOR_VERSION = "0.1.0"
# TTL mặc định của context thu thập (D-054): một ngày. Đổi bằng `--ttl-seconds`.
DEFAULT_TTL_SECONDS = 86400
# Khoá của file policy mà contract `Policy` có. Khoá khác (vd `mgmt_prefixes_all`) được giữ ở meta.
_POLICY_FIELDS = frozenset(Policy.model_fields)


def load_policy(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectorError(f"policy {path.name} không đọc được: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise CollectorError(f"policy {path.name}: gốc JSON phải là object")
    try:
        Policy.model_validate({k: v for k, v in data.items() if k in _POLICY_FIELDS})
    except ValidationError as exc:
        raise CollectorError(f"policy {path.name} sai contract: {exc.error_count()} lỗi") from exc
    return data


def build_context(
    inventory: Inventory, outputs: dict[str, dict[str, str]], policy: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(context dạng dict, ghi chú cho meta). Context CHƯA được validate: người gọi chạy contract và
    invariants trên file đã ghi (`write_context`)."""
    devices: list[dict[str, Any]] = []
    notes: dict[str, DeviceNotes] = {}
    crosscheck: dict[str, dict[str, Any]] = {}
    neighbors: dict[str, list[tuple[str, str, str]]] = {}
    for inv in inventory.devices:
        if inv.hostname not in outputs or "show running-config" not in outputs[inv.hostname]:
            raise CollectorError(f"không có `show running-config` của {inv.hostname}")
        by_command = outputs[inv.hostname]
        device, device_notes = parse_running_config(by_command["show running-config"], inv.role)
        if device["hostname"] != inv.hostname:
            raise CollectorError(
                f"{inv.hostname}: hostname trong running-config không khớp inventory (kết nối nhầm thiết bị?)"
            )
        device.update(platform=inv.platform, role=inv.role, site=inv.site)
        devices.append(device)
        notes[inv.hostname] = device_notes
        crosscheck[inv.hostname] = _crosscheck(device, by_command)
        neighbors[inv.hostname] = parse_cdp_neighbors(by_command.get("show cdp neighbors detail", ""))

    links, dropped = _links(inventory, devices, neighbors)
    context: dict[str, Any] = {
        "schema_version": "1.0",
        "context_id": inventory.context_id,
        "devices": devices,
        "links": links,
        "policy": {k: v for k, v in policy.items() if k in _POLICY_FIELDS},
    }
    if inventory.domain_name:
        context["domain_name"] = inventory.domain_name
    extra_policy = {k: v for k, v in policy.items() if k not in _POLICY_FIELDS}
    meta_notes = {
        "devices": {name: n.as_dict() for name, n in notes.items()},
        "crosscheck": crosscheck,
        "links_dropped": dropped,
        "contract_gaps": _contract_gaps(notes, extra_policy),
        "policy_not_in_contract": extra_policy,
        "secrets": {
            "values_in_context": "none",
            "placeholder": REDACTED,
            "names": {d["hostname"]: sorted(d["secrets"]) for d in devices},
            "lines_removed": sum(n.secret_lines_removed for n in notes.values()),
        },
    }
    return context, meta_notes


def _crosscheck(device: dict[str, Any], by_command: dict[str, str]) -> dict[str, Any]:
    """Đối chiếu những gì đọc từ running-config với ba lệnh show khác. Chỉ BÁO chỗ lệch; không sửa context.
    Lệnh không có output (không chạy, hoặc thiết bị không hỗ trợ) → `null`."""
    result: dict[str, Any] = {"vlan_brief": None, "ip_interface_brief": None, "interfaces_trunk": None}
    vlans = {v["vlan_id"]: v["name"] for v in device["vlans"]}
    brief = parse_vlan_brief(by_command.get("show vlan brief", ""))
    if brief:
        # VLAN 1 và 1002–1005 luôn có trong `show vlan brief` nhưng không nằm trong running-config.
        builtin = {1, 1002, 1003, 1004, 1005}
        result["vlan_brief"] = {
            "only_in_running_config": sorted(set(vlans) - set(brief)),
            "only_in_show_vlan": sorted(set(brief) - set(vlans) - builtin),
            "name_differs": sorted(v for v in set(vlans) & set(brief) if vlans[v] != brief[v]),
        }
    status = parse_ip_interface_brief(by_command.get("show ip interface brief", ""))
    if status:
        by_name = {i["name"]: i for i in device["interfaces"]}
        result["ip_interface_brief"] = {
            "only_in_running_config": sorted(set(by_name) - set(status)),
            "only_in_show": sorted(set(status) - set(by_name)),
            "admin_state_differs": sorted(
                name
                for name in set(by_name) & set(status)
                if by_name[name]["enabled"] == (status[name]["status"] == "administratively down")
            ),
            "ip_differs": sorted(
                name
                for name in set(by_name) & set(status)
                if (by_name[name].get("ipv4") or "unassigned/").split("/")[0] != status[name]["ip"]
            ),
        }
    trunks = parse_interfaces_trunk(by_command.get("show interfaces trunk", ""))
    if trunks:
        configured = {i["name"]: i for i in device["interfaces"] if i["mode"] == "trunk"}
        result["interfaces_trunk"] = {
            "configured_but_not_trunking": sorted(set(configured) - set(trunks)),
            "trunking_but_not_configured": sorted(set(trunks) - set(configured)),
            "native_differs": sorted(
                name
                for name in set(configured) & set(trunks)
                if configured[name].get("native_vlan", 1) != trunks[name].get("native", 1)
            ),
            "allowed_differs": sorted(
                name
                for name in set(configured) & set(trunks)
                if configured[name].get("trunk_allowed") is not None
                and "allowed" in trunks[name]
                and configured[name]["trunk_allowed"] != trunks[name]["allowed"]
            ),
        }
    return result


def _links(
    inventory: Inventory, devices: list[dict[str, Any]], neighbors: dict[str, list[tuple[str, str, str]]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Link từ CDP, mỗi link một lần (đầu có hostname nhỏ hơn đứng trước). Bị BỎ, kèm lý do:
    - link qua cổng quản lý của một trong hai đầu (mạng OOB dùng chung: mọi thiết bị thấy nhau, đó không
      phải topology của mạng dữ liệu);
    - link tới thiết bị ngoài inventory (vd router của ISP);
    - link mà một đầu không có trong danh sách interface đã thu thập."""
    mgmt = {d.hostname: d.mgmt_interface for d in inventory.devices}
    known = {d["hostname"]: {i["name"] for i in d["interfaces"]} for d in devices}
    links: dict[tuple[str, str, str, str], dict[str, str]] = {}
    dropped: list[dict[str, str]] = []
    for local, found in neighbors.items():
        for local_if, remote, remote_if in found:
            entry = {
                "a_device": local,
                "a_interface": local_if,
                "b_device": remote[:63],
                "b_interface": remote_if[:60],
            }
            if remote not in known:
                dropped.append({**entry, "reason": "neighbor_not_in_inventory"})
            elif local_if == mgmt[local] or remote_if == mgmt[remote]:
                dropped.append({**entry, "reason": "management_interface"})
            elif local_if not in known[local] or remote_if not in known[remote]:
                dropped.append({**entry, "reason": "interface_not_collected"})
            else:
                a, b = sorted([(local, local_if), (remote, remote_if)])
                links[(*a, *b)] = {
                    "a_device": a[0],
                    "a_interface": a[1],
                    "b_device": b[0],
                    "b_interface": b[1],
                }
    return [links[key] for key in sorted(links)], dropped


def _contract_gaps(notes: dict[str, DeviceNotes], extra_policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Những thứ có trên thiết bị (hoặc trong policy) mà contract hiện tại không mô tả được, gom theo loại."""
    explain = {
        "vrf_forwarding": "interface thuộc một VRF: contract không có trường VRF",
        "vrf_definition": "khai báo VRF",
        "vrf_route": "static route trong VRF",
        "interface_name": "interface có tên contract không nhận",
        "banner_login": "banner login: contract chỉ có `banner_motd`",
        "route_name": "tên của static route (`name ...`)",
        "route_exit_interface": "static route theo cổng ra: `StaticRoute` đòi `next_hop`",
        "acl_log": "từ khoá `log` của một dòng ACL",
        "acl_standard_as_extended": "ACL standard: ghi thành `ip <nguồn> any`, contract chỉ có dạng extended",
        "acl_entry_unsupported": "dòng ACL có thứ contract không có (cổng nguồn, range...): bỏ cả dòng",
        "vty_access_class": "ACL áp lên line vty (`access-class`)",
        "aaa_authorization": "aaa authorization",
        "aaa_accounting": "aaa accounting",
        "aaa_login_method": "phương thức đăng nhập contract không có (enable, none, line...)",
        "tacacs_server_option": "tuỳ chọn của tacacs server (timeout...)",
        "secondary_address": "địa chỉ IP phụ của interface",
        "trunk_allowed_keyword": "`switchport trunk allowed vlan all/none/except`",
    }
    totals: dict[str, dict[str, int]] = {}
    for hostname, device_notes in notes.items():
        for kind, count in device_notes.not_represented.items():
            totals.setdefault(kind, {})[hostname] = count
    gaps = [
        {
            "kind": kind,
            "what": explain.get(kind, kind),
            "count": sum(by.values()),
            "devices": dict(sorted(by.items())),
        }
        for kind, by in sorted(totals.items())
    ]
    # Tên THẬT của các interface bị bỏ, theo thiết bị: không in một ví dụ cố định thay cho chúng.
    skipped = {name: n.skipped_interfaces for name, n in sorted(notes.items()) if n.skipped_interfaces}
    for gap in gaps:
        if gap["kind"] == "interface_name":
            gap["names"] = skipped
    if extra_policy:
        gaps.append(
            {
                "kind": "policy_fields",
                "what": "khoá của file policy mà contract `Policy` không có (vd nhiều hơn một mạng quản trị)",
                "count": len(extra_policy),
                "fields": sorted(extra_policy),
            }
        )
    return gaps


def collector_provenance(collected_at: datetime, ttl_seconds: int) -> dict[str, Any]:
    """Khối `provenance` của context đọc từ thiết bị (D-054). `collected_at` là mốc của lượt thu thập."""
    return {
        "source": "collector",
        "collected_at": collected_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": ttl_seconds,
    }


def with_provenance(context: dict[str, Any], provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Bản sao của `context` có khối `provenance` ngay sau `context_id` (None: không thêm). Thứ tự khoá quyết
    định byte của file, tức hash của nó."""
    out: dict[str, Any] = {}
    for key, value in context.items():
        if key == "provenance":
            continue
        out[key] = value
        if key == "context_id" and provenance is not None:
            out["provenance"] = provenance
    return out


def write_context(
    out_dir: Path,
    context: dict[str, Any],
    meta_notes: dict[str, Any],
    source: str,
    files: dict[str, Path],
    now: datetime | None = None,
    *,
    provenance: dict[str, Any] | None,
) -> tuple[Path, Path, list[dict[str, str]]]:
    """Ghi `<context_id>-<thời gian UTC>.json` và `.meta.json`, rồi chạy contract + invariants trên file đã
    ghi. Trả về (file context, file meta, danh sách vi phạm). Có vi phạm thì VẪN ghi file và báo nguyên
    trong meta: collector không sửa dữ liệu để qua kiểm tra.

    `provenance` (D-054) đi vào context; người gọi phải truyền rõ, kể cả None (context không có khối này
    được coi là synthetic và không bị kiểm TTL)."""
    context = with_provenance(context, provenance)
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    name = f"{context['context_id']}-{stamp.strftime(STAMP_FORMAT)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    context_path = out_dir / f"{name}.json"
    body = json.dumps(context, ensure_ascii=False, indent=2) + "\n"
    context_path.write_text(body, encoding="utf-8", newline="\n")
    violations = [v.model_dump() for v in context_violations(context_path)]
    devices = context["devices"]
    meta = {
        "collected_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "provenance": provenance,
        "collector_version": COLLECTOR_VERSION,
        "context_file": context_path.name,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "commands": list(ALLOWED_COMMANDS),
        "inputs": {
            key: {"path": path.as_posix(), "sha256": _sha256(path)} for key, path in sorted(files.items())
        },
        "counts": {
            "devices": len(devices),
            "interfaces": sum(len(d["interfaces"]) for d in devices),
            "vlans": sum(len(d["vlans"]) for d in devices),
            "static_routes": sum(len(d["static_routes"]) for d in devices),
            "acls": sum(len(d["acls"]) for d in devices),
            "acl_entries": sum(len(e) for d in devices for e in d["acls"].values()),
            "aaa": sum("aaa" in d for d in devices),
            "links": len(context["links"]),
        },
        "validation": {
            "contract_and_invariants": "ok" if not violations else "violations",
            "violations": violations,
        },
        **meta_notes,
    }
    meta_path = out_dir / f"{name}.meta.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return context_path, meta_path, violations


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def validate_only(context: dict[str, Any]) -> int:
    """Số lỗi contract của một context dạng dict (không ghi file). Dùng trong test."""
    try:
        NetworkContext.model_validate(context)
    except ValidationError as exc:
        return exc.error_count()
    return 0
