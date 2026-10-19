"""Lấy output `show` từ thiết bị của một lab: qua SSH (Netmiko, `cisco_ios`) hoặc từ thư mục đã lưu.

Collector CHỈ ĐỌC. Ba giới hạn cứng, đều kiểm ở đây:
- Lệnh: chỉ các lệnh trong `ALLOWED_COMMANDS`, so khớp NGUYÊN VĂN. Mọi chuỗi khác bị từ chối trước khi tới
  thiết bị; không có chế độ cấu hình, không có `enable`.
- Host: chỉ thiết bị có trong file inventory.
- Thông tin đăng nhập: chỉ từ biến môi trường `COLLECTOR_USER`, `COLLECTOR_PASSWORD`. Không đọc từ file,
  không nhận qua tham số dòng lệnh, không ghi vào log.

Netmiko được import TRỄ (trong hàm): chế độ offline và bộ test không cần gói này (extras `[demo]`).

Trạng thái: ĐỀ XUẤT (D-047).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .parse import sanitize

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"  # mốc của một lượt thu thập, trong tên file context và thư mục raw
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

ALLOWED_COMMANDS: tuple[str, ...] = (
    "show running-config",
    "show vlan brief",
    "show ip interface brief",
    "show cdp neighbors detail",
    "show interfaces trunk",
)

USER_ENV = "COLLECTOR_USER"
PASSWORD_ENV = "COLLECTOR_PASSWORD"  # noqa: S105 — tên biến môi trường, không phải mật khẩu

# Image Cisco IOL của lab smb-01 (15.2 / 15.4, bản phát triển) chỉ hỗ trợ ba thuật toán SSH đã cũ này (đã
# thử khi dựng lab, `lab/smb-01/baseline-configs/README-smb-01.md` mục 10, L7). Cả ba dựa trên SHA-1 và
# không nên dùng ngoài một lab cô lập.
#
# Chúng được bật CHO TỪNG KẾT NỐI tới host trong inventory (`_lab_transport`), bằng tuỳ chọn bảo mật của
# đúng transport đó. Mặc định toàn cục của paramiko, và file `~/.ssh/config`, không bị sửa.
# paramiko 5 đã GỠ HẲN `diffie-hellman-group14-sha1` và `ssh-rsa`; vì vậy extras `[demo]` ghim
# `paramiko==4.0.0`, và `_lab_transport` báo lỗi rõ ràng nếu bản đang cài không còn các thuật toán này.
LEGACY_KEX = "diffie-hellman-group14-sha1"
LEGACY_HOST_KEY = "ssh-rsa"
LEGACY_MAC = "hmac-sha1"


class CollectorError(Exception):
    """Lỗi cấu hình hoặc lỗi khi lấy dữ liệu. Thông báo không bao giờ chứa mật khẩu."""


@dataclass(frozen=True)
class InventoryDevice:
    hostname: str
    host: str  # địa chỉ quản lý
    role: str
    site: str
    platform: str
    mgmt_interface: str


@dataclass(frozen=True)
class Inventory:
    context_id: str
    domain_name: str | None
    devices: tuple[InventoryDevice, ...]

    def device(self, hostname: str) -> InventoryDevice:
        for device in self.devices:
            if device.hostname == hostname:
                return device
        raise CollectorError(f"thiết bị không có trong inventory: {hostname[:60]!r}")


def load_inventory(path: Path) -> Inventory:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        devices = tuple(
            InventoryDevice(
                hostname=str(d["hostname"]),
                host=str(ipaddress.IPv4Address(d["host"])),
                role=str(d["role"]),
                site=str(d["site"]),
                platform=str(d["platform"]),
                mgmt_interface=str(d["mgmt_interface"]),
            )
            for d in data["devices"]
        )
        inventory = Inventory(str(data["context_id"]), data.get("domain_name"), devices)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CollectorError(f"inventory {path.name} không dùng được: {type(exc).__name__}: {exc}") from exc
    names = [d.hostname for d in inventory.devices]
    if not names or len(set(names)) != len(names) or len({d.host for d in inventory.devices}) != len(names):
        raise CollectorError(f"inventory {path.name}: rỗng, hoặc hostname / host bị trùng")
    return inventory


def check_command(command: str) -> str:
    """Trả về `command` nếu nó nằm trong allowlist; nếu không thì raise. So khớp nguyên văn: không cắt
    khoảng trắng, không nhận viết tắt, không nhận `|`, `;` hay xuống dòng."""
    if command not in ALLOWED_COMMANDS:
        raise CollectorError(f"lệnh ngoài allowlist, bị từ chối: {command[:60]!r}")
    return command


def command_file(command: str) -> str:
    """Tên file lưu output của một lệnh trong allowlist: `show vlan brief` → `show_vlan_brief.txt`."""
    return re.sub(r"[^a-z]+", "_", check_command(command)) + ".txt"


# ------------------------------------------------------------------ offline
def read_saved(directory: Path, inventory: Inventory) -> dict[str, dict[str, str]]:
    """Output đã lưu: `<directory>/<hostname>/<lệnh>.txt`. Chỉ đọc thiết bị trong inventory và lệnh trong
    allowlist; `show running-config` là bắt buộc, các lệnh khác thiếu thì bỏ qua."""
    outputs: dict[str, dict[str, str]] = {}
    for device in inventory.devices:
        folder = directory / device.hostname
        found: dict[str, str] = {}
        for command in ALLOWED_COMMANDS:
            path = folder / command_file(command)
            if path.is_file():
                found[command] = path.read_text(encoding="utf-8", errors="replace")
        if "show running-config" not in found:
            raise CollectorError(
                f"thiếu output `show running-config` của {device.hostname} trong {directory}"
            )
        outputs[device.hostname] = found
    return outputs


def new_raw_dir(root: Path, context_id: str, when: datetime) -> Path:
    """Tạo thư mục raw của MỘT lượt: `<root>/<context_id>-raw/<mốc UTC>/`, cùng mốc với file context của lượt
    đó. Thư mục đã có thì lỗi, không ghi đè: bản đầu ghi mọi lượt vào một thư mục và raw của hai lượt thật
    đã mất (evidence-log 10.19)."""
    if not _SAFE_NAME.fullmatch(context_id):
        raise CollectorError(f"context_id không dùng làm tên thư mục được: {context_id[:60]!r}")
    directory = root / f"{context_id}-raw" / when.astimezone(UTC).strftime(STAMP_FORMAT)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise CollectorError(f"thư mục raw đã có, không ghi đè: {directory.as_posix()}") from None
    return directory


def save_outputs(directory: Path, outputs: dict[str, dict[str, str]]) -> int:
    """Lưu output để chạy offline sau. `show running-config` được XOÁ giá trị bí mật trước khi ghi
    (`parse.sanitize`). Trả về số dòng đã xoá."""
    removed = 0
    for hostname, by_command in outputs.items():
        folder = directory / hostname
        folder.mkdir(parents=True, exist_ok=True)
        for command, text in by_command.items():
            if command == "show running-config":
                text, count = sanitize(text)
                removed += count
            (folder / command_file(command)).write_text(text, encoding="utf-8", newline="\n")
    return removed


# ------------------------------------------------------------------ SSH
def credentials() -> tuple[str, str]:
    """(user, password) từ biến môi trường. Thiếu một trong hai → lỗi, không hỏi lại và không có mặc định."""
    user, password = os.environ.get(USER_ENV), os.environ.get(PASSWORD_ENV)
    if not user or not password:
        raise CollectorError(f"cần đặt cả hai biến môi trường {USER_ENV} và {PASSWORD_ENV}")
    return user, password


def _lab_transport(sock: Any, **kwargs: Any) -> Any:
    """`transport_factory` cho paramiko: một transport có thêm ba thuật toán cũ của image IOL. Chỉ đổi tuỳ
    chọn của CHÍNH transport này; mặc định của lớp `paramiko.Transport` không bị đụng tới."""
    import paramiko

    transport = paramiko.Transport(sock, **kwargs)
    options = transport.get_security_options()
    try:
        options.kex = (*(k for k in options.kex if k != LEGACY_KEX), LEGACY_KEX)
        options.key_types = (*(k for k in options.key_types if k != LEGACY_HOST_KEY), LEGACY_HOST_KEY)
        options.digests = (*(d for d in options.digests if d != LEGACY_MAC), LEGACY_MAC)
    except ValueError as exc:
        transport.close()
        raise CollectorError(
            f"paramiko {paramiko.__version__} không còn hỗ trợ {LEGACY_KEX} / {LEGACY_HOST_KEY} / "
            f"{LEGACY_MAC} "
            "mà image IOL của lab cần; cài đúng phiên bản của extras [demo] (paramiko==4.0.0)"
        ) from exc
    return transport


def _ssh_errors() -> tuple[type[BaseException], ...]:
    """Mọi lỗi mà một kết nối SSH có thể ném ra. `NetmikoAuthenticationException` và `NetmikoTimeoutException`
    KHÔNG kế thừa `NetmikoBaseException` mà kế thừa `paramiko.SSHException`: phải bắt cả hai gốc, nếu không
    sai mật khẩu sẽ thành traceback thay vì một dòng lỗi."""
    try:
        import paramiko
        from netmiko.exceptions import NetmikoBaseException
    except ImportError as exc:
        raise CollectorError('thiếu Netmiko: cài bằng `pip install -e ".[demo]"`') from exc
    return (NetmikoBaseException, paramiko.SSHException, OSError, EOFError)


def _connect(device: InventoryDevice, user: str, password: str) -> Any:
    """Mở một kết nối Netmiko tới MỘT thiết bị của inventory."""
    from netmiko.cisco import CiscoIosSSH

    class LabIosSSH(CiscoIosSSH):
        """Kết nối tới thiết bị của lab: thêm `transport_factory` vào tham số gửi cho paramiko."""

        def _connect_params_dict(self) -> dict[str, Any]:
            params = super()._connect_params_dict()
            params["transport_factory"] = _lab_transport
            return params

    return LabIosSSH(
        host=device.host,
        username=user,
        password=password,
        device_type="cisco_ios",
        use_keys=False,
        allow_agent=False,
        # Lab cô lập, thiết bị dựng lại sau mỗi lần Wipe nên host key đổi: không kiểm host key.
        ssh_strict=False,
        system_host_keys=False,
        conn_timeout=15,
        session_log=None,
    )


def fetch_ssh(
    inventory: Inventory, commands: tuple[str, ...] = ALLOWED_COMMANDS
) -> dict[str, dict[str, str]]:
    """Chạy các lệnh `show` trên mọi thiết bị của inventory qua SSH. Mỗi lệnh đi qua `check_command` ngay
    trước khi gửi. Không vào `enable`, không vào chế độ cấu hình, không ghi session log.

    Mọi lỗi kết nối (sai mật khẩu, quá giờ, bắt tay SSH thất bại, mất kết nối giữa chừng) thành
    `CollectorError` với thông báo MỘT dòng `<hostname> <host>: <tên loại lỗi>`. Thông báo gốc của thư viện
    không được chép lại, và exception gốc không được nối vào (`from None`): không có traceback, không có gì
    ngoài tên thiết bị, địa chỉ và tên lớp lỗi đi ra màn hình hay log."""
    for command in commands:
        check_command(command)
    user, password = credentials()
    errors = _ssh_errors()
    outputs: dict[str, dict[str, str]] = {}
    for device in inventory.devices:
        try:
            connection = _connect(device, user, password)
        except errors as exc:
            raise CollectorError(f"{device.hostname} {device.host}: {type(exc).__name__}") from None
        try:
            found: dict[str, str] = {}
            for command in commands:
                found[command] = connection.send_command(check_command(command), read_timeout=30)
            outputs[device.hostname] = found
        except errors as exc:
            raise CollectorError(f"{device.hostname} {device.host}: {type(exc).__name__}") from None
        finally:
            try:
                connection.disconnect()
            except errors:
                pass  # lỗi khi đóng một kết nối đã hỏng không che lỗi gốc
    return outputs
