# A6 — cách ly capability duyệt web — 14/10/2026

## Nguồn và phạm vi

- Đầu vào: `Cisco_Harness_Agent_before_approval_fix.zip` do người vận hành cung cấp.
- Git archive comment: `1ff1e4167f5bc07aa7a06aac0125b1ac19a92863`.
- SHA-256 ZIP: `18f62435f2ccd21745c92bf4e55214e4cf33aca7d3ec97a753ac90506baef1fc`.
- Bản nguồn gồm PR #4 model pin và cập nhật Windows validation của runner trust.
- Sửa trên bản sao riêng; không commit/push, không gọi model API hoặc deploy.
- Chỉ sửa A6 multi-request web. Không đổi model pin, runner, gate, schema, dependency,
  CI, fixed-contract mode, dataset hoặc hỗ trợ đa hãng. A7 retry vẫn ngoài phạm vi.

## Vấn đề và control

Trước sửa, prepare/status trả `review_url` chứa bearer token cho model.
Một chương trình nhận phản hồi đó có thể GET trang, đọc CSRF và POST với Origin đúng.
Host/Origin/CSRF giúp chống một số yêu cầu web giả mạo nhưng không chứng minh người thật
đã duyệt khi chính chương trình có capability lấy form. Không có MCP tool approve là chưa đủ.

Bản sửa bỏ `view_token` khỏi record và bỏ `review_url` khỏi projection MCP.
Bridge tạo bootstrap secret và session secret độc lập, ngẫu nhiên, chỉ trong bộ nhớ.
Khi bridge khởi động, helper riêng nhận bootstrap URL qua stdin, tắt stdout/stderr,
rồi nhờ trình duyệt mặc định mở tab operator. URL không đi qua argv của helper,
MCP response, patch, biến môi trường hay run artifacts. Không fallback in URL khi mở tab lỗi.

GET `/open?token=...` hợp lệ dùng một lần, đặt cookie phiên HttpOnly/SameSite=Strict,
no-store, rồi redirect về `/operator` không có query. Bootstrap dùng no-referrer;
trang form giữ strict-origin để không làm mất Origin hợp lệ khi submit trên trình duyệt.
Tên cookie có cổng để các phiên trên cùng host không vô tình ghi đè nhau.
Cookie không có ranh giới theo port ở mức browser: đây không phải bảo vệ khỏi mọi app loopback.

GET `/operator`, GET `/review/<id>` và POST quyết định đều yêu cầu cookie phiên.
POST còn kiểm tra Host, Origin, content type, kích thước, trường form và CSRF theo request.
CSRF không dùng chéo request hoặc sau khi đã quyết định. Nội dung đề xuất vẫn HTML-escaped,
CSP chặn script/frame, không cache; request/proposal/context hashes và RBAC giữ nguyên.

Nếu helper không mở được tab, bridge chỉ báo thông điệp chung, generate vẫn cần approval.
Người vận hành dừng rồi mở lại từ desktop có trình duyệt mặc định; ID/phiên cũ mất hiệu lực.
Không thêm bypass bằng biến môi trường hoặc tool duyệt.

## Quy trình mới

1. Mở multi-request web; bridge tự mở thêm tab operator riêng.
2. Prepare trả request_id và đề xuất. Chuyển sang tab operator, Refresh requests, khớp ID.
3. Kiểm tra intent, role, snapshot hash, operations rồi Approve hoặc Reject.
4. Quay lại chat yêu cầu generate đúng ID. Approval không tự gọi model hoặc deploy.
5. Giữ URL/cookie/token ngoài chat. Không cấp cho model quyền điều khiển tab operator.

## File thay đổi

| File | Nội dung |
| --- | --- |
| `web/reviews.py` | Bỏ view_token/review_url, thêm danh sách private cho operator |
| `web/approval_http.py` | Bootstrap một lần, cookie phiên, bảo vệ GET và POST |
| `web/open_browser.py` | Helper mở tab qua stdin, tắt output |
| `scripts/web_multi_server.py` | Khởi tạo tab operator, mô tả tool không yêu cầu link |
| `scripts/start_web.ps1` | Thông báo quy trình tab riêng, giữ CRLF |
| `tests/test_approval_isolation.py` | Probe thiếu capability, đối chứng âm, helper |
| `tests/test_web_multi.py`, `tests/test_web_multi_integration.py` | Cập nhật flow, MCP thật với fake_dsh |
| `CLAUDE.md`, `DECISIONS.md`, `docs/MULTI_REQUEST_WEB.md` | Ranh giới tin cậy, D-046 ĐỀ XUẤT, cách dùng |

Đường dẫn `web/*` trong bảng nằm dưới `src/harness_agent/`.

## Kiểm thử và đối chứng

- Biết request_id, giả Origin hoặc biết CSRF nhưng thiếu cookie: không lấy form/không duyệt.
- Bootstrap sai, Unicode sai, dùng lại; cookie sai/phiên khác; CSRF sai request/replay: bị chặn.
- Operator hợp lệ: xem proposal, approve/reject, generate theo contract; RBAC vẫn từ chối khi thiếu quyền.
- Prepare/status/completed/rejected projection và artifact fixture không chứa capability được sinh.
- MCP stdio thật chỉ có ba tool workflow; fake_dsh prepare trả pending không có link/token;
  generate trước approval và tool/argument approve giả đều bị chặn.
- Đối chứng âm cố ý chèn bootstrap URL vào status: chương trình tự mở form, duyệt và generate
  thành công. Điều này tái hiện vấn đề capability leakage, không được dùng trong production.
- Helper mock kiểm tra URL chỉ qua stdin, output bị tắt, lỗi/timeout không in capability.
- Test HTTP chỉ loopback; fake runner không gọi API. Test stdio thay opener bằng mock chỉ trong
  wrapper test và dùng tmp_path cho run artifacts; không thêm cờ bypass vào production.

## Giới hạn

- Đây là cách ly capability khỏi kênh MCP/HTTP không xác thực, không phải chứng thực người thật.
  Runtime cùng user đọc bộ nhớ, profile trình duyệt, điều khiển tab, chặn lời gọi OS hoặc sửa code
  vẫn ngoài phạm vi. Browser có thể lưu URL bootstrap trong history/argv nội bộ; token đã dùng hết hiệu lực.
- HttpOnly chặn JavaScript đọc cookie; không chặn OS/browser automation được cấp quyền.
  SameSite không cô lập mọi ứng dụng trên cùng host. Muốn ranh giới mạnh hơn cần cách ly OS/profile.
- Chưa kiểm thử trình duyệt desktop Windows thật hoặc dsh/model thật sau sửa này.
  Kết quả Linux không thay cho kiểm tra Windows. Cần kiểm tra mở tab bằng flow offline trước khi chạy API.
- D-046 là ĐỀ XUẤT; không tự nâng thành quyết định nhóm/GVHD.


## Kết quả Linux

- Full suite: **1058 passed, 1 xfailed in 126.49s (0:02:06)**.
  Lệnh: `python -m pytest -n 6 --dist loadfile --tb=short`.
- Nhóm web/launcher sau điều chỉnh wrapper dùng tmp_path: **49 passed in 17.65s**.
  Không thay logic production sau full suite; wrapper integration và chú thích được hoàn thiện sau đó.
- Ruff toàn repo: **All checks passed!**
- Chạy khô B4: **16/16 case đúng kỳ vọng**. Token/request counts trong log là số fixture fake_dsh,
  không phải usage từ API thật. Không chạy lại B0–B3 riêng vì runner/gates không đổi.
- Helper kiểm tra browser trong gói được thử bằng HTTP operator mô phỏng, đạt PASS;
  chưa chạy với trình duyệt desktop thật.
- Python 3.12.14 / Linux; pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.9.
- Guard TCP chặn địa chỉ ngoài loopback ở tiến trình test; cấu hình fake và opener mock.
- Không gọi model/API hoặc triển khai thiết bị. Chưa có kết quả Windows cho bản sửa này.

## Windows validation — 2026-10-14

- Ruff: All checks passed.
- Web/launcher tests: 49 passed in 34.16s.
- Full suite: 1057 passed, 1 skipped, 1 xfailed in 1277.97s.
- Desktop browser check: PASS, decision recorded as approved.
- Fixed Windows POST connection abort by reading the bounded request body
  before rejecting authentication/origin headers.
- Results reported from the operator's Windows machine.
- No model/API calls, CLI generation or deployment during browser validation.