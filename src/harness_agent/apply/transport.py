"""Kênh tới thiết bị cho `apply`: Protocol `Transport` và hiện thực SSH `NetmikoTransport`.

`NetmikoTransport` dùng lại tham số kết nối của collector (`collector.fetch._connect`: Netmiko
`cisco_ios`, host key được kiểm nghiêm ngặt; thuật toán cũ chỉ bật khi inventory có legacy_ssh=true).
Thông tin đăng nhập CHỈ từ biến môi trường `COLLECTOR_USER` / `COLLECTOR_PASSWORD`.
Netmiko được import trễ, trong `connect()`: bộ test và chế độ dry-run không cần gói này (extras `[demo]`).

Tài khoản phải vào thẳng privileged EXEC (privilege 15): transport không gửi `enable` và không đọc enable
secret. Lỗi của thư viện thành `TransportError` chỉ mang tên lớp lỗi: thông báo gốc của Netmiko có thể chép
lại lệnh đã gửi (kể cả giá trị secret đã thay vào), nên không được chép ra.

Trạng thái: ĐỀ XUẤT (D-064). Chưa chạy với Netmiko thật trong bộ test (chỉ `FakeTransport`).
"""

from __future__ import annotations

from typing import Any, Protocol

from harness_agent.collector.fetch import CollectorError, InventoryDevice


class TransportError(Exception):
    """Lỗi kết nối hoặc lỗi khi gửi lệnh. Thông báo không chứa lệnh đã gửi hay mật khẩu."""


class Transport(Protocol):
    def connect(self) -> None: ...
    def send_show(self, cmd: str) -> str: ...
    def send_config(self, lines: list[str]) -> str: ...
    def close(self) -> None: ...


SHOW_RUNNING = "show running-config"
# VTP server/client giữ VLAN trong vlan.dat, không in trong running-config: apply đọc bảng VLAN để biết VLAN
# nào đã có trước khi áp (rollback không bao giờ xoá VLAN vốn có) và kiểm VLAN sau khi áp.
SHOW_VLAN = "show vlan brief"
ALLOWED_SHOWS = (SHOW_RUNNING, SHOW_VLAN)


class NetmikoTransport:
    """SSH tới MỘT thiết bị của inventory. Ở chế độ EXEC chỉ gửi đúng các lệnh của `ALLOWED_SHOWS`; mọi thứ
    khác đi qua `send_config` (chế độ cấu hình của Netmiko, tự thoát bằng `end`)."""

    def __init__(self, device: InventoryDevice):
        self.device = device
        self._conn: Any = None
        self._errors: tuple[type[BaseException], ...] = ()

    def __repr__(self) -> str:
        return f"NetmikoTransport({self.device.hostname!r}, {self.device.host!r})"

    def _fail(self, exc: BaseException) -> TransportError:
        return TransportError(f"{self.device.hostname} {self.device.host}: {type(exc).__name__}")

    def connect(self) -> None:
        from harness_agent.collector.fetch import _connect, _ssh_errors, credentials

        try:
            user, password = credentials()
            self._errors = _ssh_errors()  # import lười netmiko/paramiko; thiếu gói → CollectorError
        except CollectorError as exc:
            raise TransportError(str(exc)) from None
        try:
            self._conn = _connect(self.device, user, password)
            privileged = self._conn.check_enable_mode()
        except (CollectorError, *self._errors) as exc:
            self.close()
            raise self._fail(exc) from None
        if not privileged:
            self.close()
            raise TransportError(
                f"{self.device.hostname}: tài khoản không ở privileged EXEC (privilege 15); "
                "apply không gửi enable"
            )

    def send_show(self, cmd: str) -> str:
        if cmd not in ALLOWED_SHOWS:
            raise TransportError("apply chỉ gửi `show running-config` hoặc `show vlan brief` ở chế độ EXEC")
        if self._conn is None:
            raise TransportError("chưa kết nối")
        try:
            return str(self._conn.send_command(cmd, read_timeout=60))
        except self._errors as exc:
            raise self._fail(exc) from None

    def send_config(self, lines: list[str]) -> str:
        if self._conn is None:
            raise TransportError("chưa kết nối")
        try:
            return str(self._conn.send_config_set(lines, read_timeout=120))
        except self._errors as exc:
            raise self._fail(exc) from None

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.disconnect()
        except self._errors:
            pass  # lỗi khi đóng một kết nối đã hỏng không che lỗi gốc
        finally:
            self._conn = None
