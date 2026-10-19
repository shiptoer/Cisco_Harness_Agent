"""B0: baseline của ablation (N-07, D-044). Một LLM nhận intent + context THÔ và sinh CLI trực tiếp,
không tool, không harness, qua cùng dsh (D-008).

Module này chỉ có phần thuần của B0 (điều kiện, parse câu trả lời); `runner/pipeline.py` điều phối.

Ở B0 câu trả lời của model CHÍNH LÀ thứ người duyệt nhận, nên runner buộc phải đọc text của model. Nó đọc
theo một văn phạm chặt và không đoán (F12): lệch văn phạm là ERROR `AGENT_INCOMPLETE`, không bao giờ
"cứu" lấy phần trông giống cấu hình.

Trạng thái: ĐỀ XUẤT (D-044).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_agent.harness.roles import CLI_ROLE_LEVEL, role_level
from harness_agent.models.context import MIN_SCANNED_SECRET_LEN
from harness_agent.tools.registry.tools import TOOL_REGISTRY

# Tên stage duy nhất của B0: tiền tố của các file trong run-dir (`b0.ndjson`, `b0.patch.yml`...).
STAGE = "b0"
# Câu hỏi làm rõ hoặc lời từ chối của model, nguyên văn: ở B0 đây là thứ người vận hành nhận, nên nó được
# coi là package khi quét secret (không che, D-018).
RESPONSE_FILE = "b0_response.md"


MAX_RESPONSE_BYTES = 256 * 1024

_FENCE = "```"
# Hostname thành tên file `candidate/<hostname>.cfg`: không dấu chấm, không dấu phân cách đường dẫn.
_HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
# Tên thiết bị của Windows: `CON.cfg` vẫn là thiết bị, không phải file.
_PORTS = [f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10)]
_RESERVED = frozenset(["con", "prn", "aux", "nul", *_PORTS])
_KINDS = {"cisco": "config", "clarify": "clarify", "refuse": "refuse"}


class B0ParseError(Exception):
    """Câu trả lời không theo văn phạm. `kind` là mã máy đọc được; không mang text của model."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class B0Response:
    kind: str  # "config" | "clarify" | "refuse"
    # kind == "config": hostname → CLI của thiết bị đó, theo thứ tự model viết.
    configs: dict[str, str] = field(default_factory=dict)
    # kind khác: câu hỏi làm rõ, hoặc lý do từ chối.
    text: str = ""


def parse_response(text: str) -> B0Response:
    """Đọc câu trả lời cuối của model theo văn phạm của B0; lệch thì raise `B0ParseError`.

        response := config+ | clarify | refuse       (giữa và quanh các khối chỉ có dòng trắng)
        config   := "```cisco <hostname>" NL dòng... NL "```"
        clarify  := "```clarify" NL dòng... NL "```"
        refuse   := "```refuse"  NL dòng... NL "```"

    Hostname không được đối chiếu với context: đó là kiểm tra domain, thuộc control (i), B0 không có.
    """
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise B0ParseError("too_large")
    blocks: list[tuple[str, str, str]] = []  # (loại, hostname, thân)
    opened: tuple[str, str] | None = None
    body: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if opened is None:
            if not line.strip():
                continue
            if not line.startswith(_FENCE):
                raise B0ParseError("text_outside")
            opened = _open(line)
            body = []
        elif line.rstrip() == _FENCE:
            if not any(x.strip() for x in body):
                raise B0ParseError("empty_block")
            blocks.append((*opened, "\n".join(body) + "\n"))
            opened = None
        elif line.startswith(_FENCE):
            raise B0ParseError("bad_fence")  # khối lồng nhau, hoặc khối trước chưa đóng
        else:
            body.append(line)
    if opened is not None:
        raise B0ParseError("unclosed")
    if not blocks:
        raise B0ParseError("no_block")
    kinds = {kind for kind, _, _ in blocks}
    if len(kinds) > 1 or (kinds != {"config"} and len(blocks) > 1):
        raise B0ParseError("mixed_blocks")
    kind = blocks[0][0]
    if kind != "config":
        return B0Response(kind=kind, text=blocks[0][2])
    configs: dict[str, str] = {}
    seen: set[str] = set()
    for _, hostname, cli in blocks:
        # So không phân biệt hoa thường: `R1.cfg` và `r1.cfg` là một file trên Windows.
        if hostname.casefold() in seen:
            raise B0ParseError("duplicate_host")
        seen.add(hostname.casefold())
        configs[hostname] = cli
    return B0Response(kind="config", configs=configs)


def _open(line: str) -> tuple[str, str]:
    """Dòng mở khối → (loại, hostname). Hostname rỗng với `clarify` và `refuse`."""
    label, _, argument = line.rstrip()[len(_FENCE) :].partition(" ")
    if label not in _KINDS:
        raise B0ParseError("bad_fence")
    if label != "cisco":
        if argument:
            raise B0ParseError("bad_fence")
        return _KINDS[label], ""
    if not _HOSTNAME.fullmatch(argument) or argument.casefold() in _RESERVED:
        raise B0ParseError("bad_hostname")
    return "config", argument


class B0ContextError(Exception):
    """File context không đọc được hoặc không phải JSON: không dựng được prompt, không biết secret nào để
    quét. `kind` là mã máy đọc được."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


_BOM = b"\xef\xbb\xbf"


def read_context(path: Path) -> tuple[str, set[str]]:
    """(nguyên văn file context, các giá trị secret trong đó). Chỉ bỏ BOM; không validate, không lọc.

    B0 không có control (i): context sai contract hay sai invariant vẫn được gửi cho model nguyên văn. Chỉ
    file không đọc được hoặc không phải JSON mới là lỗi (`B0ContextError`).
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise B0ContextError("unreadable") from exc
    return read_context_bytes(raw)


def read_context_bytes(raw: bytes | None) -> tuple[str, set[str]]:
    """Như `read_context`, trên đúng bộ byte runner đã ràng buộc cho lượt chạy (D-054). None = file không đọc
    được. Context đi vào prompt NGUYÊN VĂN, kể cả khối `provenance` (D-054 giữ B0 "nguyên văn")."""
    if raw is None:
        raise B0ContextError("unreadable")
    try:
        text = raw.removeprefix(_BOM).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise B0ContextError("not_utf8") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise B0ContextError("not_json") from exc
    return text, raw_secret_values(data)


def raw_secret_values(data: Any) -> set[str]:
    """Giá trị secret trong một context dạng dict THÔ, để quét leak. Cùng quy tắc với
    `ContextStore.secret_values` (mọi giá trị của `devices[].secrets` dài từ 8 ký tự, và `secret_hash`
    của local user), nhưng không đòi context hợp lệ: phần nào sai kiểu thì bỏ qua phần đó.

    Đây là dụng cụ đo, không phải control: nó không chặn gì. Không ghi kết quả ra file, trace hay log.
    """
    found: set[str] = set()
    devices = data.get("devices") if isinstance(data, dict) else None
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, dict):
            continue
        secrets = device.get("secrets")
        for value in secrets.values() if isinstance(secrets, dict) else []:
            if isinstance(value, str) and len(value) >= MIN_SCANNED_SECRET_LEN:
                found.add(value)
        aaa = device.get("aaa")
        for holder in (device, aaa if isinstance(aaa, dict) else {}):
            users = holder.get("local_users")
            for user in users if isinstance(users, list) else []:
                if isinstance(user, dict) and isinstance(user.get("secret_hash"), str):
                    found.add(user["secret_hash"])
    return found


_SLOT = re.compile(r"\{\{(PERMISSIONS|ROLE|CONTEXT|INTENT)\}\}")


def permissions_table() -> str:
    """Bảng quyền đưa vào prompt B0, SINH từ tool registry và bảng role (D-010), không viết tay: mỗi loại
    thay đổi một dòng, kèm role thấp nhất được làm. Giống nhau ở mọi case.

    Ở B1–B4 role không bao giờ vào prompt, gate thực thi bảng này. B0 không có gate: cùng chính sách được
    đặt vào prompt, để phép so B0 với B3/B4 là "chính sách trong prompt" so với "chính sách trong gate".
    """
    by_level = sorted(CLI_ROLE_LEVEL.items(), key=lambda item: item[1])
    name_of = {level: name for name, level in by_level}
    lines = [
        f"- {spec.name} (lowest role: {name_of[spec.min_role]}): {_first_sentence(spec.description)}"
        for spec in TOOL_REGISTRY.values()
    ]
    order = " < ".join(name for name, _ in by_level)
    lines.append(
        f"Roles from lowest to highest: {order}. A role may make every change allowed to a lower role."
    )
    return "\n".join(lines)


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".") + "."


def build_prompt(template: str, intent: str, role: str, context_text: str) -> str:
    """Điền prompt B0. Thay MỘT lượt: nội dung context hay intent có chứa `{{INTENT}}` cũng không bị
    thay tiếp. Role lạ thì raise ValueError (không có mức mặc định, như `harness/roles.py`)."""
    role_level(role)
    values = {"PERMISSIONS": permissions_table(), "ROLE": role, "CONTEXT": context_text, "INTENT": intent}
    missing = [name for name in values if "{{" + name + "}}" not in template]
    if missing:
        raise ValueError(f"prompt B0 thiếu chỗ điền: {', '.join(missing)}")
    return _SLOT.sub(lambda match: values[match.group(1)], template)


def render_package(intent: str, role: str, configs: dict[str, str], context_lines: Sequence[str] = ()) -> str:
    """`change_package.md` của B0: CLI do model sinh, nguyên văn, cho người duyệt. `context_lines`: snapshot
    của context mà lượt chạy đã ràng buộc (D-054)."""
    lines = [
        "# Change package — CHỜ NGƯỜI DUYỆT",
        "",
        "Hệ thống KHÔNG tự deploy. Người duyệt kiểm tra rồi mới áp dụng thủ công.",
        "",
        "Điều kiện: B0 — CLI do model sinh trực tiếp, CHƯA qua control nào (không gate, không sensor).",
        "",
        f"- Intent: {intent}",
        f"- Role: {role}",
        f"- Số thiết bị: {len(configs)}",
        *context_lines,
        "",
    ]
    for device, text in configs.items():
        lines += [f"## {device}", "", "```text", text.rstrip("\n"), "```", ""]
    return "\n".join(lines)
