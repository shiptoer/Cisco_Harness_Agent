# Apply có kiểm soát (`cha-apply`)

Trạng thái: ĐỀ XUẤT (D-064). Code: `src/harness_agent/apply/`. Test: `tests/test_apply.py` (chỉ transport giả;
`NetmikoTransport` chưa chạy với Netmiko thật).

## 1. Mục đích

Đẩy cấu hình **đã được người duyệt** của một lượt chạy xuống **một** thiết bị lab qua SSH, để demo trọn vòng
intent → change package → duyệt → thiết bị. Đây là đường duy nhất trong repo gửi cấu hình xuống thiết bị
(CLAUDE.md §10). Nó không thêm control nào cho phép đo B0–B4 và không bao giờ chạy tự động.

- Tuỳ chọn, mặc định **dry-run** (in lệnh, không kết nối).
- Commit cần `--commit --confirm <run_id>`: người vận hành gõ lại đúng run_id của lượt đã duyệt.
- Người chịu trách nhiệm là người duyệt trên UI (bản ghi `human_review`, actor `reviewer`) cộng người gõ lệnh.
- **Không dùng trong lô đo.** Runner, evaluator, corpus, UI, web, harness và `scripts/` không import module
  này (test quét mã nguồn); lượt của lô đo không có `human_review` nên không qua điều kiện tiên quyết.

## 2. Điều kiện tiên quyết (`ApplyPlan.from_run`, từ chối = mã thoát 2)

| # | Điều kiện | Nguồn |
| --- | --- | --- |
| 1 | `result.json`: `status == "ok"`, `verdict == "accept"`, `leak_in_package is False`, `leak_scan_complete is True` | như `UiApp.review_blocker` |
| 1b | `result.json.level` là B3 hoặc B4 (thiếu → từ chối): `.cfg` của B0 là text tự do của model, B1/B2 chưa qua gate (iii) | |
| 2 | `verify_chain(trace.jsonl)` là `ok`/`unsigned`; `broken`/`bad_signature` bị từ chối | `harness/trace.py` (D-060) |
| 3 | Bản `human_review` ĐẦU TIÊN của actor `reviewer` có `decision == "approve"` | như `UiApp._review` |
| 4 | sha256 của `candidate/<device>.cfg` trùng `candidates[device]` của bản duyệt | như `UiApp.export_cfg` |
| 5 | Thiết bị có trong inventory (`lab/<lab>/inventory.json`) và trong bản duyệt | `collector.fetch.load_inventory` |
| 6 | Cấu hình không có khối `interface <mgmt_interface>` | rollback `default interface` sẽ cắt phiên SSH |
| 6b | File tách dòng CHỈ ở LF; mỗi dòng chỉ gồm dấu cách thụt đầu rồi ký tự ASCII in được 0x20–0x7E (không tab, CR, Ctrl-Z, DEL, C1, NBSP, U+2028/2029) | Ctrl-Z (0x1A) ở chế độ cấu hình IOS là `end`: phần sau chạy như lệnh EXEC (`write memory`) |
| 7 | Mỗi dòng khớp ALLOWLIST dạng lệnh của 11 template, ĐÚNG hình dạng template sinh ra, thành phần lấy từ contract/registry (`InterfaceName`, `VlanName`, `ACL_NAME_PATTERN`, `LIST_NAME_PATTERN` + `AuthMethod`, `SAFE_TEXT` của description, endpoint/cổng ACL, IP dotted-quad), chỉ lớp ký tự tường minh (`plan.ALLOWED_TOP`, `plan.ALLOWED_CHILD`): lệnh cấp 1 ở cấp 1, dòng con chỉ trong khối đang mở đúng loại, `exit` chỉ khi có khối mở, `end` chỉ ở cuối; lỗi chỉ nêu số dòng | một `exit` thừa đưa phiên về EXEC, khi đó `wr`/`wri mem` là lệnh EXEC; denylist theo từ không chặn được viết tắt |
| 8 | Context (đường dẫn trong `case_start` hoặc `--context`) có sha256 trùng `case_start.context_sha256` | secret của context để che |

Ngay trước commit, kế hoạch được LẬP LẠI bằng `ApplyPlan.from_run` từ run-dir, inventory và context đã ghi trong
kế hoạch (mọi điều kiện trên, gồm bản duyệt và sha256), và phải trùng kế hoạch được truyền vào (dòng, sha256, thiết
bị, host, run_id): một `ApplyPlan` dựng tay hay một bản duyệt bị xoá sau khi lập kế hoạch không đẩy được gì.
Placeholder `<SECRET:ref>` cần biến `CHA_SECRET_<REF>`; thiếu, rỗng hay có ký tự điều khiển → từ chối **trước
khi kết nối**.

## 3. Luồng

```text
cha-apply ──► ApplyPlan.from_run (điều kiện 1–8) ──► dry-run? ── có ──► in dòng, trace apply_dry_run, thoát 0
                                                         │
                                                        không: --confirm == run_id? ── không ──► từ chối (2)
                                                         │
                     kiểm lại .cfg + chuỗi; thay <SECRET:ref> từ env (thiếu → từ chối, chưa kết nối)
                                                         │
connect ─► show running-config ─► có `hostname <dev>` và dòng cuối `end`? ── không ──► apply_error, từ chối (2)
        ─► (cấu hình có `vlan N`) show vlan brief ─► đọc được bảng VLAN? ── không ──► apply_error, từ chối (2)
        ─► apply/<dev>.pre.cfg ─► trace apply_start (sha256 pre, vlans_checked, warnings)
        ─► send_config(dòng của .cfg, bỏ `end` và `!`, giữ `exit`)
        ─► show running-config (+ show vlan brief) ─► apply/<dev>.post.cfg ─► verify
                 │ đủ và không có dòng `%`                    │ thiếu dòng hoặc thiết bị báo lỗi `%`
                 ▼                                            ▼
        trace apply_done (sha256 post, số dòng +/-)    rollback_lines(pre) ─► send_config ─► show running-config
        thoát 0                                        ─► apply/<dev>.rollback.cfg ─► trace apply_rollback
                                                       (dòng thiếu, lệnh rollback, số dòng còn lệch so với pre)
                                                       thoát 1
lỗi transport: trước apply_start → từ chối (2); sau đó → trace apply_error, "trạng thái không xác định" (1)
```

Snapshot `pre` phải có `hostname <device>` (không phân biệt hoa thường) và kết thúc bằng `end`: một output rỗng,
bị cắt hay của thiết bị khác mà vẫn dùng làm `pre` thì rollback sẽ `no` cả những thứ vốn có. Output của
`send_config` có dòng bắt đầu bằng `%` (IOS từ chối một lệnh) cũng đi đường rollback, kể cả khi verify đủ (một dòng
bị từ chối mà dòng giống hệt đã có sẵn trên thiết bị). Report và CLI ghi `device_errors` và
`residual_diff_lines`.

**VLAN ngoài running-config** (sửa 17/10/2026): switch ở chế độ VTP server/client giữ VLAN trong vlan.dat và
KHÔNG in `vlan N` trong running-config. Khi cấu hình có khối `vlan N`, commit đọc `show vlan brief` (lệnh EXEC thứ
hai mà `NetmikoTransport` cho phép, `transport.ALLOWED_SHOWS`) ngay sau snapshot `pre` và sau khi gửi. Bảng phải có
tiêu đề `VLAN Name Status Ports` và VLAN 1, mọi dòng bắt đầu bằng chữ số phải đọc được (`execute.vlan_table`); không
thì từ chối trước khi gửi (`pre_vlan_table_invalid`). VLAN không có trong running-config sau khi áp được kiểm bằng
bảng: VLAN N `active` và `name X` trùng tên trong bảng.

**Cảnh báo khoá đăng nhập**: cấu hình có `aaa new-model` mà thiết bị không có `username` nào thì phiên SSH/console
mới có thể bị từ chối. Dry-run xét context của lượt chạy (`ApplyPlan.context_local_users`), commit xét
running-config trước khi áp (dòng cấp 1 `username …`). Chỉ cảnh báo (stderr `[CẢNH BÁO]`, `ApplyReport.warnings`,
trường `warnings` của `apply_dry_run`/`apply_start`), không chặn: phiên đang áp vẫn giữ và rollback gửi
`no aaa new-model` nếu verify thiếu.

Mọi bản ghi trace có actor `apply`, không ký, nối đuôi chuỗi sau `human_review` (D-060 cho phép đúng hai actor
này ở đuôi không ký). Backup của lần áp trước không bị ghi đè (từ chối commit); muốn áp lại, chuyển `apply/` đi.

**Kiểm sau khi áp** (`verify`): chuẩn hoá (bỏ khoảng trắng đầu/cuối, gộp khoảng trắng), bỏ `exit`, `end`, `!`.
Lệnh cấp 1 phải là một dòng cấp 1 của running-config; dòng con phải nằm trong **đúng khối cha** (một
`switchport access vlan 30` dưới interface khác không tính). Dòng `no X` đạt khi X vắng mặt (trong khối cha, với
dòng con). Dòng có placeholder chỉ kiểm phần đứng trước placeholder (thiết bị hiển thị key đã mã hoá).

## 4. Rollback (best-effort)

Dựa trên running-config TRƯỚC khi áp (giữ trong bộ nhớ), theo thứ tự ngược của cấu hình:

| Trong cấu hình | Có trước khi áp? | Lệnh rollback |
| --- | --- | --- |
| `interface X` + dòng con | có | `default interface X`, rồi `interface X` + các dòng con cũ (+ `no shutdown` nếu khối cũ không có `shutdown`), `exit` |
| `interface X` (SVI mới) | không | `no interface X` |
| `vlan N` | không trong running-config, bảng `show vlan brief` trước khi áp XÁC NHẬN chưa có | `no vlan N` |
| `vlan N` | không trong running-config nhưng có trong bảng (VTP server/client) | không xoá; `vlan N`, ` name <tên cũ>`, `exit` nếu tên đổi (tên cũ phải đúng dạng `VlanName`) |
| `vlan N` | không có bảng (`rollback_lines` gọi không kèm `vlans`) | không gửi gì: không biết VLAN đã có hay chưa |
| lệnh cấp 1 khác (`ip route …`, `ip access-list …`, `aaa …`) | không | `no <lệnh>` |
| lệnh cấp 1 có khối con | có | không xoá khối; `no <dòng con mới>`, rồi gửi lại dòng con cũ |
| `line …` | (luôn có) | như dòng trên |
| `no X` | X có | gửi lại X (và dòng con cũ của X) |
| dòng có placeholder | không / có | `no <phần trước placeholder>` / gửi lại dòng cũ |

Giới hạn:
- Không phải `configure replace`: lệnh có hiệu ứng phụ (vd `aaa new-model` tự thêm dòng mặc định), thứ tự mục
  ACL, `default interface` trên interface có cấu hình ngoài running-config, không khôi phục được hoàn toàn.
  `apply_rollback.residual_diff_lines` cho biết còn bao nhiêu dòng lệch so với `.pre.cfg`.
- Phép kiểm là so nguyên văn đã chuẩn hoá: thiết bị hiển thị khác dạng render (`eq 80` → `eq www`) làm cấu hình
  thật ra đúng bị rollback. Hướng đó chỉ an toàn vì rollback không xoá thứ vốn có trên thiết bị: lệnh cấp 1 đã có
  trong running-config không bị `no`, và `no vlan N` chỉ gửi khi `show vlan brief` trước khi áp xác nhận VLAN N
  chưa có (trước bản sửa 17/10/2026, một VLAN vốn có trên switch VTP server/client, không in trong running-config,
  bị coi là "mới" và bị xoá).
- `switchport access vlan N` tới VLAN chưa có: IOS tự tạo VLAN N (kèm dòng `%`, đi đường rollback); rollback
  không xoá VLAN tự tạo đó.
- Một thiết bị một lần; không có giao dịch nhiều thiết bị.

**Phải kiểm trên lab trước khi tin rollback** (chưa kiểm, máy hiện thực không có thiết bị):
- *Số thứ tự ACL*: gửi lại mục ACL cũ với cùng seq trên một ACL có sẵn cho `% Duplicate sequence number` (vô hại,
  mục cũ vẫn còn); nhưng nếu cấu hình mới THAY một seq có sẵn bằng nội dung khác thì IOS từ chối dòng mới (dòng
  `%` → rollback), và `no <seq> …` của rollback phải đúng nguyên văn mục mới. Kiểm `show ip access-lists` sau
  rollback trên IOL L2 và router.
- *`default interface` và trạng thái shutdown*: trên router, `default interface` để interface ở `shutdown`;
  running-config không in `no shutdown` cho interface đang bật, nên rollback thêm `no shutdown` khi khối cũ
  không có `shutdown`. Trên switch IOL L2 mặc định là bật; kiểm cả hai, và kiểm `default interface` không đụng
  `switchport` của cổng trunk ngoài phạm vi (CDP/STP có thể đổi trạng thái tạm thời).
- Mất kết nối giữa lúc gửi: không rollback được (không có phiên); người vận hành so với `.pre.cfg` và khôi phục tay.

## 5. Secret

- `<SECRET:ref>` chỉ được thay bằng `CHA_SECRET_<REF>` (ref viết hoa, ký tự khác chữ/số → `_`) trong danh sách
  dòng gửi cho transport, ngay trước khi kết nối. Dry-run không đọc giá trị, chỉ in tên biến cần đặt.
- Dòng ghi chú `!` không được gửi: IOS bỏ qua chúng, và không có lý do mang giá trị thật trong một ghi chú.
- Backup, diff, dòng thiếu, lệnh rollback và thông báo lỗi đi qua `collector.parse.sanitize` (xoá giá trị bí mật
  dạng IOS) rồi `harness.leak.redact_value` với secret của context **cộng** các giá trị đã thay.
- `TransportError` chỉ mang tên lớp lỗi của Netmiko/paramiko: thông báo gốc có thể chép lại lệnh đã gửi.

## 6. Những gì KHÔNG làm

- Không `configure replace`, không archive/rollback của IOS.
- Không `write memory` / `copy run start`: người vận hành kiểm rồi tự lưu.
- Không giao dịch nhiều thiết bị, không áp song song.
- Không gửi `enable`, không đọc enable secret: tài khoản phải ở privilege 15.
- Không dùng trong lô đo, không gọi từ runner/evaluator/UI, không có nút trên UI.
- Không kiểm host key (giống collector: lab dựng lại sau mỗi lần Wipe). Không dùng ngoài lab cô lập khi chưa sửa
  điểm này.
