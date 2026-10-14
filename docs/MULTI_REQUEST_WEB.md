# Một Web Harness cho nhiều yêu cầu Cisco

Bản cập nhật cho repo đã hợp nhất shared web launcher (main d769fff).
Chế độ mới dùng một context và role do người vận hành chọn khi mở web.
Mỗi yêu cầu có đề xuất và phê duyệt riêng, không cần tạo sẵn file contract.
Chế độ cũ `-Contract ...` vẫn dùng được cho các case đối chứng.

## Cài vào repo trên Windows

Dừng web đang chạy bằng Ctrl+C ở terminal của web. Mở PowerShell tại repo `main`.
Lưu ZIP tải về với tên `multi-request-web.zip` trong Downloads.
Chạy từng khối, dừng nếu có lỗi; không chép dấu nhắc `PS ...>` vào lệnh.

```powershell
git status --short
git switch -c feat/multi-request-web
```

Nếu hai file launcher có chỉnh sửa chưa commit, lưu chúng trước khi giải nén.
Các file dataset cũ đang untracked không thuộc bản cập nhật này.

```powershell
$backup = Join-Path $PWD ("runs\backups\multi-web-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item .\scripts\prepare_web.py, .\scripts\start_web.ps1 -Destination $backup
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\multi-request-web.zip" -DestinationPath . -Force
```

ZIP chỉ thay hai file launcher; thêm bridge, module duyệt, tests và tài liệu.
Không thay `config/local.toml`, runner core, context hoặc file dữ liệu hiện có.
Không cần cài dependency mới nếu repo đã được cài editable bằng `pip install -e ".[dev]"`.

## Kiểm tra offline

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
bridge mở thêm cổng loopback ngẫu nhiên và tự mở một tab duyệt riêng trên trình duyệt mặc định.
Giữ tab này riêng cho người vận hành; không dán URL/cookie/token vào chat. Nếu tab không mở,
dừng web và khởi động lại từ terminal desktop có trình duyệt mặc định hoạt động.
Không có đường dẫn duyệt dự phòng trong phản hồi model.
Model/reasoning lấy từ `config/model.toml`, không gắn cứng vào từng case.
Gửi yêu cầu trong web có thể phát sinh phí API. Không deploy lên thiết bị.

## Cách dùng trong chat Harness

Có thể dán lời nhắc sau vào chat mới:

> Với mỗi yêu cầu Cisco mới, gọi prepare_network_request một lần và giữ nguyên đầy đủ yêu cầu của tôi. Nếu cần làm rõ thì hỏi tôi; nếu pending thì hiển thị request_id, nhắc tôi làm mới tab duyệt riêng đã mở khi bridge khởi động rồi dừng. Không yêu cầu hoặc nhận URL/cookie/token duyệt trong chat. Không tự duyệt, không tự thử lại khi lỗi. Chỉ gọi generate_approved_request khi tôi đã duyệt trên trang cục bộ và yêu cầu tiếp tục. Không tự viết CLI thay cho kết quả tool, không deploy.

Ví dụ yêu cầu độc lập, không phải danh sách case được cài sẵn:

- Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 của SW2 vào VLAN 30. Giữ nguyên trạng thái bật/tắt cổng.
- Đặt description của SW2 GigabitEthernet1/2 thành SALES-PC-03. Giữ nguyên trạng thái bật/tắt cổng.
- Cho tôi biết còn thiếu thông tin gì để tạo một VLAN mới trên SW2.

Quy trình cho mỗi yêu cầu:

1. Chat gọi `prepare_network_request(intent)`. CBA đọc context và đề xuất thao tác, chưa sinh CLI.
2. Nếu thiếu dữ kiện, bổ sung rồi gửi lại một yêu cầu đầy đủ. Nếu có đề xuất, chuyển sang tab duyệt riêng, chọn **Refresh requests**, rồi mở đúng `request_id` đang chờ.
3. Trang duyệt hiện nguyên văn intent, role, hash context và toàn bộ thao tác. Chọn **Approve these exact operations** hoặc **Reject**. Chỉ duyệt nếu nội dung đúng ý bạn.
4. Sau khi duyệt, quay lại Harness và gửi: `Tôi đã duyệt request_id <ID>. Hãy gọi generate_approved_request một lần cho ID này, hiển thị kết quả rồi dừng.`
5. Pipeline kiểm tra contract và các gate. Kết quả accept vẫn chỉ là change package chờ duyệt cấu hình cuối cùng, không phải đã áp dụng.
6. Nhập yêu cầu khác trong cùng web. Không đổi patch, không khởi động lại.

`get_network_request(request_id)` chỉ xem trạng thái; không duyệt hoặc sinh cấu hình.
Không có tool approve hoặc capability duyệt trong kết quả MCP. Lời nhắc trong chat không thay thế phiên duyệt riêng.
Việc bảo vệ này không chứng minh người thật đã bấm nút nếu runtime có quyền đọc phiên trình duyệt.
Phê duyệt trên browser không tự gọi model; bước sinh CLI mới gọi model.

## Phạm vi và giới hạn

- Chỉ hỗ trợ các operation trong registry hiện có: VLAN/access/trunk, IP/trạng thái/description cổng, static route, ACL và AAA. Yêu cầu ngoài phạm vi phải làm rõ hoặc bị từ chối.
- Context là snapshot cố định của lần mở web. Không đọc cấu hình thiết bị thật và không cập nhật snapshot sau khi sinh CLI. Các yêu cầu sau không tự kế thừa thay đổi của yêu cầu trước.
- Role cố định trong phiên; đổi role hoặc context cần mở lại launcher. RBAC và B4 vẫn có hiệu lực sau khi người dùng duyệt.
- Đề xuất trên trang duyệt chưa đi qua toàn bộ TSA gate. Phê duyệt không bảo đảm accept.
- Bước generate tái sử dụng runner có sẵn: chạy lại CBA, so khớp với contract đã duyệt, sau đó mới chạy TSA. Nếu CBA đổi đề xuất, pipeline chặn; không tự duyệt bản mới. Vì vậy một yêu cầu hoàn chỉnh tốn một lượt CBA prepare cộng một lượt CBA/TSA generate.
- Generate đã hoàn tất trả lại kết quả lưu trong phiên khi bị gọi lặp, không chạy lại. Khi generate gặp lỗi bất thường, approval bị tiêu thụ; kiểm tra log trước khi chuẩn bị lại.
- Trạng thái duyệt giữ trong RAM của bridge. Restart làm mất hiệu lực phiên duyệt/ID cũ, nhưng file audit và kết quả vẫn ở `runs/web-prepare-*` / `runs/web-generate-*`.
- Trang duyệt phục vụ người vận hành trên cùng máy: loopback, cookie phiên riêng HttpOnly/SameSite=Strict, kiểm tra Host/Origin, nonce cho form, escape dữ liệu, không cache, không cho nhúng iframe. URL khởi tạo dùng một lần chỉ chuyển tới trình duyệt qua helper riêng; prepare/status không trả `review_url`. Đây không phải cơ chế đăng nhập đa người dùng hay sandbox OS. Runtime cùng user có quyền đọc bộ nhớ, browser profile, điều khiển tab hoặc sửa code vẫn ngoài phạm vi. URL ban đầu có thể tồn tại trong lịch sử trình duyệt nhưng hết hiệu lực sau một lần mở.
- Không đưa các link có token hoặc raw logs chứa dữ liệu mạng lên GitHub.
- Không tiếp tục công việc dataset ZIP 2400 mẫu.

## Ghi nhận thay đổi trong Git

Sau khi checks trên Windows đạt:

```powershell
git add -- scripts/prepare_web.py scripts/start_web.ps1 scripts/web_multi_server.py src/harness_agent/web tests/test_web_multi.py tests/test_web_multi_launcher.py tests/test_web_multi_integration.py docs/MULTI_REQUEST_WEB.md docs/MULTI_REQUEST_WEB_VALIDATION.md
git diff --cached --stat
git commit -m "Add multi-request web workflow with per-request browser approval"
git push -u origin feat/multi-request-web
```

Tạo PR vào main và chờ CI. Không dùng `git add .` để tránh đưa các file thử dataset đang untracked vào commit.
