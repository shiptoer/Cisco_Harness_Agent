"""Kiểm một file cấu hình Cisco IOS bất kỳ với network context, không có LLM (`cha-check`).

    python -m harness_agent.check candidate/SW1.cfg --context data/contexts/campus-small-01.json
    cha-check SW1.cfg R1.cfg --context ctx.json --role network_admin --json
    cha-check running.txt --device SW1 --context ctx.json

Tên file là `<device>.cfg` (device = tên file bỏ đuôi), hoặc chỉ định bằng `--device` khi có đúng một file.
Mọi file của một lần gọi được kiểm như MỘT change (giới hạn số device/interface của gate tính chung).

Mã thoát: 0 = không có finding (ngoài mức info), 1 = có finding, 2 = lỗi dùng (file không đọc được, context
sai contract, role lạ, device không có trong context). Secret của context không bao giờ được in ra.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from harness_agent.harness.roles import ROLE_NAMES

from . import DEFAULT_ROLE, CheckError, check_files

COLUMNS = ("device", "line", "kind", "code", "message")
# Ký tự điều khiển (C0 trừ tab/LF, DEL, C1 gồm CSI 0x9b). ESC (0x1b) nằm trong dải C0 nên mọi chuỗi ESC (ANSI)
# mất tác dụng khi ESC bị thay. Text cấu hình là untrusted: không để nó điều khiển terminal của người đọc.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# JSON chỉ bắt buộc escape C0. `json_text` escape thêm DEL, C1 và U+2028/2029 thành dạng u+4 chữ số hex (vẫn
# là JSON hợp lệ, giá trị đọc lại không đổi), giữ tiếng Việt đọc được (`ensure_ascii=False`).
_JSON_UNSAFE = re.compile(r"[\x7f-\x9f\u2028\u2029]")


class _Parser(argparse.ArgumentParser):
    """argparse thoát mã 2 khi lỗi tham số; giữ mã đó nhưng trả về thay vì `sys.exit` để `main` gọi được
    trong test."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CheckError(message)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = _Parser(prog="cha-check", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("configs", nargs="+", metavar="cfg", help="file cấu hình, tên <device>.cfg")
    ap.add_argument("--context", required=True, help="file network context JSON")
    ap.add_argument(
        "--role",
        default=DEFAULT_ROLE,
        help=f"role phát lại qua gate (mặc định {DEFAULT_ROLE}; hợp lệ: {', '.join(ROLE_NAMES)})",
    )
    ap.add_argument("--device", help="tên device cho file duy nhất (thay cho tên file)")
    ap.add_argument("--json", action="store_true", help='in {"devices": {...}, "exit_code": n}')
    try:
        a = ap.parse_args(argv)
        result = check_files(a.configs, a.context, role=a.role, device=a.device)
    except CheckError as exc:
        print(f"[LỖI] {safe_text(str(exc))}", file=sys.stderr)
        return 2
    if a.json:
        print(json_text(result))
    else:
        print(render_text(result))
    return int(result["exit_code"])


def json_text(result: dict[str, Any]) -> str:
    """`--json`: ký tự DEL/C1/U+2028/2029 chỉ có thể nằm trong chuỗi JSON, nên thay chúng bằng escape unicode
    của JSON là an toàn về cú pháp."""
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def render_text(result: dict[str, Any]) -> str:
    """Một bảng cho mỗi device: `device | line | kind | code | message`."""
    out: list[str] = []
    for device, entry in result["devices"].items():
        out.append(f"== {safe_text(device)} ({safe_text(entry['file'])}): {len(entry['findings'])} finding")
        rows = [
            tuple(
                safe_text(v)
                for v in (
                    device,
                    "-" if f["line"] is None else str(f["line"]),
                    f["kind"],
                    f["code"],
                    f["message"],
                )
            )
            for f in entry["findings"]
        ]
        if not rows:
            out.append("   không có finding")
            out.append("")
            continue
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(COLUMNS[:-1])]
        out.append(_row(COLUMNS, widths))
        out.append("-+-".join([*("-" * w for w in widths), "-" * len(COLUMNS[-1])]))
        out += [_row(r, widths) for r in rows]
        out.append("")
    sensors = ", ".join(f"{k}={v}" for k, v in result.get("sensors", {}).items())
    out.append(f"sensors: {sensors}")
    out.append(f"exit_code: {result['exit_code']}")
    return "\n".join(out)


def safe_text(text: str) -> str:
    """Thay mỗi ký tự điều khiển bằng dạng `\\xNN` nhìn thấy được (BEL → `\\x07`, ESC → `\\x1b`)."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _row(cells: tuple[str, ...], widths: list[int]) -> str:
    """Căn trái các cột trừ cột cuối (message, không cắt)."""
    return " | ".join([*(c.ljust(w) for c, w in zip(cells[:-1], widths, strict=True)), cells[-1]])


if __name__ == "__main__":
    raise SystemExit(main())
