# A9 — Sửa test grounding, chưa triển khai grounding

## Nguồn

Cisco_Harness_Agent_before_grounding_test_fix.zip, base d411fc80d14b27e0d4310c8ad43c620229ceefe0.
Bản nguồn đã có A8 (PR #8). Sửa trên bản sao; không commit/push hoặc sửa ZIP gốc.
D-050 là ĐỀ XUẤT. Phạm vi chỉ kiểm thử và tài liệu.

## Vì sao test cũ chưa đúng

Test ghi ý định VLAN 20 trong comment, nhưng chỉ gọi gate với approved VLAN 10 và call VLAN 10.
Gate không nhận intent gốc, nên không thể phân biệt CBA đúng hay sai ý định. G5 kiểm consistency
với proposal, không phải semantic grounding. Một sửa đúng ở runner cũng không làm test cũ pass.

## Test mới

- Ba test gate mô tả matching proposal, TSA deviation và correlated CBA/TSA choice.
- Ba đối chứng pipeline B4: đúng VLAN 20 accept và CLI đúng; TSA lệch bị G5 reject;
  proposal VLAN 10 bị confirmed_intent VLAN 20 reject trước TSA khi có oracle operator.
- Một strict xfail pipeline B4: truyền intent VLAN 20 thật vào run_case; fake CBA/TSA chọn VLAN 10.
  Không bật confirmed_intent để không dùng HITL che giới hạn grounding tự động.
- Xác minh status=ok, không error_code, intent có trong prompt, proposal chứa đúng fixture.
  Nếu accept, xác minh chính xác candidate/SW2.cfg VLAN 10 và package tồn tại trước khi phát
  UngroundedPublication. Marker chỉ xfail loại exception này; các assert/runtime error khác fail thường.
- Nếu control grounding tương lai trả reject/clarify, stopped_by=intent_grounding và không xuất
  candidate/package, test thành XPASS(strict); cần xem bằng chứng rồi bỏ marker.

Test fixture dùng fake_dsh/context canary sao chép vào tmp_path; UTF-8 explicit cho Windows.
Không dùng API, thiết bị, model thật hoặc dữ liệu production. Không sửa thuật toán hay B0–B4.

## Giới hạn

A9 được sửa ở biên pipeline thay vì tạo ground() giả hoặc triển khai ngay thiết kế H3 của báo cáo.
H3/D-030 vẫn là việc riêng cần thiết kế phạm vi, chống báo nhầm và duyệt ảnh hưởng ablation.
Test này chỉ bao phủ một ca VLAN cụ thể, không chứng minh khả năng grounding cho 11 tool.
xfail vẫn là lỗ hổng ngữ nghĩa đã biết, không phải PASS và không được bỏ để làm xanh suite.
Confirmed intent là oracle người vận hành, không thay thế semantic grounding tự động.
Các báo cáo merge/test cũ được giữ như bằng chứng lịch sử, không sửa số liệu quá khứ.

## Kết quả Linux

- Targeted grounding + confirmed intent: 10 passed, 1 xfailed in 6.89s.
- Full suite: 1115 passed, 1 xfailed in 148.59s (0:02:28)
- Ruff toàn repo: All checks passed!
- Dry B4: 16/16 đúng kỳ vọng. Runtime không đổi; không cần lặp dry mọi mức.
- Chạy riêng known-gap với --runxfail: 1 failed in 3.90s, chính xác UngroundedPublication
  (intent VLAN 20, published VLAN 10). Đây là bằng chứng lỗ hổng còn tồn tại, không phải test suite pass.
- Python 3.12.14, pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.9.
- Full: python -m pytest -n 6 --dist loadfile --tb=short.
- Fake runners/dsh; parent test dùng guard TCP loopback-only; không API/deploy.
- Source ZIP SHA-256: 31ce1bf6cb4997295e06d47fc3b08cfdfbd7cfe06b72d4f85e91578b1caa989a.
- Patch đã áp thử trên bản giải nén sạch; đối chiếu byte toàn bộ nguồn.
- Windows: chưa kiểm thử; cần chạy lệnh trong HUONG_DAN_WINDOWS.md.

## Windows validation — 2026-10-15

- Ruff: All checks passed.
- Grounding + confirmed intent tests: 10 passed, 1 xfailed in 88.06s.
- Full suite: 1114 passed, 1 skipped, 1 xfailed in 2085.32s.
- Results reported from the operator's Windows machine.
- Offline tests; no model API calls or deployment.
- Automatic grounding remains unimplemented; the known-gap test remains xfail.
