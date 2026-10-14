# A7 — retry sau lỗi trước khi generate bắt đầu — 14/10/2026

## Nguồn và phạm vi

- ZIP đầu vào: `Cisco_Harness_Agent_before_retry_fix.zip`, do Khang cung cấp.
- Git archive comment: `43470c6dfeb59ff54a8477a1388199cea0ccc77c` (main đã merge PR #5).
- SHA-256: `f42d769fb0ed048ebec3789dc6d75e5fbdacb2232f080cefdf6f506d3708a380`.
- Sửa trên bản sao riêng, không sửa ZIP gốc, không commit/push thay người dùng.
- Giữ nguyên A6 đã sửa lỗi Windows POST unread-body, A3 model pin và runner trust.
- Chỉ sửa multi-request web. Không đổi schema/model/dependency/CI, runner core, workspace
  theo D-025, dataset, evaluator, grounding hoặc hỗ trợ đa hãng.
- Không gọi model API hoặc triển khai thiết bị. Tất cả runner dùng fake/stub trong kiểm thử.

## Lỗi tái hiện trước sửa

Báo cáo kỹ thuật 11/10/2026, mục A7, chỉ ra generate tiêu thụ approval khi workspace bận.
Code thực tế chuyển `approved -> running` trước khi gọi run_case. run_case lấy khóa workspace
và ném DshSetupError nếu process khác đang giữ khóa. ReviewService bắt mọi exception thành failed.
Kết quả: chưa gọi dsh, chưa tạo run-dir generate nhưng approval đã mất; lần gọi lại bị chặn.

Test mới dùng process con giữ khóa hệ điều hành thật. Không dùng hold lồng trong cùng process
vì D-025 cho phép re-entrant trong cùng process, nên cách đó không mô phỏng tranh chấp đúng.
Test trên source gốc thất bại đúng assertion `failed != approved`; log before-fix.log đi kèm.

## Thay đổi

Generate dùng ExitStack giữ reservation `lock_workspace(cfg)` trước khi đặt running.
Reservation giữ liên tục qua runner và xử lý kết quả; run_case lồng lại cùng khóa theo D-025.
Không kiểm tra khóa rồi thả và lấy lại, tránh race ở ranh giới này.

| Tình huống | Trạng thái / hành vi |
| --- | --- |
| Bridge đang chạy yêu cầu khác | Blocked, approval hiện có không bị tiêu thụ |
| Nạp config/lấy reservation thất bại trước running | Blocked, giữ approved; chưa gọi runner |
| Operator chủ động thử lại sau khi xử lý nguyên nhân | Kiểm tra lại các hash, lấy khóa rồi mới chạy |
| Hash context/config/model/proposal/contract đổi | Blocked; không gọi runner |
| Exception sau biên running, kể cả DshSetupError | Failed, approval không phục hồi |
| Audit lỗi sau biên running | Failed; không để trạng thái chạy dở mở đường retry |
| Runner trả CaseResult accept/reject/clarify/error | Completed, giữ nguyên outcome; gọi lặp trả cache |
| Hai generate đồng thời | Chỉ một lời gọi vào runner; lời gọi còn lại blocked |

Sửa không dựa vào tên/thông điệp exception để suy ra model đã chạy hay chưa. DshSetupError có thể
xảy ra sau một stage đã gọi model; chỉ lỗi tại biên lấy reservation trước execution mới giữ approval.
Exception sau start được ghi bằng phase và lớp exception ra stderr. Không log raw message/traceback
có thể chứa đường dẫn, dữ liệu mạng hoặc secret. Các phase: before_start, after_start, failure_audit.
Nếu ghi audit failed cũng lỗi, trạng thái failed trong RAM vẫn giữ; không tuyên bố file audit đã cập nhật.

Thông báo tool và tài liệu hướng dẫn dừng khi blocked, xử lý nguyên nhân rồi yêu cầu generate lại
đúng request_id. Không cần prepare lại chỉ vì workspace bận. Không thêm vòng lặp retry tự động,
MCP approval tool hoặc quyền mới. Yêu cầu người vận hành gửi lời nhắc tiếp tục là hướng dẫn workflow,
không phải cơ chế chứng thực độc lập người gửi lời nhắc.

## File

| File | Vai trò |
| --- | --- |
| `src/harness_agent/web/reviews.py` | Reservation trước start; terminal failure; chẩn đoán có giới hạn |
| `scripts/web_multi_server.py` | Mô tả tool về manual retry và approval retained |
| `tests/test_approval_retry.py` | 21 regression/control cases, có process lock thật và fake pipeline |
| `docs/MULTI_REQUEST_WEB.md` | Cập nhật hướng dẫn khi workspace bận/lỗi sau start |
| `CLAUDE.md`, `DECISIONS.md` | Quy tắc và D-047 ĐỀ XUẤT |
| `docs/APPROVAL_RETRY_FIX.md` | Báo cáo này |

## Kiểm thử có ý nghĩa

- Tái hiện trước sửa: process khác giữ workspace; trạng thái cũ failed thay vì approved.
- Sau sửa: cùng tranh chấp không gọi runner, không sinh generate run-dir; nhả khóa rồi thử lại được.
- Đối chứng âm chỉ gỡ reservation mới: runner vẫn lấy khóa thật và tái hiện mất approval.
- Tích hợp run_case/fake_dsh với fixed workspace: thử lại sau tranh chấp vẫn accept và cache đúng;
  reservation lồng không deadlock.
- Giữ khóa xuyên runner, giải phóng sau lỗi; concurrent generate chỉ thực thi một lần.
- Exception RuntimeError/DshSetupError/OSError sau entry đều failed, không có retry.
- Outcome error/reject/clarify giữ nguyên, cached, không chuyển ERROR thành PASS.
- Mọi hash đã duyệt vẫn kiểm lại khi retry. Secret trong exception không xuất ra stderr/tool message.
- Audit running/completed/failed lỗi đều không cho tái sử dụng approval.
- Toàn bộ nhóm A6 web và launcher vẫn được kiểm tra, không mở browser desktop thật trong pytest.

## Giới hạn và quyết định

D-047 là ĐỀ XUẤT, cần GVHD duyệt semantics retry trong thực nghiệm.
Giữ workspace cố định; không áp đề xuất tạo workspace mới mỗi phiên vì có thể đổi context runtime
và không cần thiết để giải quyết mất approval khi tranh chấp. Không thay implementation khóa D-025.
Trạng thái duyệt chỉ trong RAM; restart vẫn cần prepare/approve mới. Không tự khôi phục một execution
đã bắt đầu hoặc trạng thái không xác định. Không có sandbox OS, không chống runtime cùng user sửa
code/bộ nhớ/files. Bản sửa không thay thế A6 hay chứng thực con người đã gửi lời nhắc retry.

Kết quả Windows và trình duyệt desktop chưa được kiểm chứng cho A7 trong lượt này; phải ghi nhận
kết quả thực tế của người vận hành, không sao chép số liệu Windows A6 làm bằng chứng A7.

## Kết quả Linux

- Full suite: **1079 passed, 1 xfailed in 839.23s (0:13:59)**.
- A7 + web/launcher: **70 passed in 72.54s (0:01:12)**.
- Ruff toàn repo: **All checks passed!**
- Chạy khô B4: **16/16 case đúng kỳ vọng**; token/request trong log là số fake, không phải API usage.
- Python 3.12.14, pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.9, mcp 1.30.0.
- Full command: `python -m pytest -n 6 --dist loadfile --tb=short`.
- Guard TCP loopback-only ở tiến trình test; fake config, opener mock; không gọi model/API hoặc deploy.
- Giữ log before-fix thất bại có chủ đích làm bằng chứng tái hiện; không tính là kết quả cuối.
- Chưa có kết quả Windows A7.

## Windows validation — 2026-10-14

- Ruff: All checks passed.
- A7 + web/launcher tests: 70 passed in 42.16s.
- Full suite: 1078 passed, 1 skipped, 1 xfailed in 1233.06s.
- Results reported from the operator's Windows machine.
- Validation used fake/stub runners; no model API calls or deployment.
