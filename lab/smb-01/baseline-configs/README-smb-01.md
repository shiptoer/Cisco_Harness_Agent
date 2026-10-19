# Lab smb-01 — thiết kế chốt (12/10/2026)

Mô phỏng mạng doanh nghiệp nhỏ (khoảng 60–80 người): một trụ sở, một chi nhánh.
Nguồn chuẩn cho lab, cấu hình nền và context `smb-01`. Mọi thay đổi sau ngày này đi qua DECISIONS.md.

## 1. Ràng buộc thiết kế (vì sao lab trông như thế này)

| Ràng buộc | Nguồn | Hệ quả trong lab |
|---|---|---|
| Chỉ Cisco IOS, 5 loại cấu hình: VLAN, interface, static route, ACL, AAA | Scope Ch1–2 | Không NAT, không OSPF/BGP, không HSRP, không DHCP server |
| Contract hiện tại không nhận subinterface (`Ethernet0/1.110`) | Kiểm code 12/10 | Không dùng router-on-a-stick; chi nhánh dùng switch L3 làm gateway |
| Contract không có trường VRF | Kiểm code 12/10 | VRF chỉ dùng cho cổng quản lý; collector ghi danh sách vào `.meta.json` |
| Lab chạy Cisco IOL trên EVE-NG, máy 20 GB RAM | Hạ tầng nhóm | 8 node, tên cổng `EthernetX/Y` |
| Collector phải SSH được dù agent làm hỏng mạng dữ liệu | An toàn thực nghiệm | Mạng quản lý out-of-band riêng (Cloud0) |

## 2. Topology

```text
                         [ISP]  (mô phỏng Internet, KHÔNG quản lý)
                           │ e0/0 203.0.113.2
                           │ 203.0.113.0/30
                     e0/1  │ 203.0.113.1
                       [R-EDGE1] e0/2 10.255.0.5 ──10.255.0.4/30── e0/0 10.255.0.6 [R-BR1]
                     e0/0  │ 10.255.0.1                                     e0/1 │ 10.255.0.9
                           │ 10.255.0.0/30                                       │ 10.255.0.8/30
                     e0/0  │ 10.255.0.2 (routed)                           e0/0 │ 10.255.0.10 (routed)
                       [CORE1]  switch L3, SVI cho VLAN trụ sở                [ACC-BR1] switch L3,
                   e0/1 │  e0/2 │  e0/3 │  (trunk, native 999)                SVI 110 và 199
                     [ACC1] [ACC2] [ACC3]
```

Quản lý OOB: R-EDGE1 e0/3, R-BR1 e0/3, CORE1/ACC1/ACC2/ACC3/ACC-BR1 e1/3 → các object `Management(Cloud0)` (cùng một mạng `192.168.137.0/24`). ISP không nối.

## 3. Thiết bị

| Hostname | Vai trò | Image | Portgroup | IP quản lý | VRF MGMT |
|---|---|---|---|---|---|
| R-EDGE1 | Router biên trụ sở | IOL L3 | 2 (e1/0–e1/3 không dùng, `shutdown`) | 192.168.137.11 (e0/3) | Có |
| R-BR1 | Router chi nhánh | IOL L3 | 1 | 192.168.137.12 (e0/3) | Có |
| CORE1 | Switch L3 lõi trụ sở | IOL L2 | 2 | 192.168.137.21 (e1/3) | Có |
| ACC1 | Switch truy cập tầng 1 | IOL L2 | 2 | 192.168.137.31 (e1/3) | Không (không bật định tuyến) |
| ACC2 | Switch truy cập tầng 2 | IOL L2 | 2 | 192.168.137.32 (e1/3) | Không |
| ACC3 | Switch phòng server | IOL L2 | 2 | 192.168.137.33 (e1/3) | Không |
| ACC-BR1 | Switch L3 chi nhánh | IOL L2 | 2 | 192.168.137.34 (e1/3) | Có |
| ISP | Mô phỏng Internet | IOL L3 | 1 | — | — |

### Phiên bản image (ghi 12/10/2026, `show version`)

| Loại | Thiết bị | Image | Phiên bản |
|---|---|---|---|
| IOL L3 | R-EDGE1, R-BR1, ISP | `I86BI_LINUX-ADVENTERPRISEK9-M` | 15.4(2)T4, DEVELOPMENT TEST SOFTWARE |
| IOL L2 | CORE1, ACC1–3, ACC-BR1 | `I86BI_LINUXL2-ADVENTERPRISEK9-M` | 15.2(CML_NIGHTLY_20190423) FLO_DSGS7, EARLY DEPLOYMENT DEVELOPMENT BUILD |

Cả hai là bản phát triển/kiểm thử nội bộ, không phải bản phát hành chính thức: ghi vào threats to validity (hành vi có thể khác IOS thật, ví dụ L2 ở mục 10).

Platform trong `lab/smb-01/inventory.json` (sửa 17/10/2026, D-055): image IOL L2 (CORE1, ACC1–3, ACC-BR1) khai `iosvl2`, IOL L3 khai `ios`; trước ngày này cả bảy khai `ios`, nên snapshot `smb-01-collected-ref.json` (giữ nguyên byte) mang `ios` cho switch, dùng bản dẫn xuất `smb-01-collected-ref-l2.json` khi cần platform đúng.

Quy tắc: thiết bị nào bật `ip routing` thì cổng quản lý nằm trong VRF MGMT, để mạng người dùng không định tuyến được sang mạng quản lý.

## 4. VLAN và địa chỉ

| VLAN | Tên | Subnet | Gateway | Ở đâu |
|---|---|---|---|---|
| 10 | STAFF | 10.10.10.0/24 | 10.10.10.1 (CORE1) | ACC1 e0/1 |
| 20 | SALES | 10.10.20.0/24 | 10.10.20.1 (CORE1) | ACC1 e0/2 |
| 30 | FINANCE | 10.10.30.0/24 | 10.10.30.1 (CORE1) | ACC2 e0/1 |
| 40 | HR | 10.10.40.0/24 | 10.10.40.1 (CORE1) | ACC2 e0/2 |
| 50 | SERVERS | 10.10.50.0/24 | 10.10.50.1 (CORE1) | ACC3 e0/1–e0/3 |
| 60 | GUEST | 10.10.60.0/24 | 10.10.60.1 (CORE1) | ACC1 e0/3 |
| 99 | MGMT (in-band) | 10.10.99.0/24 | 10.10.99.1 (CORE1) | SVI trên ACC1–3: .11, .12, .13 |
| 110 | BR-STAFF | 10.20.10.0/24 | 10.20.10.1 (ACC-BR1) | ACC-BR1 e0/1 |
| 199 | BR-MGMT (in-band) | 10.20.99.0/24 | 10.20.99.1 (ACC-BR1) | — |
| 999 | NATIVE-UNUSED | — | — | native VLAN của trunk, cổng trống |

Server: AAA/TACACS+ 10.10.50.10, máy chủ tài chính 10.10.50.20, file server 10.10.50.30.
VLAN ID còn trống (70, 80, …) và cổng trống (ACC1/ACC2 e1/0–e1/2) để corpus sinh intent hợp lệ.

## 5. Static route

| Thiết bị | Đích | Next-hop |
|---|---|---|
| R-EDGE1 | 10.10.0.0/16 | 10.255.0.2 (CORE1) |
| R-EDGE1 | 10.20.0.0/16 | 10.255.0.6 (R-BR1) |
| R-EDGE1 | 10.255.0.8/30 | 10.255.0.6 (link chuyển tiếp chi nhánh) |
| R-EDGE1 | 0.0.0.0/0 | 203.0.113.2 (ISP) |
| CORE1 | 0.0.0.0/0 | 10.255.0.1 |
| R-BR1 | 10.20.0.0/16 | 10.255.0.10 (ACC-BR1) |
| R-BR1 | 0.0.0.0/0 | 10.255.0.5 |
| ACC-BR1 | 0.0.0.0/0 | 10.255.0.9 |
| ISP | 10.0.0.0/8 | 203.0.113.1 (không NAT, đơn giản hoá) |

## 6. ACL và AAA

| Tên | Ở đâu | Nội dung |
|---|---|---|
| ACL-GUEST-IN | CORE1 SVI 60, in | GUEST không vào 10.0.0.0/8, chỉ ra ngoài |
| ACL-SERVERS-OUT | CORE1 SVI 50, out | Chỉ VLAN 30 tới 10.10.50.20 cổng 443 |
| ACL-MGMT-VTY | VTY mọi thiết bị | Chỉ 192.168.137.1, 10.10.99.0/24, 10.20.99.0/24 |
| AAA | Mọi thiết bị quản lý | TACACS+ 10.10.50.10, dự phòng local; console xác thực local |

## 7. Policy cho harness (`smb-01.policy.json`)

- Interface được bảo vệ: R-EDGE1 e0/0–e0/3; R-BR1 e0/0, e0/1, e0/3; CORE1 e0/0–e0/3, e1/3; ACC1–3 e0/0, e1/3; ACC-BR1 e0/0, e1/3.
- Mạng quản lý: 192.168.137.0/24, 10.10.99.0/24, 10.20.99.0/24, 10.255.0.0/30, 10.255.0.4/30, 10.255.0.8/30.
- VLAN dành riêng: 1, 99, 199, 999.
- Giới hạn blast radius: như `campus-small-01`.
- Secret: enable secret, mật khẩu `netadmin`, TACACS key. Trong repo chỉ là placeholder `{{...}}`.

## 8. Giới hạn đã biết (ghi vào threats to validity)

- Không NAT, không firewall, không HSRP, không DHCP: đơn giản hoá so với doanh nghiệp thật, vì ngoài scope.
- Quản lý OOB là mức best practice, cao hơn mức phổ biến ở doanh nghiệp nhỏ.
- Image IOL là bản phát triển/kiểm thử nội bộ (xem mục 3), hành vi có thể khác IOS phát hành.
- SSH dùng thuật toán cũ (SHA-1) do giới hạn của image IOL; chỉ chấp nhận được vì lab cô lập.
- Bản lab dùng chung một mật khẩu cho mọi secret; không dùng lab này cho thực nghiệm leak theo canary.
- Contract chưa mô tả subinterface và VRF (đề xuất D-048, sau Review).
- Vlan199 (BR-MGMT) trên ACC-BR1 ở trạng thái `down/down` vì không có cổng thành viên: chi nhánh không có host
  quản trị trong lab. Không ảnh hưởng SSH, vì mọi thiết bị được quản lý qua cổng OOB.
- Mọi switch chạy `vtp mode transparent`: VLAN nằm trong running-config, nên nạp startup-config sau khi Wipe vẫn tạo đủ VLAN, và collector đọc được VLAN từ `show running-config`.

## 9. Hai bộ file

| File | Dùng cho | Secret |
|---|---|---|
| `smb-01-FINAL-lab.zip` | Nạp lên EVE-NG | Mật khẩu lab thật (giữ ngoài repo, xem trình quản lý mật khẩu của nhóm) |
| `smb-01-FINAL-repo.zip` | `lab/smb-01/baseline-configs/` trong repo, ground truth cho collector | Placeholder |

## 10. Ghi nhận khi dựng lab (12/10/2026)

| # | Hiện tượng | Nguyên nhân | Cách xử lý trong bản chốt |
|---|---|---|---|
| L1 | Sau Wipe, switch chỉ còn VLAN 1 và 999; mọi SVI down | VTP mode server: VLAN lấy từ `vlan.dat`, bỏ qua các dòng `vlan` trong startup-config | `vtp mode transparent` trên mọi switch |
| L2 | CORE1 không định tuyến; có `L 10.255.0.2/32` nhưng thiếu `C 10.255.0.0/30`; default route không được cài | `ip routing` trong startup-config không có hiệu lực lúc boot trên IOL L2; bật tay sau đó thì route nối trực tiếp của cổng routed không được tạo lại | Bật `ip routing` rồi `shutdown`/`no shutdown` cổng e0/0. Đang kiểm lỗi có lặp lại sau khởi động lại không |
| L3 | Ping từ link chuyển tiếp chi nhánh `10.255.0.8/30` không có đường về | R-EDGE1 thiếu route cho `10.255.0.8/30` (không nằm trong `10.20.0.0/16`) | Thêm route `BR-TRANSIT` trên R-EDGE1 |
| L4 | Contract không nhận subinterface `Ethernet0/1.110` | Mẫu tên interface trong contract và schema tool | Bỏ router-on-a-stick; ACC-BR1 làm gateway L3 |
| L5 | TFTP timeout, duplex mismatch, banner thừa ký tự, `line vty 5 15` bị từ chối | Đặc điểm IOL và lỗi cú pháp | `no service config`, `duplex full`, banner dùng `#`, chỉ `line vty 0 4` |
| L6 | Console vào `>` dù user privilege 15 | `aaa authorization exec` không áp cho console | Dùng `enable`; SSH vào thẳng `#` |
| L7 | OpenSSH trên Windows không bắt tay được với thiết bị | IOL chỉ hỗ trợ `diffie-hellman-group14-sha1`, `ssh-rsa`, `hmac-sha1` | Ngoại lệ trong `~/.ssh/config` chỉ cho `192.168.137.*` |
| L8 (13/10) | Các switch thấy nhau qua cổng quản lý e1/3 (CDP quảng bá qua mạng OOB) dù baseline có `no cdp enable` | Giả thuyết, khớp dữ liệu: `no cdp enable` đứng TRƯỚC `no switchport`; `no switchport` đưa interface về mặc định nên xoá dòng đó. CORE1 hết bị sau khi e1/3 được cấu hình lại bằng tay | Đặt `no cdp enable` SAU `no switchport` (5 switch); `check_lab.py` báo mọi lệnh đứng trước `no switchport` trong cùng interface, trừ `description` |
| L9 (13/10) | R-EDGE1 thật có 8 cổng, thiết kế ghi 4 | Node EVE-NG có 2 portgroup | Giữ node như cũ; baseline thêm e1/0–e1/3 `shutdown` (64 interface) |

Kiểm chứng đã đạt (12/10): 7/7 IP quản lý ping và SSH được; mạng người dùng (CORE1 nguồn Vlan10) **không** tới được mạng quản lý (0/5, VRF cô lập đúng); chi nhánh Vlan110 tới trụ sở Vlan10 và tới ISP 198.51.100.1 đều 5/5.

Bài học quy trình: mọi bộ cấu hình phải qua `check_lab.py` (route đi và về, next-hop, VLAN, thứ tự lệnh quanh `no switchport`) trước khi nạp; sau khi nạp phải kiểm theo 4 tầng (quản lý, link, định tuyến đầu-cuối, bảo mật).
