"""Token và số request của một stage, đọc từ output NDJSON của dsh (D-021).

dsh 0.2.0-rc.1 phát một event `status` có `phase = "step_end"` cho mỗi bước của agent (mỗi bước là một
request tới model), kèm `usage` (thấy ở 9 log của spike 1.1 và ở lượt chạy thật đầu tiên, 01/10/2026):

    {"type":"status","phase":"step_end","turn":1,"step":2,
     "usage":{"inputTokens":627,"outputTokens":344,"cacheReadTokens":1664,"cacheWriteTokens":0,"totalTokens":2635}}

`inputTokens` là phần input KHÔNG lấy từ cache (file cache của dsh gọi nó là `uncachedInputTokens`).

Nguyên tắc: chỉ chép lại những gì dsh báo. Trường nào dsh không cung cấp thì là None, không đoán và không
ước tính. dsh không báo riêng token reasoning, nên `reasoning` luôn là None ở bản này.
"""

from __future__ import annotations

import json
from typing import Any

# Tên trường trong result.json → khoá trong `usage` của dsh.
_FIELDS = {
    "input_uncached": "inputTokens",
    "input_cached": "cacheReadTokens",
    "cache_write": "cacheWriteTokens",
    "output": "outputTokens",
}
TOKEN_FIELDS = (*_FIELDS, "reasoning")


def stage_usage(ndjson: bytes) -> dict[str, Any]:
    """Cộng `usage` của mọi event `step_end` trong output của một stage.

    `requests` là số event `step_end`, tức số bước agent đã chạy; mỗi bước là một lần dsh gọi model (lần
    thử lại bên trong dsh không được đếm riêng). Bước kết thúc bằng lỗi cũng được đếm: dsh thật không có
    API key phát một `step_end` không kèm `usage` (lượt thử 01/10/2026), nên `requests = 1` và token là
    None. Vì vậy `requests` KHÔNG phải số request đã được tính tiền.
    Một trường token là None khi không có `step_end` nào, hoặc khi có `step_end` thiếu trường đó: cộng một
    phần sẽ ra con số thấp hơn thực tế mà trông như đầy đủ.
    """
    requests = 0
    sums: dict[str, int | None] = dict.fromkeys(_FIELDS, 0)
    for line in ndjson.decode("utf-8-sig", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "status" or event.get("phase") != "step_end":
            continue
        requests += 1
        reported = event.get("usage")
        for name, key in _FIELDS.items():
            value = reported.get(key) if isinstance(reported, dict) else None
            if sums[name] is None or type(value) is not int or value < 0:
                sums[name] = None
            else:
                sums[name] += value
    if requests == 0:
        sums = dict.fromkeys(_FIELDS)
    return {"requests": requests, **sums, "reasoning": None}


def token_sum(usage: dict[str, Any]) -> int | None:
    """Tổng token của một mục usage (input không cache + input từ cache + ghi cache + output); None nếu
    thiếu bất kỳ thành phần nào."""
    parts = [usage.get(name) for name in _FIELDS]
    return sum(parts) if all(type(p) is int for p in parts) else None


def total_usage(stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Tổng qua các stage đã chạy. Một trường là None nếu có stage không báo trường đó."""
    total: dict[str, Any] = {"requests": sum(s["requests"] for s in stages.values())}
    for name in TOKEN_FIELDS:
        values = [s[name] for s in stages.values()]
        total[name] = sum(values) if values and all(type(v) is int for v in values) else None
    return total
