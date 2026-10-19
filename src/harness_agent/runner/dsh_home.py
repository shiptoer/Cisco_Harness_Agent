"""DSH_HOME của một case: quét secret trong những gì dsh tự ghi, rồi dọn dữ liệu phiên (D-019).

dsh là process KHÔNG tin cậy và có thư mục trạng thái riêng. Với dsh 0.2.0-rc.1 (đã xem trên dữ liệu
spike 1.1 và một lượt chạy không có API key):

    <DSH_HOME>/profiles/headless/…                               profile, tự tạo ở lần chạy đầu
    <DSH_HOME>/sessions/--<cwd>--/session-<id>/session.v4.jsonl.zstd   TOÀN BỘ phiên, nén zstd
    <DSH_HOME>/storages/session_projcache/sessions/session-<id>.json   cache theo phiên, JSON thường
    <DSH_HOME>/logs/…                                            báo cáo lỗi khởi động (nếu có)

Session log chứa nguyên tham số và kết quả của mọi tool call. Nếu model đưa secret của context vào một
lời gọi tool thì secret nằm ở đây, kể cả khi harness đã chặn lời gọi đó. File nén zstd, nên tìm theo
byte thô sẽ sót: phải giải nén (trong bộ nhớ) rồi mới tìm.

Runner đặt DSH_HOME riêng cho từng case (`<run-dir>/.dsh_home`), sau mỗi stage gọi `scan` rồi
`clear_sessions`. Kết quả quét chỉ có vị trí (đường dẫn, sha256 của file, loại event), không có nội dung.

Giới hạn: chỉ tìm secret ĐÃ KHAI BÁO trong context, ở dạng nguyên văn hoặc dạng JSON-escape của nó.

Trạng thái: ĐỀ XUẤT (D-019).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

# Tên thư mục DSH_HOME bên trong run-dir của một case.
DSH_HOME_DIR = ".dsh_home"
# Dữ liệu theo phiên mà dsh ghi; bị xoá sau khi quét. `profiles/` được giữ để stage sau dùng lại.
SESSION_DATA = ("sessions", "storages/session_projcache")
# Chặn trên cho một file sau khi giải nén: file do process không tin cậy ghi, không tin kích thước của nó.
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

REDACTED = "[REDACTED]"
# Thư mục con của DSH_HOME chứa session log; dùng để đếm `session_logs_scanned`.
SESSIONS_DIR = "sessions"


@dataclass(frozen=True)
class ScanResult:
    """Kết quả một lần quét. "Không thấy secret" chỉ có nghĩa khi biết đã quét được gì: một thư mục
    thiếu hay rỗng cho `sources == []` giống hệt một thư mục sạch, nên phải đọc cả `status` và số đếm.
    """

    # File có secret, hoặc file không đọc được (`unreadable`).
    sources: list[dict[str, Any]]
    # Số file đã đọc VÀ quét được; file `unreadable` không được tính.
    files_scanned: int
    # Trong đó, số file nằm dưới `sessions/` (session log của dsh).
    session_logs_scanned: int
    # "ok" | "missing" (không phải thư mục) | "empty" (thư mục không có file nào).
    status: str


def scan(home: Path, secrets: Collection[str], stage: str, source: str = "dsh_home") -> ScanResult:
    """Tìm secret trong mọi file dưới `home` (trừ `node_modules`). File `.zstd` được giải nén trong bộ nhớ.

    Mỗi file có secret cho một mục `{"source", "stage", "file", "sha256"}`. `source` mặc định là
    `"dsh_home"`; runner dùng lại hàm này cho thư mục làm việc của dsh với `source="dsh_cwd"`. `sha256`
    là hash của file như nằm trên đĩa. Session log (JSONL) có thêm `events`: loại event của các dòng chứa
    secret, vd `["tool/call"]`. File không đọc hoặc không giải nén được cho một mục có `unreadable` (tên
    lỗi): không biết trong đó có secret hay không, nên không được coi là sạch.
    Không mục nào chứa nội dung file.

    `home` không phải thư mục → `status="missing"`; không có file nào → `"empty"`. Cả hai KHÔNG phải
    là sạch: người gọi quyết định đó có phải lỗ hổng của phép quét hay không.
    """
    found: list[dict[str, Any]] = []
    forms = _forms(secrets)
    if not _long(home).is_dir():
        return ScanResult(sources=[], files_scanned=0, session_logs_scanned=0, status="missing")
    seen = scanned = session_logs = 0
    for path, rel in _files(home):
        seen += 1
        entry: dict[str, Any] = {"source": source, "stage": stage, "file": _safe(rel, secrets)}
        try:
            raw = path.read_bytes()
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            data = _decompress(raw) if path.suffix == ".zstd" else raw
        except (OSError, ValueError, zstandard.ZstdError) as exc:
            entry["unreadable"] = type(exc).__name__
            found.append(entry)
            continue
        scanned += 1
        session_logs += rel.split("/", 1)[0] == SESSIONS_DIR
        lines = [line for line in data.split(b"\n") if any(form in line for form in forms)]
        if not lines:
            continue
        events = _event_types(lines, secrets)
        if events:
            entry["events"] = events
        found.append(entry)
    return ScanResult(
        sources=found,
        files_scanned=scanned,
        session_logs_scanned=session_logs,
        status="ok" if seen else "empty",
    )


def clear_sessions(home: Path) -> bool:
    """Xoá dữ liệu theo phiên dưới `home` (`SESSION_DATA`). Trả về False nếu còn thư mục không xoá được
    (vd một process con của dsh còn giữ file trên Windows)."""
    ok = True
    for name in SESSION_DATA:
        target = _long(home / name)
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
        except OSError:
            ok = False
    return ok


def request_configs(home: Path) -> list[dict[str, str | None]]:
    """Cấu hình model mà dsh THỰC SỰ gửi đi, đọc từ các event `request/header` của session log (D-020).

    Mỗi mục là `{"provider", "model", "reasoning_effort"}`, không lặp, theo thứ tự gặp. NDJSON `--json`
    của dsh không có tên model; chỉ session log có. Session log không đọc được thì bị bỏ qua: không có
    thông tin thì trả về ít mục hơn, không đoán.
    """
    seen: list[dict[str, str | None]] = []
    for header in _request_headers(home):
        config = header.get("config")
        if not isinstance(config, dict):
            continue
        entry = {
            "provider": _text(config.get("provider")),
            "model": _text(config.get("model")),
            "reasoning_effort": _text(config.get("reasoningEffort")),
        }
        if entry not in seen:
            seen.append(entry)
    return seen


def offered_tools(home: Path) -> tuple[int, list[str]]:
    """(số event `request/header` đọc được, tên các tool mà dsh THỰC SỰ cấp cho model trong các request đó).

    Dùng cho D-008 điều kiện 1 ở B0: tool set phải rỗng. Tên không lặp, đã sắp. Người gọi phải coi "0
    header" là KHÔNG xác minh được, không phải là "không có tool" (F9). Header không có khoá `tools` là
    không cấp tool nào; `tools` không phải danh sách, hay một mục không có tên, cho một tên giữ chỗ, để
    không bao giờ đọc thành rỗng. Tên do process không tin cậy ghi: đã cắt độ dài, người gọi tự che secret.
    """
    headers = 0
    names: set[str] = set()
    for header in _request_headers(home):
        headers += 1
        tools = header.get("tools", [])
        if not isinstance(tools, list):
            names.add("<unreadable>")
            continue
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else tool
            names.add(_text(name) or "<unnamed>")
    return headers, sorted(names)


def _request_headers(home: Path) -> Iterator[dict[str, Any]]:
    """`data.header` của mọi event `request/header` trong các session log dưới `home`. Session log hay
    dòng không đọc được thì bị bỏ qua."""
    for path, _ in _files(home):
        if not path.name.endswith(".jsonl.zstd"):
            continue
        try:
            lines = _decompress(path.read_bytes()).split(b"\n")
        except (OSError, ValueError, zstandard.ZstdError):
            continue
        for line in lines:
            if b'"request/header"' not in line:
                continue
            try:
                event = json.loads(line)
                header = event["data"]["header"] if event.get("type") == "request/header" else None
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if isinstance(header, dict):
                yield header


def _text(value: Any) -> str | None:
    """Giá trị lấy từ file của phía không tin cậy: chỉ nhận chuỗi, cắt độ dài."""
    return value[:100] if isinstance(value, str) else None


def read_file(path: Path) -> bytes:
    """Nội dung một file dưới DSH_HOME, đã giải nén nếu là `.zstd`. Dùng cho test và công cụ soát."""
    raw = _long(path).read_bytes()
    return _decompress(raw) if path.suffix == ".zstd" else raw


def _files(home: Path) -> Iterator[tuple[Path, str]]:
    """(đường dẫn, đường dẫn tương đối dạng posix) của mọi file dưới `home`, theo thứ tự cố định."""
    root = _long(home)
    if not root.is_dir():
        return
    pairs = [
        (p, p.relative_to(root).as_posix())
        for p in root.rglob("*")
        if p.is_file() and "node_modules" not in p.parts
    ]
    yield from sorted(pairs, key=lambda pair: pair[1])


def _long(path: Path) -> Path:
    r"""Trên Windows, thêm tiền tố `\\?\` để đọc được đường dẫn dài hơn 260 ký tự: thư mục session của
    dsh mang cả đường dẫn của thư mục làm việc trong tên."""
    if sys.platform != "win32":
        return path
    text = str(path.resolve())
    return path if text.startswith("\\\\") else Path("\\\\?\\" + text)


def _decompress(raw: bytes) -> bytes:
    with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
        data = reader.read(MAX_DECOMPRESSED_BYTES + 1)
    if len(data) > MAX_DECOMPRESSED_BYTES:
        raise ValueError("file giải nén vượt giới hạn")
    return data


def _forms(secrets: Collection[str]) -> list[bytes]:
    """Các dạng byte của secret trong file của dsh: nguyên văn, JSON-escape một lớp (chuỗi trong JSON) và
    hai lớp (tham số tool call là một chuỗi JSON nằm trong một dòng JSON)."""
    forms: set[bytes] = set()
    for secret in secrets:
        once = json.dumps(secret, ensure_ascii=False)[1:-1]
        twice = json.dumps(once, ensure_ascii=False)[1:-1]
        ascii_once = json.dumps(secret)[1:-1]
        forms.update(text.encode("utf-8") for text in (secret, once, twice, ascii_once))
    return sorted(forms, key=len, reverse=True)


def _event_types(lines: list[bytes], secrets: Collection[str]) -> list[str]:
    """Loại event (trường `type`) của các dòng JSON chứa secret, theo thứ tự xuất hiện, không lặp."""
    types: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if isinstance(kind, str) and _safe(kind[:60], secrets) not in types:
            types.append(_safe(kind[:60], secrets))
    return types


def _safe(text: str, secrets: Collection[str]) -> str:
    """Chuỗi lấy từ phía không tin cậy (tên file, loại event): không để nó mang secret vào kết quả."""
    return REDACTED if any(secret in text for secret in secrets) else text
