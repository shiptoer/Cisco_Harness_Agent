# A8 — Duplicate operations in ChangeRequest

## Nguồn và phạm vi

Nguồn: Cisco_Harness_Agent_after_prompt_fix.zip.
Base: 8f3ed94dc8449cbfa1836009a6b0b48d3327452a (PR #7).
Sửa trên bản sao riêng; không commit/push hoặc sửa ZIP gốc.
D-049 là ĐỀ XUẤT, chưa có nhóm/GVHD duyệt.

## Lỗi và cách sửa

CBA từng nhận changes=[V,V]. TSA chủ ý stage idempotent nên chỉ lưu [V].
Runner so danh sách có thứ tự/multiplicity và báo ERROR/STAGED_MISMATCH.

Helper harness/proposal.py dùng equality của ToolCall đã validate/chuẩn hóa registry.
CBA kiểm trước ghi change_request.json, ghi trace change_request_rejected với control
 duplicate_operations và TERMINATED với lý do cố định, trả terminal reject.
Runner kiểm lại file CR sau validation và model check, trước prepare/review/TSA.
Kết cục là status=ok, verdict=reject, stopped_by=change_request_validation.
Không có candidate/package; đề xuất trùng không được chuyển sang review.

Không tự deduplicate, không sắp xếp lại hoặc so theo tên tool đơn thuần.
Thao tác khác args giữ nguyên; không tuyên bố phát hiện xung đột hoặc grounding.
TSA gọi lặp thao tác hợp lệ vẫn already_staged. Staged artifacts bị sửa vẫn ERROR.
B1–B4 cùng áp dụng; B0, prompts, registry, models/schema và dataset giữ nguyên.

## Kiểm thử

18 test mới: exact/normalized/key-order/nonadjacent duplicates; CBA B1–B4;
terminal behavior; fake MCP B1–B4; runner kiểm artifact giả ở run/prepare;
đối chứng gỡ từng control; TSA idempotency.
Đối chứng runner tắt kiểm tra tái hiện STAGED_MISMATCH (run) và pending_review (prepare).
Đối chứng CBA tắt kiểm tra tái hiện accepted proposal chứa hai thao tác giống nhau.

Các test dùng fake_dsh/context canary, không gọi model/API, không deploy.
Số liệu offline không chứng minh độ chính xác LLM hoặc CLI trên thiết bị thật.
Grounding xfail và giới hạn Batfish giữ nguyên. Chưa chạy bản sửa này trên Windows.

## Kết quả Linux

- Test mới: 18 passed in 8.22s.
- Full suite: 1111 passed, 1 xfailed in 158.76s (0:02:38)
- Ruff toàn repo: All checks passed!
- Dry B0–B4: 16/16 mỗi mức, 80/80 đúng kỳ vọng.
- Python 3.12.14, pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.9.
- Full: python -m pytest -n 6 --dist loadfile --tb=short.
- Fake runners/dsh; parent test dùng guard TCP loopback-only; không API/deploy.
- Source ZIP SHA-256: bab2212af9c2b1be7de2535f258fb6f8bf19bcdaa0fcefab865af011b1e05d07.
- Patch đã áp thử trên bản giải nén sạch; đối chiếu byte toàn bộ nguồn.
- Windows: chưa kiểm thử; cần chạy lệnh trong HUONG_DAN_WINDOWS.md.

## Windows validation — 2026-10-15

- Ruff: All checks passed.
- Duplicate operations + harness pipeline tests: 70 passed in 671.24s.
- Full suite: 1110 passed, 1 skipped, 1 xfailed in 2796.01s.
- Results reported from the operator's Windows machine.
- Offline tests; no model API calls or deployment.