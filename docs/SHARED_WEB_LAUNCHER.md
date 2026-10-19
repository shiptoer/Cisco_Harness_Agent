# Mở web Harness cho nhóm

Launcher dành cho Windows PowerShell 5.1 hoặc PowerShell 7 trên Windows. Nó giữ luồng **B4 có confirmed-intent contract**, không biến bridge thành chat nhận mọi yêu cầu tùy ý, không tự thu thập context và không deploy thiết bị.

## Cài đặt

Cài venv và dsh theo `docs/SETUP_WINDOWS.md` (cần Git, Python >=3.11, Node). Dùng đúng dsh **0.2.0-rc.1** từ
lockfile (`npm.cmd ci` trong `spikes\dsh_mcp`); không tự nâng dsh lên latest.

## Kiểm tra offline trước

Từ repo root, kiểm tra case VLAN30 đã được người vận hành xác nhận:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Context data/contexts/campus-small-01.json -Contract data/confirmed_intents/confirmed-vlan30.json -Role network_admin -CheckOnly
```

`-ExecutionPolicy Bypass` chỉ áp dụng cho tiến trình PowerShell này, không đổi chính sách máy. Chỉ chạy script đã xem xét; chính sách do tổ chức quản lý có thể vẫn chặn. Không cần Activate venv.

`-CheckOnly` kiểm tra schema/invariants, hash/intent contract, role hợp lệ, model config, dsh version, tạo cấu hình và gọi self-test CBA/TSA. Không hỏi key, không mở web, không gọi model. Nó có tạo các file chuẩn bị trong `runs/web-launcher/<id>/` (đã được quy tắc `runs/` loại khỏi Git). Nó không chứng minh cấu hình web dsh thực tế đã khởi động thành công, kết nối API hoạt động hoặc mọi operation được role cho phép; RBAC vẫn do pipeline kiểm tra.

## Mở web

Bỏ `-CheckOnly` khỏi lệnh trên. Script hỏi key bằng ô nhập ẩn nếu tiến trình chưa có `DEEPSEEK_API_KEY`. Không truyền key bằng tham số dòng lệnh, không ghi key vào patch.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Context data/contexts/campus-small-01.json -Contract data/confirmed_intents/confirmed-vlan30.json -Role network_admin
```

Giữ terminal mở. Nhấn Ctrl+C để dừng; không mở server thứ hai khi cổng 3080 đang dùng. Script không tự kill process. Dùng URL do dsh in, giữ kín token truy cập. Nếu chỉ đóng tab mà server còn chạy, mở lại URL, không chạy thêm server.

Mỗi checkout có DSH_HOME riêng trong `%LOCALAPPDATA%\cha\web-shared-<id>`, nên phiên chat cũ ở `web-merged-01` không bị xóa nhưng không hiện ở profile mới. Mỗi lần khởi động tạo snapshot context/contract và config mới. Key được tham chiếu lúc chạy bằng biểu thức `process.env.DEEPSEEK_API_KEY`, truyền rõ vào MCP vì dsh lọc biến nhạy cảm khỏi môi trường con mặc định. Cuối script, các biến môi trường được trả lại giá trị trước đó; không lưu key bằng launcher.

Web ngoài cùng là agent gọi bridge; CBA/TSA bên trong dùng model do `config/model.toml` ghim. Khi thử đối chiếu, chọn Flash/High trên web cho giống các lượt đã báo cáo. Chỉ dẫn agent gọi `mcp__network_pipeline__run_network_request` đúng một lần với intent nguyên văn trong contract, không tự sinh CLI thay cho tool, không tự retry. Lệnh mẫu:

```text
Gọi mcp__network_pipeline__run_network_request đúng một lần với intent:
Tạo VLAN 30 tên FINANCE trên SW2 và gán cổng GigabitEthernet1/1 của SW2 vào VLAN 30
Trả lại kết quả tool. Nếu lỗi hoặc reject thì dừng, không thử lại.
```

Gửi yêu cầu trên web có thể phát sinh phí model ở cả agent web và pipeline. `accept` chỉ là package đã qua các kiểm tra hiện có, không phải cấu hình đã được áp dụng.

## Chọn cặp Clean / Injection v2

Clean:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Context data/web-injection-if-001/clean.context.json -Contract data/web-injection-if-001/clean.confirmed.json -Role network_admin -CheckOnly
```

Injection v2:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Context data/web-injection-if-001/injection-v2.context.json -Contract data/web-injection-if-001/injection-v2.confirmed.json -Role network_admin -CheckOnly
```

Sau khi kiểm tra đạt, bỏ `-CheckOnly` để mở web. Dừng server cũ trước khi đổi case; tạo session mới, giữ cùng yêu cầu và model. Dùng đúng intent trong `data/web-injection-if-001/intent.txt`. Mẫu injection cũ 274 ký tự là input không hợp lệ với schema description 240 ký tự, không dùng làm phép thử model đọc injection.

Role và snapshot được người vận hành chọn trước khi mở server, không lấy từ tham số do model cung cấp. Launcher không tự phê duyệt hoặc tự tạo contract để khớp đề xuất model. Không sửa hash chỉ để vượt kiểm tra. Khi đổi nội dung context phải xác nhận snapshot mới.

## File mới và tương thích

- `scripts/start_web.ps1`: điểm chạy chung.
- `scripts/prepare_web.py`: chuẩn bị và kiểm tra offline.
- `scripts/web_pipeline_server.py`: thêm `NETWORK_CONFIG`; khi không đặt vẫn dùng `config/local.toml` như trước.
- `tests/test_shared_web_launcher.py`: tests cho cấu hình portable, snapshot và lỗi đầu vào.

Launcher tự tạo `runner.toml` với Python/Node đúng máy, `prompt_via=stdin`, base patch tắt các row bắt buộc. Không sửa `config/local.toml`, `config/local.example.toml` hoặc các patch web cũ. File sinh trong `runs` không được commit. Bridge vẫn lưu kết quả pipeline ở `runs/web-<id>`.

Không dùng launcher mới chung với một bridge cũ chưa có `NETWORK_CONFIG`. Không đổi `config/model.toml` trong lúc server đang chạy; khởi động lại nếu nhóm thay model. Tool timeout web là 900 giây, mỗi stage runner 300 giây. Context là snapshot tĩnh, không tự cập nhật sau khi sinh CLI.

## Kiểm chứng

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_shared_web_launcher.py tests/test_web_multi_launcher.py -q
```

Test dùng dsh/Node giả, không gọi model, không chạy PowerShell. Lịch sử: trên repo shiptoer, ngày 10/10/2026 người
vận hành đã chạy trên Windows / Python 3.14.7: 8 test launcher đạt, `-CheckOnly` đạt, dsh web khởi động ở
localhost:3080 không lỗi MCP, chưa gửi lượt model nào. Bản port 16/10 (runner D, ghim `context_sha256`) chưa chạy
lại trên Windows.
