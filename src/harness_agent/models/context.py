"""NetworkContext: intended state của mạng, đóng vai source of truth (contract v0.2, D-042).

Định dạng theo file context thật (`data/contexts/*.json`), là định dạng mà harness, 15 case chạy khô
và corpus dùng.

Phạm vi file này là validation CẤU TRÚC (kiểu, dải giá trị, enum, trùng lặp trong cùng một danh
sách, các trường đi cùng nhau theo `mode` của interface). Ràng buộc NGỮ NGHĨA liên đối tượng
(access_vlan phải tồn tại, VLAN của trunk phải tồn tại, IP không trùng giữa các thiết bị,
`secret_ref`/`key_ref`/ACL/login list trỏ tới thứ có thật, link trỏ tới interface có thật, ...) là
domain invariants, thuộc task 2.2 (`models/invariants.py`, D-005).

Secret: giá trị trong `Device.secrets` là `SecretStr`: `model_dump`, `model_dump_json`, `repr` đều
che giá trị. Chỗ DUY NHẤT lấy giá trị thật là `NetworkContext.secret_values()`, dùng cho phép quét
leak.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator

from harness_agent.tools.registry.tools.aaa import AuthMethod

from .common import (
    AnyVlanId,
    DeviceRole,
    FreeText,
    Hostname,
    InterfaceMode,
    InterfaceName,
    Platform,
    StrictModel,
    VlanName,
)

# Phiên bản ĐỊNH DẠNG file context (trường `schema_version` của file), không phải phiên bản của contract.
SCHEMA_VERSION = "1.0"

# Ngưỡng độ dài của secret dùng cho phép quét leak: chuỗi ngắn hơn dễ trùng ngẫu nhiên với output bình
# thường.
# Giống `ContextStore.secret_values()`. Contract vẫn nhận secret ngắn (min_length=1).
MIN_SCANNED_SECRET_LEN = 8

ContextId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
DomainName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$")]
SiteName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
# Cùng pattern với tham số của registry (`ACL_NAME_PATTERN`, `LIST_NAME_PATTERN`).
AclName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
ListName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
ServerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
Username = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,32}$")]
# Tên khoá trong `Device.secrets`; `secret_ref`, `key_ref` là tên này, KHÔNG phải giá trị. Cùng pattern với
# `KEY_REF_PATTERN` của registry.
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
SecretValue = Annotated[SecretStr, Field(min_length=1)]
# Đầu mút ACL dạng IOS: `any`, `host A.B.C.D`, hoặc `A.B.C.D W.W.W.W` (địa chỉ + wildcard).
_IPV4 = r"\d{1,3}(\.\d{1,3}){3}"
AclEndpoint = Annotated[str, StringConstraints(pattern=rf"^(any|host {_IPV4}|{_IPV4} {_IPV4})$")]


class AclAction(StrEnum):
    PERMIT = "permit"
    DENY = "deny"


class AclProtocol(StrEnum):
    IP = "ip"
    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"


class Vlan(StrictModel):
    vlan_id: AnyVlanId
    name: VlanName


class Interface(StrictModel):
    name: InterfaceName
    mode: InterfaceMode
    enabled: bool = True
    access_vlan: AnyVlanId | None = None
    trunk_allowed: list[AnyVlanId] | None = None
    native_vlan: AnyVlanId | None = None
    ipv4: IPv4Interface | None = None
    acl_in: AclName | None = None
    acl_out: AclName | None = None
    # UNTRUSTED: nguồn tấn công A2 (indirect prompt injection).
    description: FreeText = ""

    @model_validator(mode="after")
    def _mode_fields_consistent(self) -> Interface:
        trunk = self.trunk_allowed is not None or self.native_vlan is not None
        if self.mode is InterfaceMode.ACCESS:
            if self.access_vlan is None:
                raise ValueError("access interface phải có access_vlan")
            if self.ipv4 is not None or trunk:
                raise ValueError("access interface không được có ipv4, trunk_allowed hoặc native_vlan")
        elif self.mode is InterfaceMode.TRUNK:
            if self.access_vlan is not None or self.ipv4 is not None:
                raise ValueError("trunk interface không được có access_vlan hoặc ipv4")
        elif self.mode is InterfaceMode.ROUTED:
            if self.access_vlan is not None or trunk:
                raise ValueError("routed interface không được có access_vlan, trunk_allowed hoặc native_vlan")
        elif self.mode is InterfaceMode.UNUSED:
            # Cổng không dùng: tắt, không địa chỉ, không trunk, không ACL. access_vlan (VLAN parking)
            # được phép.
            if self.enabled:
                raise ValueError("unused interface phải có enabled = false")
            if self.ipv4 is not None or trunk or self.acl_in is not None or self.acl_out is not None:
                raise ValueError("unused interface không được có ipv4, trunk_allowed, native_vlan hoặc ACL")
        return self


class StaticRoute(StrictModel):
    prefix: IPv4Network
    next_hop: IPv4Address


class AclEntry(StrictModel):
    seq: int = Field(ge=1, le=2147483647)
    action: AclAction
    protocol: AclProtocol
    src: AclEndpoint
    dst: AclEndpoint
    dst_port: int | None = Field(default=None, ge=1, le=65535)
    # UNTRUSTED
    remark: FreeText | None = None

    @field_validator("src", "dst")
    @classmethod
    def _addresses_parse(cls, value: str) -> str:
        for part in value.split()[1:] if value.startswith("host ") else value.split():
            if part != "any":
                ipaddress.IPv4Address(part)  # ValueError nếu có octet > 255
        return value

    @model_validator(mode="after")
    def _port_needs_l4(self) -> AclEntry:
        if self.dst_port is not None and self.protocol not in (AclProtocol.TCP, AclProtocol.UDP):
            raise ValueError("dst_port chỉ dùng với tcp hoặc udp")
        return self


class AaaLocalUser(StrictModel):
    """Có mặt trong context để kiểm tra control (ii): projection KHÔNG BAO GIỜ được lộ trường này."""

    username: Username
    privilege: int = Field(ge=0, le=15)
    secret_ref: SecretName  # tên khoá trong `Device.secrets`, không phải giá trị


class TacacsServer(StrictModel):
    name: ServerName
    address: IPv4Address
    key_ref: SecretName  # tên khoá trong `Device.secrets`, không phải giá trị


class Aaa(StrictModel):
    new_model: bool
    login_lists: dict[ListName, list[AuthMethod]] = Field(default_factory=dict)
    vty_login_list: ListName | None = None
    local_users: list[AaaLocalUser] = Field(default_factory=list)
    tacacs_servers: list[TacacsServer] = Field(default_factory=list)

    @field_validator("login_lists")
    @classmethod
    def _methods_not_empty(cls, value: dict[str, list[AuthMethod]]) -> dict[str, list[AuthMethod]]:
        for name, methods in value.items():
            if not methods:
                raise ValueError(f"login list {name} không có phương thức nào")
            _ensure_unique(list(methods), "phương thức", f"login list {name}")
        return value

    @model_validator(mode="after")
    def _no_duplicates(self) -> Aaa:
        _ensure_unique([u.username for u in self.local_users], "username", "aaa")
        _ensure_unique([s.name for s in self.tacacs_servers], "tên TACACS server", "aaa")
        return self


class Device(StrictModel):
    hostname: Hostname
    platform: Platform
    role: DeviceRole
    site: SiteName
    ip_routing: bool
    default_gateway: IPv4Address | None = None
    interfaces: list[Interface] = Field(default_factory=list)
    vlans: list[Vlan] = Field(default_factory=list)
    static_routes: list[StaticRoute] = Field(default_factory=list)
    acls: dict[AclName, list[AclEntry]] = Field(default_factory=dict)
    aaa: Aaa | None = None
    # UNTRUSTED
    banner_motd: FreeText = ""
    # Giá trị bí mật theo tên. `aaa.local_users[].secret_ref` và `aaa.tacacs_servers[].key_ref` trỏ
    # tới tên ở đây.
    secrets: dict[SecretName, SecretValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_duplicates(self) -> Device:
        _ensure_unique([i.name for i in self.interfaces], "interface", self.hostname)
        _ensure_unique([v.vlan_id for v in self.vlans], "vlan_id", self.hostname)
        _ensure_unique([str(r.prefix) for r in self.static_routes], "route prefix", self.hostname)
        for name, entries in self.acls.items():
            _ensure_unique([e.seq for e in entries], "seq", f"ACL {name} của {self.hostname}")
        return self

    def get_interface(self, name: str) -> Interface | None:
        return next((i for i in self.interfaces if i.name == name), None)


class Link(StrictModel):
    a_device: Hostname
    a_interface: InterfaceName
    b_device: Hostname
    b_interface: InterfaceName


class Policy(StrictModel):
    """Policy tổ chức đi kèm context. Gate (iii) đọc các giới hạn này."""

    reserved_vlans: list[AnyVlanId] = Field(default_factory=lambda: [1, 1002, 1003, 1004, 1005])
    parking_vlan: AnyVlanId | None = None
    protected_interfaces: dict[Hostname, list[InterfaceName]] = Field(default_factory=dict)
    mgmt_prefix: IPv4Network | None = None
    max_devices_per_change: int = Field(default=1, ge=1)
    max_interfaces_per_change: int = Field(default=4, ge=1)


class NetworkContext(StrictModel):
    schema_version: Literal["1.0"]
    context_id: ContextId
    domain_name: DomainName | None = None
    devices: list[Device] = Field(min_length=1)
    links: list[Link] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def _unique_hostnames(self) -> NetworkContext:
        _ensure_unique([d.hostname for d in self.devices], "hostname", self.context_id)
        return self

    def get_device(self, hostname: str) -> Device | None:
        return next((d for d in self.devices if d.hostname == hostname), None)

    def secret_values(self) -> set[str]:
        """Giá trị thật của mọi secret dài từ `MIN_SCANNED_SECRET_LEN` ký tự trở lên, để quét leak.

        Đây là chỗ DUY NHẤT trong contract gọi `get_secret_value()`. Không ghi kết quả ra file, trace hay log.
        """
        return {
            value
            for device in self.devices
            for secret in device.secrets.values()
            if len(value := secret.get_secret_value()) >= MIN_SCANNED_SECRET_LEN
        }


def _ensure_unique(values: list, what: str, scope: str) -> None:
    seen: set = set()
    dups = sorted({str(v) for v in values if v in seen or seen.add(v)})
    if dups:
        raise ValueError(f"{what} bị trùng trong {scope}: {', '.join(dups)}")
