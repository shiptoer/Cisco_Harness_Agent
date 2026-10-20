# Bản sửa collector, sensor và SSH — 20/10/2026

Nền: `shiptoer/Cisco_Harness_Agent`, commit `2d0dd5250e49442d43d8eed148e2fc4a92821302`.
Nhánh làm việc: `tv1/collector-sensor-hardening`. Chưa commit, push hay merge lên GitHub.
Không quét mạng, không đăng nhập thiết bị thật, không apply cấu hình trong quá trình sửa.

## Thay đổi và giới hạn

| Hạng mục | Đã làm | Giới hạn cần biết |
|---|---|---|
| VLAN database | Hợp nhất VLAN chỉ có trong `show vlan brief`; giữ running-config nếu tên xung đột; ghi nguồn bổ sung và chênh lệch trong meta | Bảng VLAN có thể chứa VLAN suspended; context hiện chưa mô hình hóa trạng thái suspended |
| Trunk VLAN | Xử lý tuần tự replace/add/remove/except/all/none và lệnh `no`; mặc định IOS là 1–4094; chặn dải đảo chiều/ngoài giới hạn trước khi khai triển | Chỉ nhắm Cisco IOS, không áp ngầm quy tắc này cho NX-OS |
| Context security | Thêm `collected_security` lưu quan sát VRF, VTY ACL, AAA và danh mục phần chưa mô hình hóa; nằm trong JSON được hash, không chỉ file meta | Đây là quan sát có cấu trúc, **chưa phải mô hình forwarding VRF/AAA đầy đủ**. Gate/apply chặn thao tác liên quan miền chưa hỗ trợ: VRF/routing, AAA/VTY, ACL. Thao tác không liên quan (ví dụ tạo VLAN) vẫn được kiểm và cho phép theo gate bình thường |
| Sensor ngữ nghĩa | Bộ command contract độc lập Jinja đối chiếu từng lệnh, thứ tự, số lần xuất hiện với thao tác đã duyệt | Đo tính đúng của render; không chứng minh intent ngôn ngữ tự nhiên đúng hoặc mạng hội tụ. B4 cưỡng chế, B1–B3 shadow; B0 không có approved tool calls để đối chiếu |
| Apply | Chạy lại syntax/CIS và command contract trước kết nối; thiếu staged_ops hoặc lệnh sai thì từ chối | Gói cũ chỉ có candidate mà thiếu staged_ops không còn apply được. Giữ giới hạn tin cậy process cùng user của thiết kế cũ |
| SSH | Bật strict host-key verification; known_hosts hệ thống hoặc file do người vận hành chỉ định; thuật toán legacy chỉ bật theo từng thiết bị; thêm auth/banner timeout | Cần thiết lập host key đã xác minh và cấu hình legacy riêng cho IOL cũ; chưa thử SSH/EVE-NG thật |
| Độ bền | Chặn hostname có path traversal, lỗi parser trả thông báo đã lọc, giới hạn độ lớn/range; đóng kết nối khi kiểm privileged EXEC thất bại; test lazy-import chạy trong process riêng | Không tự retry/authenticate hàng loạt; lỗi thu thập dừng an toàn |
| Discovery | Module riêng, chỉ TCP 22 trên CIDR RFC1918 được chỉ định, tối đa 256 địa chỉ, 8 workers, timeout giới hạn | Không tự sửa inventory, không suy đoán role/site/management interface. Cổng mở chưa chứng minh đăng nhập được |
| Nhận diện OS | `show version` vào allowlist; xác minh họ Cisco IOS; OS không xác minh được bị từ chối khi có output | `ios`/`iosv`/`iosvl2` vẫn do inventory khai. Dữ liệu offline cũ thiếu show version được ghi `not_collected`, không coi đã xác minh |
| Batfish | Giữ trạng thái skipped trung thực; `CHA_REQUIRE_BATFISH=1` khiến thiếu backend thành lỗi chặn | **Chưa tích hợp backend Batfish hoạt động**. Không có container/service hay snapshot đầy đủ để kiểm nghiệm; không dùng test giả để tuyên bố hỗ trợ |

Không thay dependency hoặc workflow CI. Contract có trường tùy chọn mới, schema JSON đã tái sinh;
context cũ vẫn nạp được. Thay đổi sensor là đề xuất nghiên cứu D-067, cần GVHD duyệt trước khi dùng
số đo mới thay số liệu ablation đã đăng ký.

## Kiểm chứng

- Trước sửa: 249 test đạt, 1 lỗi phụ thuộc thứ tự import khi chạy collector + apply + mutation với SSH extras.
- Syntax/CIS cũ: 22/27 đột biến bị bắt. Giữ phép đo này để không trộn kết quả.
- Command contract mới: 27/27 đột biến bị bắt; cả 11 tool production đều qua đối chứng.
- Kết quả toàn bộ test, kiểm thử tập trung, lint và dry-run: xem `verification/` trong gói bàn giao.
- Tất cả test mạng dùng mock; dry-run dùng fake_dsh, không gọi LLM và không SSH thiết bị thật.
- 27/27 là mutation score trên tập cố định, không phải “độ chính xác mạng 100%”.

## Áp patch trên Windows, giữ nguyên thư mục đang làm

Giải nén gói bàn giao. `hardening.patch` và thư mục `Cisco_Harness_Agent/` là hai cách dùng cùng bản sửa;
không cần áp patch nếu bạn dùng trực tiếp thư mục source đã sửa.

Nếu muốn áp patch trong Git, từ repo gốc, dùng worktree riêng:

```powershell
git worktree add ..\Cisco_Harness_Agent-hardening -b review/hardening-20261020 2d0dd5250e49442d43d8eed148e2fc4a92821302
cd ..\Cisco_Harness_Agent-hardening
git apply --check C:\duong-dan\goi-ban-giao\hardening.patch
git apply C:\duong-dan\goi-ban-giao\hardening.patch
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,demo]"
.\.venv\Scripts\python.exe -m pytest -n auto
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe scripts\run_cases.py --config config\fake.toml --level B4 --out runs-hardening
```

Nếu `git apply --check` lỗi, dừng và xem diff; không ép ghi đè. Chưa chạy apply/commit lên thiết bị ở bước này.

## Chuyển cấu hình SSH

1. Xác minh host key qua console EVE-NG hoặc nguồn tin cậy độc lập, rồi đưa public host key vào file
   OpenSSH known_hosts. Không tự chấp nhận key chỉ vì nó vừa được mạng trả về.
2. Có thể dùng file known_hosts riêng:

```powershell
$env:COLLECTOR_KNOWN_HOSTS = "$env:USERPROFILE\.ssh\known_hosts_lab"
```

3. Với image IOL cũ của lab cần SHA-1/RSA, thêm `"legacy_ssh": true` vào đúng thiết bị trong inventory.
   Mặc định là false; không sửa thiết lập toàn cục của Paramiko. Host key vẫn phải xác minh khi bật legacy.
4. Dùng lại cách nhập `COLLECTOR_USER`/`COLLECTOR_PASSWORD` từ biến môi trường trong hướng dẫn cũ.
   Không đặt mật khẩu trong inventory, argv hoặc patch.
5. Thu thập như cũ: `python -m harness_agent.collector --ssh --save-raw`.
   Nếu thiết bị từ chối `show version` hoặc kết quả không nhận diện là Cisco IOS, kiểm lại quyền/OS.

## Discovery tùy chọn

Chỉ chạy khi subnet bên dưới đúng phạm vi lab bạn quản lý:

```powershell
python -m harness_agent.collector.discover --cidr 192.168.137.0/24
```

Kết quả chỉ có IP, trạng thái TCP/SSH và vendor unknown; sau đó chọn thiết bị để khai inventory.
Không có bước quét tự động khi chạy collector thông thường.

## Batfish và bước kiểm nghiệm còn lại

```powershell
$env:CHA_REQUIRE_BATFISH = "1"
```

Với bản hiện tại lệnh này **sẽ chặn B4/apply** vì backend chưa có, đúng mục đích fail-closed.
Không bật biến này nếu đang chạy thí nghiệm syntax/CIS/intent và chấp nhận báo Batfish skipped.
Bước tiếp theo cần service Batfish thực, snapshot đầy đủ trước/sau thay đổi, kiểm parse/init issues và
undefined references; delta CLI riêng lẻ không đủ để kết luận reachability. Chưa có bằng chứng lab cho phần này.

Nguồn kỹ thuật đã đối chiếu:
- Cisco IOS VLAN trunking: https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst3650/software/release/3se/vlan/configuration_guide/b_vlan_3se_3650_cg/b_vlan_3se_3650_cg_chapter_0100.html
- Batfish snapshots: https://batfish.readthedocs.io/en/stable/notebooks/interacting.html
- API Netmiko 4.8.0 và Paramiko 4.0.0 kiểm trực tiếp trong các package đã cài ở môi trường test.
