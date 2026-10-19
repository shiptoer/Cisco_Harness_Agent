"""Record/replay cho lời gọi LLM (task 1.2).

Khoá = SHA-256 của (model, messages, tham số sinh) đã chuẩn hoá (`sort_keys`), nên cùng request → cùng
file; nội dung file ghi lại cả request lẫn response để kiểm tra bằng mắt. Chế độ:

- `off`:    luôn gọi API, không ghi.
- `record`: luôn gọi API và ghi đè cache.
- `replay`: chỉ đọc cache; thiếu → `CacheMiss` (không bao giờ gọi mạng — dùng cho CI và chạy lặp).
- `auto`:   có cache thì dùng, không thì gọi API rồi ghi (mặc định khi thử nghiệm).

Cache nằm trong `cache/llm/` (đã có trong .gitignore). Không bao giờ ghi API key vào cache.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Mode = Literal["off", "record", "replay", "auto"]
MODES: tuple[str, ...] = ("off", "record", "replay", "auto")


class CacheMiss(LookupError):
    """Chế độ replay nhưng không có bản ghi cho request này."""


def request_key(request: dict[str, Any]) -> str:
    body = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ReplayCache:
    def __init__(self, directory: str | Path, mode: Mode = "auto"):
        if mode not in MODES:
            raise ValueError(f"mode không hợp lệ: {mode!r} (hợp lệ: {', '.join(MODES)})")
        self.dir = Path(directory)
        self.mode = mode

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if self.mode in ("off", "record"):
            return None
        p = self.path(key)
        if not p.exists():
            if self.mode == "replay":
                raise CacheMiss(f"replay: không có bản ghi {key[:12]}… trong {self.dir}")
            return None
        return json.loads(p.read_text(encoding="utf-8"))["response"]

    def put(self, key: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        if self.mode == "off":
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": request,
            "response": response,
        }
        self.path(key).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
