# Một Web Harness cho nhiều yêu cầu Cisco

Chế độ nhiều yêu cầu (MCP bridge `scripts/web_multi_server.py`) dùng một context và role do người vận hành chọn khi mở web.
Mỗi yêu cầu có đề xuất và phê duyệt riêng, không cần tạo sẵn file contract.
Chế độ cũ `-Contract ...` vẫn dùng được cho các case đối chứng.

## Kiểm tra offline

Cài đặt chung (venv, `npm ci` trong `spikes/dsh_mcp` để có dsh 0.2.0-rc.1): `docs/SETUP_WINDOWS.md`.

```powershell
.\.venv\Scripts\python.exe -m ruff check scripts/prepare_web.py scripts/web_multi_server.py src/harness_agent/web tests/test_web_multi.py tests/test_web_multi_launcher.py tests/test_web_multi_integration.py
$testTemp = Join-Path $env:TEMP ("cisco-multi-web-" + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest tests/test_shared_web_launcher.py tests/test_web_multi.py tests/test_web_multi_launcher.py tests/test_web_multi_integration.py --basetemp="$testTemp" --tb=short
```

Các test sử dụng runner giả hoặc `fake_dsh`, không gọi model. Thư mục tạm riêng tránh lỗi cleanup `pytest-current` đã gặp trên Windows.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -MultiRequest -Context data/contexts/campus-small-01.json -Role network_admin -CheckOnly
```

CheckOnly xác thực context/model, tạo patch và self-test CBA/TSA; không mở web, không gọi model.

## Mở web

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -MultiRequest -Context data/contexts/campus-small-01.json -Role network_admin
```

Nhập API key khi được hỏi. Giữ terminal mở. Web vẫn dùng cổng 3080;
bridge mở thêm cổng loopback ngẫu nhiên cho trang duyệt cục bộ.
Model/reasoning lấy từ `config/model.toml`, không gắn cứng vào từng case.
Gửi yêu cầu trong web có thể phát sinh phí API. Không deploy lên thiết bị.

## Cách dùng trong chat Harness

Có thể dán lời nhắc sau vào chat mới:

> Với mỗi yêu cầu Cisco mới, gọi prepare_network_request một lần và giữ nguyên đầy đủ yêu cầu của tôi. Nếu cần làm rõ thì hỏi tôi; nếu pending thì hiển thị request_id rồi dừng (link duyệt nằm ở terminal mở web, không qua chat). Không tự duyệt, không tự thử lại khi lỗi. Chỉ gọi generate_approved_request khi tôi đã duyệt trên trang cục bộ và yêu cầu tiếp tục. Không tự viết CLI thay cho kết quả tool, không deploy.

Ví dụ yêu cầu độc lập, không phải danh sách case được cài sẵn:

- Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 của SW2 vào VLAN 30. Giữ nguyên trạng thái bật/tắt cổng.
- Đặt description của SW2 GigabitEthernet1/2 thành SALES-PC-03. Giữ nguyên trạng thái bật/tắt cổng.
- Cho tôi biết còn thiếu thông tin gì để tạo một VLAN mới trên SW2.

Quy trình cho mỗi yêu cầu:

1. Chat gọi `prepare_network_request(intent)`. CBA đọc context và đề xuất thao tác, chưa sinh CLI.
2. Nếu thiếu dữ kiện, bổ sung rồi gửi lại một yêu cầu đầy đủ. Nếu có đề xuất, mở link duyệt in ở terminal đã chạy `start_web.ps1` (dòng `[multi-web] Review link for <request_id>: ...`).
3. Trang duyệt hiện nguyên văn intent, role, hash context và toàn bộ thao tác. Chọn **Approve these exact operations** hoặc **Reject**. Chỉ duyệt nếu nội dung đúng ý bạn.
4. Sau khi duyệt, quay lại Harness và gửi: `Tôi đã duyệt request_id <ID>. Hãy gọi generate_approved_request một lần cho ID này, hiển thị kết quả rồi dừng.`
5. Pipeline kiểm tra contract và các gate. Kết quả accept vẫn chỉ là change package chờ duyệt cấu hình cuối cùng, không phải đã áp dụng.
6. Nhập yêu cầu khác trong cùng web. Không đổi patch, không khởi động lại.

`get_network_request(request_id)` chỉ xem trạng thái; không duyệt hoặc sinh cấu hình.
Không có tool approve để model tự phê duyệt. Lời nhắc trong chat không thể thay nút duyệt.
Phê duyệt trên browser không tự gọi model; bước sinh CLI mới gọi model.

## Phạm vi và giới hạn

- Chỉ hỗ trợ các operation trong registry hiện có: VLAN/access/trunk, IP/trạng thái/description cổng, static route, ACL và AAA. Yêu cầu ngoài phạm vi phải làm rõ hoặc bị từ chối.
- Context là snapshot cố định của lần mở web. Không đọc cấu hình thiết bị thật và không cập nhật snapshot sau khi sinh CLI. Các yêu cầu sau không tự kế thừa thay đổi của yêu cầu trước.
- Role cố định trong phiên; đổi role hoặc context cần mở lại launcher. RBAC và B4 vẫn có hiệu lực sau khi người dùng duyệt.
- Đề xuất trên trang duyệt chưa đi qua toàn bộ TSA gate. Phê duyệt không bảo đảm accept.
- Bước generate tái sử dụng runner có sẵn: chạy lại CBA, so khớp với contract đã duyệt, sau đó mới chạy TSA. Nếu CBA đổi đề xuất, pipeline chặn; không tự duyệt bản mới. Vì vậy một yêu cầu hoàn chỉnh tốn một lượt CBA prepare cộng một lượt CBA/TSA generate.
- Generate đã hoàn tất trả lại kết quả lưu trong phiên khi bị gọi lặp, không chạy lại. Khi generate gặp lỗi bất thường sau khi dsh đã chạy, approval bị tiêu thụ; kiểm tra log trước khi chuẩn bị lại. Lỗi máy trước khi dsh khởi động (khoá workspace, cấu hình, preflight harness) giữ nguyên approval; sửa máy rồi generate lại.
- Trạng thái duyệt giữ trong RAM của bridge. Restart làm mất hiệu lực các link/ID cũ, nhưng file audit và kết quả vẫn ở `runs/web-prepare-*` / `runs/web-generate-*`.
- Trang duyệt phục vụ người vận hành trên cùng máy: loopback, kiểm tra Host/Origin, nonce cho form, escape dữ liệu, không cache, không cho nhúng iframe. Đây không phải cơ chế đăng nhập đa người dùng hay bảo vệ trước chương trình cục bộ có quyền sửa file.
- Không đưa các link có token hoặc raw logs chứa dữ liệu mạng lên GitHub.

## Ghi chú port 16/10

Bản này port từ repo shiptoer vào nhánh hoàn thiện 16/10 (runner D). Khác với bản gốc:

- **Runner D.** `ReviewService` gọi `run_case` với `level="B4"`, `context_sha256` (hash file context lấy lúc khởi
  động bridge) và `evaluated_at` (đồng hồ máy lúc gọi), giống UI vận hành (D-054, D-057). File context đổi sau khi
  mở web thì cả bridge lẫn runner dừng. `web_pipeline_server.py` cũng ghim hash và chạy B4 rõ ràng (mặc định của
  runner D là B3). `prepare_web.py` truyền hash của snapshot cho `build_patch`/`preflight` (harness D bắt buộc
  `--context-sha256`) và không resolve symlink của python (trên Linux/macOS python của venv là symlink; resolve sẽ
  chạy harness server ngoài venv).
- **A6 (review 11/10).** Kết quả tool `prepare_network_request` / `get_network_request` không còn `review_url`
  (link có token xem trang duyệt). Model chỉ nhận `request_id`; link in ra stderr của bridge, tức terminal chạy
  `start_web.ps1`. Test: `test_prepare_response_never_contains_the_view_token`.
- **A7 (review 11/10).** Lỗi trước khi dsh khởi động (`DshSetupError`, `WorkspaceError`, `ContextBindingError`
  và chưa có `<stage>.ndjson` nào trong run-dir) đưa record về `approved` thay vì `failed`; loại và nội dung lỗi
  (đã che secret của context) in ra stderr. Lỗi sau khi dsh đã chạy, hoặc lỗi loại khác, vẫn tiêu thụ approval.
  Test: `test_workspace_error_before_dsh_keeps_approval`, đối chứng âm
  `test_setup_error_after_dsh_started_consumes_approval`.
- **Kịch bản `fake_dsh`.** Runner D tìm kịch bản theo tên file case (D-045), không qua khoá `fake_scenario`;
  `tests/test_web_multi_integration.py` dùng `scenario_path`.
- **Dữ liệu.** `data/confirmed_intents/` và `data/web-injection-if-001/` chép nguyên; hash
  `9150933a…817c` vẫn đúng với `data/contexts/campus-small-01.json` của nhánh này (byte giống hệt), không phải
  tính lại. `confirmed-vlan30-invalid-hash.json` cố ý sai hash; `injection.context.json` cố ý vi phạm schema
  (description dài hơn 240 ký tự) và bị chặn trước CBA.
- Bỏ các phần chỉ dành cho bản ZIP của shiptoer (giải nén vào repo, lệnh `git add`/`commit`/`push` riêng).
- Biến `CHA_WEB_OUT` (tuỳ chọn, người vận hành đặt; không bao giờ từ tham số tool) đổi thư mục kết quả của bridge,
  mặc định `runs/`. Test dùng nó để chỉ ghi vào `tmp_path`.
- `prepare_web.py` chạy preflight harness ở B4, cùng mức với bridge.
- Preflight hỏng (kết cục ERROR `PREFLIGHT_FAILED`, không phải exception) mà chưa stage nào chạy dsh cũng giữ
  approval như A7. Test: `test_preflight_failure_before_dsh_keeps_approval`, đối chứng âm
  `test_preflight_failure_after_dsh_started_consumes_approval`.
