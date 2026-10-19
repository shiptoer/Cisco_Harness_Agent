"""Thống kê cho báo cáo ablation (D-037, hiện thực ở D-063). Chỉ dùng thư viện chuẩn: không thêm dependency.

- `wilson`: khoảng tin cậy Wilson cho một tỉ lệ k/n (đúng hơn khoảng Wald khi n nhỏ hoặc p sát 0/1).
- `bootstrap_diff`: CI percentile của hiệu hai trung bình trên mẫu GHÉP CẶP theo chỉ số (cùng case ở hai mức).
- `mcnemar_exact`: kiểm định McNemar chính xác (nhị thức hai phía) trên hai ô lệch b, c của bảng 2×2 ghép cặp.
- `holm`: hiệu chỉnh Holm–Bonferroni cho nhiều phép so sánh.
"""

from __future__ import annotations

import math
import random
import statistics


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Khoảng Wilson của k thành công trên n phép thử. n = 0 → (0.0, 1.0): không có dữ liệu thì mọi tỉ lệ đều
    có thể."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"cần 0 <= k <= n, nhận k={k}, n={n}")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _percentile(sorted_values: list[float], q: float) -> float:
    """Percentile nội suy tuyến tính (cùng cách với numpy mặc định) trên danh sách đã sắp."""
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_diff(a: list[float], b: list[float], iters: int = 10000, seed: int = 0) -> tuple[float, float]:
    """CI 95% (percentile 2.5 / 97.5) của mean(a) - mean(b). Mỗi mẫu bootstrap rút lại CHỈ SỐ có hoàn lại và
    lấy cùng chỉ số ở cả a và b (ghép cặp theo case, D-037). RNG riêng theo `seed`: chạy lại cho cùng kết
    quả."""
    if len(a) != len(b):
        raise ValueError(f"bootstrap ghép cặp cần hai mẫu cùng độ dài ({len(a)} != {len(b)})")
    if not a:
        raise ValueError("bootstrap cần ít nhất một cặp")
    rng = random.Random(seed)  # noqa: S311 — tất định theo seed, không phải mật mã
    n = len(a)
    diffs = []
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(statistics.fmean(a[i] for i in idx) - statistics.fmean(b[i] for i in idx))
    diffs.sort()
    return (round(_percentile(diffs, 0.025), 12), round(_percentile(diffs, 0.975), 12))


def mcnemar_exact(b: int, c: int) -> float:
    """p hai phía của McNemar chính xác: p = min(1, 2·P(X <= min(b, c))) với X ~ Bin(b + c, 0.5). b + c = 0
    (không có cặp lệch) → 1.0."""
    if b < 0 or c < 0:
        raise ValueError("b, c phải không âm")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(p: dict[str, float]) -> dict[str, float]:
    """Hiệu chỉnh Holm–Bonferroni: sắp p tăng dần, p_(i) nhân (m - i), lấy max tích luỹ để giữ thứ tự,
    kẹp ở 1.
    Thứ tự hoà giữa hai p bằng nhau theo tên (tất định)."""
    m = len(p)
    out: dict[str, float] = {}
    running = 0.0
    for i, (name, value) in enumerate(sorted(p.items(), key=lambda kv: (kv[1], kv[0]))):
        running = max(running, min(1.0, (m - i) * value))
        out[name] = running
    return out
