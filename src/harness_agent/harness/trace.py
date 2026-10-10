"""Trace (control v): JSONL append-only, mỗi dòng nối hash với dòng trước.

Trạng thái: ĐỀ XUẤT (bản tối thiểu). Task 4.2 (TV4) hoàn thiện.
Nhiều process (runner, MCP server stage 1, stage 2) cùng ghi một file, nên hash dòng trước
được đọc lại từ file ngay trước khi ghi.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: str | Path, actor: str):
        self.path = Path(path)
        self.actor = actor
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else "0" * 64

    def write(self, event: str, **data: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "actor": self.actor,
            "event": event,
            "data": data,
            "prev_hash": self._last_hash(),
        }
        body = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        record["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        with self.path.open("ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())


def verify_chain(path: str | Path) -> bool:
    """True nếu chuỗi hash liền mạch (không dòng nào bị sửa, xoá hay chèn)."""
    prev = "0" * 64
    for raw in Path(path).read_bytes().splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        h = rec.pop("hash")
        if rec["prev_hash"] != prev:
            return False
        body = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != h:
            return False
        prev = h
    return True
