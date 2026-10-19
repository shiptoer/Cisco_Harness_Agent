"""Trace (control v): JSONL append-only, mỗi dòng nối hash với dòng trước.

Trạng thái: ĐỀ XUẤT (bản tối thiểu; khoá HMAC: D-060). Task 4.2 (TV4) hoàn thiện.
Nhiều process (runner, MCP server stage 1, stage 2) cùng ghi một file, nên hash dòng trước
được đọc lại từ file ngay trước khi ghi.

Khoá (D-060): runner sinh một khoá ngẫu nhiên cho mỗi lượt chạy và đưa cho harness qua biến môi trường
`CHA_TRACE_KEY` (hex 64 ký tự). `hash` LUÔN là SHA-256 của thân bản ghi (chuỗi như trước D-060, ai cũng kiểm
được liên kết). Có khoá thì bản ghi có thêm `sig` = HMAC-SHA256(khoá, thân bản ghi kèm `hash`) và
`signed: true`; không khoá thì không có `sig` và `signed: false`. Một process ghi được file mà không có khoá
thì không nối được dòng nào qua được `verify_chain(path, key)`, kể cả khi nó tính đúng SHA-256 và `prev_hash`.

Giới hạn: dsh chạy cùng user hệ điều hành và nhận khoá trong env của nó, nên một dsh giả mạo đọc được khoá.
Khoá chống ghi đè từ process KHÔNG có khoá (process khác, sửa tay sau lượt chạy); control chính chống dsh giả
mạo là runner phát lại gate trước render (`runner.pipeline._replay_gate`).

Bản ghi của người (`reviewer` ở UI, `apply`) ghi SAU lượt chạy, khi khoá của lượt đã mất: chúng không ký, và
chỉ được nằm ở ĐUÔI chuỗi (phần đã ký đứng trước). Chúng được xác thực bằng phiên/token của UI, không bằng
chuỗi.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# - ok: mọi bản ghi của runner/harness có chữ ký đúng với khoá; đuôi không ký (nếu có) chỉ gồm bản ghi của
#   `reviewer`/`apply` và nối đúng SHA-256. Chỉ có khi gọi với khoá.
# - unsigned: chuỗi SHA-256 liền mạch nhưng không xác thực được: gọi không có khoá (`sig` không được kiểm),
#   hoặc trace rỗng.
# - bad_signature: có khoá, chuỗi liền mạch, mà một bản ghi có `sig` sai hay thiếu, bản ghi của runner/harness
#   không ký, hoặc bản ghi đã ký đứng sau đuôi không ký.
# - broken: dòng không đọc được, thiếu trường, `prev_hash` không nối, hay `hash` (SHA-256) sai. Thắng
#   bad_signature.
ChainStatus = Literal["ok", "broken", "unsigned", "bad_signature"]

KEY_ENV = "CHA_TRACE_KEY"
KEY_BYTES = 32
# Actor được ghi KHÔNG ký sau phần đã ký (quyết định trong D-060).
UNSIGNED_ACTORS: frozenset[str] = frozenset({"reviewer", "apply"})
_GENESIS = "0" * 64
_HEX_KEY = re.compile(r"[0-9a-f]{64}")


def key_from_env(environ: Mapping[str, str] | None = None) -> bytes | None:
    """Khoá trace từ `CHA_TRACE_KEY`; None nếu không đặt. Đặt mà sai định dạng là lỗi (ValueError): harness
    không được âm thầm ghi không ký khi runner đã đưa khoá."""
    raw = (os.environ if environ is None else environ).get(KEY_ENV)
    if raw is None or raw == "":
        return None
    if not _HEX_KEY.fullmatch(raw):
        raise ValueError(f"{KEY_ENV} phải là {KEY_BYTES * 2} ký tự hex thường")
    return bytes.fromhex(raw)


def _body(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _hmac(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


class Trace:
    def __init__(self, path: str | Path, actor: str, key: bytes | None = None):
        if key is not None and (not isinstance(key, bytes) or len(key) < KEY_BYTES):
            raise ValueError(f"khoá trace phải có ít nhất {KEY_BYTES} byte")
        self.path = Path(path)
        self.actor = actor
        self._key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # không bao giờ in khoá
        return f"Trace({str(self.path)!r}, actor={self.actor!r}, signed={self._key is not None})"

    def _last_hash(self) -> str:
        if not self.path.exists():
            return _GENESIS
        last = ""
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else _GENESIS

    def write(self, event: str, **data: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "actor": self.actor,
            "event": event,
            "data": data,
            "prev_hash": self._last_hash(),
            "signed": self._key is not None,
        }
        record["hash"] = _sha256(_body(record))
        if self._key is not None:
            record["sig"] = _hmac(self._key, _body(record))  # thân kèm `hash`
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        with self.path.open("ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())


def verify_chain(path: str | Path, key: bytes | None = None) -> ChainStatus:
    """Trạng thái chuỗi hash của trace (xem `ChainStatus`). "ok" và "unsigned" đều là chuỗi liền mạch; chỉ
    "ok" là đã xác thực bằng khoá. File không mở được thì raise OSError như trước.

    Không khoá: chỉ kiểm chuỗi SHA-256 (`prev_hash`, `hash`); liền mạch là "unsigned" kể cả khi bản ghi có
    `sig`. Có khoá: chuỗi phải liền mạch ("broken" thắng), VÀ mọi bản ghi của runner/harness có `sig` đúng;
    đuôi không ký chỉ gồm `reviewer`/`apply`."""
    prev = _GENESIS
    verified = 0  # số bản ghi đã ký có chữ ký đúng
    in_tail = False  # đã gặp bản ghi không ký của reviewer/apply
    bad_sig = False
    for raw in Path(path).read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            return "broken"
        if not isinstance(rec, dict) or not isinstance(rec.get("hash"), str):
            return "broken"
        sig = rec.pop("sig", None)
        h = rec.pop("hash")
        # Trace cũ (trước D-060) không có `signed`: coi là không ký.
        signed = rec.get("signed", False)
        if type(signed) is not bool or rec.get("prev_hash") != prev:
            return "broken"
        if not hmac.compare_digest(_sha256(_body(rec)), h):
            return "broken"
        prev = h
        if key is None or bad_sig:
            continue
        if signed:
            rec["hash"] = h
            ok = isinstance(sig, str) and hmac.compare_digest(_hmac(key, _body(rec)), sig)
            if in_tail or not ok:
                bad_sig = True
            else:
                verified += 1
        elif sig is not None or rec.get("actor") not in UNSIGNED_ACTORS:
            bad_sig = True
        else:
            in_tail = True
    if bad_sig:
        return "bad_signature"
    return "ok" if key is not None and verified > 0 else "unsigned"
