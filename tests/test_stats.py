"""Thống kê của D-037 (hiện thực ở D-063): Wilson CI, bootstrap cặp, McNemar exact, Holm. Thư viện chuẩn."""

from __future__ import annotations

import pytest

from harness_agent.eval import stats


def test_wilson_known_values():
    lo, hi = stats.wilson(20, 20)
    assert lo == pytest.approx(0.839, abs=0.001) and hi == pytest.approx(1.0, abs=0.001)
    assert stats.wilson(0, 0) == (0.0, 1.0)
    lo, hi = stats.wilson(0, 20)
    assert lo == pytest.approx(0.0, abs=1e-9) and hi == pytest.approx(0.161, abs=0.001)
    # 1/8 = 12.5%: khoảng Wilson 95% là [2.2%, 47.1%] (tính tay theo công thức Wilson 1927).
    lo, hi = stats.wilson(1, 8)
    assert lo == pytest.approx(0.0224, abs=0.001) and hi == pytest.approx(0.4709, abs=0.001)


def test_wilson_rejects_impossible_counts():
    with pytest.raises(ValueError):
        stats.wilson(3, 2)
    with pytest.raises(ValueError):
        stats.wilson(-1, 2)


def test_mcnemar_exact():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert stats.mcnemar_exact(10, 0) < 0.01
    assert stats.mcnemar_exact(10, 0) == pytest.approx(2 / 1024)
    assert stats.mcnemar_exact(3, 3) == 1.0  # đối xứng hoàn toàn: p kẹp ở 1
    assert stats.mcnemar_exact(2, 7) == stats.mcnemar_exact(7, 2)


def test_holm():
    assert stats.holm({"a": 0.01, "b": 0.04}) == pytest.approx({"a": 0.02, "b": 0.04})
    # Đơn điệu: p hiệu chỉnh của giả thuyết sau không nhỏ hơn giả thuyết trước; kẹp ở 1.
    adj = stats.holm({"x": 0.03, "y": 0.02, "z": 0.5})
    assert adj == pytest.approx({"y": 0.06, "x": 0.06, "z": 0.5})
    assert stats.holm({}) == {}
    assert stats.holm({"a": 0.6, "b": 0.7}) == pytest.approx({"a": 1.0, "b": 1.0})


def test_bootstrap_diff_is_deterministic_and_paired():
    a = [1.0] * 10 + [0.0] * 10
    b = [0.0] * 20
    lo, hi = stats.bootstrap_diff(a, b, iters=2000, seed=0)
    assert (lo, hi) == stats.bootstrap_diff(a, b, iters=2000, seed=0)
    assert 0.2 < lo < 0.5 < hi < 0.8
    # Cùng dữ liệu → hiệu 0 ở mọi mẫu (ghép cặp theo chỉ số).
    assert stats.bootstrap_diff(a, a, iters=500) == (0.0, 0.0)


def test_bootstrap_diff_needs_paired_samples():
    with pytest.raises(ValueError):
        stats.bootstrap_diff([1.0, 0.0], [1.0])
    with pytest.raises(ValueError):
        stats.bootstrap_diff([], [])
