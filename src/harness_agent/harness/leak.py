"""Phát hiện và che secret của context trong dữ liệu đi qua phía tin cậy (D-018).

Một cơ chế dùng chung cho hai nơi:
- harness: chặn lời gọi tool có tên hoặc tham số chứa secret (`secret_paths`);
- runner: tìm secret trong output thô của dsh, ghi nhận vị trí, rồi che trước khi lưu (`redact_stream`),
  và quét các file trong run-dir (`secret_lines`).

Mọi hàm chỉ trả về VỊ TRÍ (tên trường, số dòng, hash của dòng gốc), không bao giờ trả về giá trị secret.

Giới hạn: chỉ phát hiện secret ĐÃ KHAI BÁO trong context, ở dạng nguyên văn (và dạng JSON-escape của
nó trong luồng byte). Secret bị biến đổi (base64, cắt làm nhiều phần, đảo chữ) không bị phát hiện.

Trạng thái: ĐỀ XUẤT (D-018).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from typing import Any

REDACTED = "[REDACTED]"
# Thay cho một khoá (tên trường) mà chính nó chứa secret: không ghi được tên khoá ra đường dẫn.
SECRET_KEY = "<key>"  # noqa: S105 — nhãn trong đường dẫn, không phải mật khẩu


def secret_paths(value: Any, secrets: Collection[str], root: str = "") -> list[str]:
    """Đường dẫn của mọi chuỗi trong `value` (khoá hoặc giá trị, đệ quy) có chứa một secret.

    Ví dụ `["args.hostname", "args.changes[0].args.name", "args.<key>"]`. Khoá chứa secret được ghi
    là `<key>`. Đường dẫn không bao giờ chứa giá trị secret.
    """
    if not secrets:
        return []
    hits: list[str] = []
    _walk(value, secrets, root, hits)
    # Phòng khi tên các khoá ghép lại trùng một secret: không để đường dẫn tự nó thành chỗ rò rỉ.
    return [REDACTED if _has_secret(path, secrets) else path for path in hits]


def redact_value(value: Any, secrets: Collection[str]) -> Any:
    """Bản sao của `value` với mọi secret trong chuỗi (khoá lẫn giá trị) được thay bằng `[REDACTED]`."""
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, dict):
        return {
            (_redact_text(k, secrets) if isinstance(k, str) else k): redact_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v, secrets) for v in value]
    return value


def redact_stream(raw: bytes, secrets: Collection[str]) -> tuple[bytes, list[dict[str, Any]]]:
    """Tìm secret trong một luồng byte theo từng dòng, rồi che. Trả về (luồng đã che, danh sách vị trí).

    Mỗi vị trí là `{"line": n, "sha256": ...}`; dòng là JSON thì có thêm `event` (trường `type` của dòng)
    và `fields` (đường dẫn trường chứa secret). `sha256` là hash của dòng GỐC, trước khi che, không gồm
    ký tự kết thúc dòng (LF hoặc CRLF): để đối chiếu với bản gốc nếu có, không khôi phục được dòng.
    Dòng JSON được che theo cấu trúc rồi ghi lại, nên bắt được cả secret bị escape theo kiểu bất kỳ; dòng
    không phải JSON được che theo byte. Dòng không có secret giữ nguyên byte.
    """
    if not secrets or not raw:
        return raw, []
    forms = _byte_forms(secrets)
    out: list[bytes] = []
    found: list[dict[str, Any]] = []
    for number, original in enumerate(raw.split(b"\n"), 1):
        line = original
        where: dict[str, Any] | None = None
        try:
            obj = json.loads(line.decode("utf-8-sig"))
        except ValueError:  # không phải UTF-8 hoặc không phải JSON
            obj = None
        fields = secret_paths(obj, secrets) if isinstance(obj, dict | list) else []
        if fields:
            obj = redact_value(obj, secrets)
            where = {"line": number, "sha256": _line_hash(original)}
            kind = obj.get("type") if isinstance(obj, dict) else None
            if isinstance(kind, str):
                where["event"] = kind[:40]
            where["fields"] = fields
            ending = b"\r" if line.endswith(b"\r") else b""
            line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + ending
        if any(form in line for form in forms):
            for form in forms:
                line = line.replace(form, REDACTED.encode("ascii"))
            where = where or {"line": number, "sha256": _line_hash(original)}
        if where:
            found.append(where)
        out.append(line)
    return b"\n".join(out), found


def _line_hash(line: bytes) -> str:
    return hashlib.sha256(line.removesuffix(b"\r")).hexdigest()


def secret_lines(raw: bytes, secrets: Collection[str]) -> list[int]:
    """Số thứ tự các dòng (từ 1) của `raw` có chứa một secret."""
    if not secrets or not raw:
        return []
    forms = _byte_forms(secrets)
    return [n for n, line in enumerate(raw.split(b"\n"), 1) if any(form in line for form in forms)]


def _walk(value: Any, secrets: Collection[str], at: str, hits: list[str]) -> None:
    if isinstance(value, str):
        if _has_secret(value, secrets):
            hits.append(at or "<root>")
    elif isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if isinstance(key, str) and _has_secret(key, secrets):
                name = SECRET_KEY
                hits.append(_join(at, name))
            _walk(item, secrets, _join(at, name), hits)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _walk(item, secrets, f"{at}[{index}]", hits)


def _join(at: str, name: str) -> str:
    return f"{at}.{name}" if at else name


def _has_secret(text: str, secrets: Collection[str]) -> bool:
    return any(s in text for s in secrets)


def _redact_text(text: str, secrets: Collection[str]) -> str:
    # Secret dài trước: nếu một secret là chuỗi con của secret khác thì không để sót phần đuôi.
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def _byte_forms(secrets: Collection[str]) -> list[bytes]:
    """Các dạng byte của secret trong một luồng: nguyên văn và hai dạng JSON-escape. Dài trước."""
    forms: set[bytes] = set()
    for secret in secrets:
        for text in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
            forms.add(text.encode("utf-8"))
    return sorted(forms, key=len, reverse=True)
