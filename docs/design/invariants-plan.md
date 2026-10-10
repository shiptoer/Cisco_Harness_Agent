# Kế hoạch invariants: control (i) phần domain (D-005, task 2.2)

| | |
|---|---|
| Nhánh | `tv2/invariants`, tách từ `main` tại `ad35e77` (merge PR #13, contract v0.2) |
| Ngày phân tích | 08/10/2026 |
| Quyết định liên quan | D-005 (tách validation cấu trúc và invariants, NHÓM CHỐT), D-042 (một schema context), D-040 (corpus, tập corrupted), D-017 (kết cục) |
| Trạng thái | Kế hoạch được nhóm duyệt ngày 08/10/2026 kèm các quyết định ở mục 0; danh sách 16 mã ghi ở D-043 (ĐỀ XUẤT) |

Mọi số liệu dưới đây đo bằng một bản nháp chạy trong bộ nhớ (ngoài repo) trên `data/contexts/campus-small-01.json`
và `examples/valid/network_context.json`. Không in giá trị secret nào: chỉ tên khoá.

## 0. Quyết định đã duyệt (08/10/2026)

Các quyết định dưới đây THAY phần tương ứng ở các mục sau.

- Q1: `CTX_MGMT_UNREACHABLE` dùng dạng nới: thiết bị có interface nằm trong `mgmt_prefix`, HOẶC có static route cụ thể
  (không phải default) bao trùm `mgmt_prefix`.
- Q2: không đọc file zip ngoài repo; ánh xạ mutator → invariant làm ở bước corpus (mục 10).
- Q3: `Violation` có thêm `path`, dạng `devices[1].interfaces[3].acl_in` (chỉ số trong ngoặc vuông, tên khoá sau dấu
  chấm). Lỗi contract ở runner dùng cùng dạng (`format_loc`).
- Q4: giữ đủ 16 mã. `CTX_SUBNET_OVERLAP` chỉ xét trong CÙNG một thiết bị: hai đầu một link dùng chung subnet là hợp lệ.
- Q5: giữ `reject` / `stopped_by=context_validation`, event `context_invalid`.
- `detail` không chứa free-text (description, banner_motd, remark) lẫn giá trị secret: chỉ tên, mã, số VLAN, đường dẫn.
  Thêm khi hiện thực: nếu một tên trong context (vd `key_ref`) trùng giá trị secret thì cả `detail` bị thay bằng chuỗi
  che cố định.
- Runner gọi contract + invariants qua một hàm riêng, `runner/context_check.py::context_violations(path)`, để PR
  ablation sau này tắt được bước này cho B0/B1 mà không sửa `run_case`.

Hiện thực: commit `25da0e0` (models + test), commit (b) (runner, test tích hợp, plan, DECISIONS).

## 1. Vì sao

Contract v0.2 kiểm từng trường (kiểu, enum, dải giá trị, trùng lặp trong một danh sách). Nó không kiểm các trường có
KHỚP NHAU hay không: một cổng access trỏ tới VLAN không có, một ACL được áp nhưng không định nghĩa, một `key_ref`
không có trong `secrets`. Đó là invariants (D-005): `check_invariants(ctx) -> list[Violation]`, trả mọi vi phạm thay
vì raise, để trace ghi đủ và tập corrupted đo được tỉ lệ phát hiện (RQ2: context sai lệch thì phát hiện, hay silent
failure).

Hiện `models/invariants.py` chỉ `raise NotImplementedError`. Lúc chạy, harness dùng ba kiểm tra viết tay trong
`harness/context_store.py` (`CTX_DANGLING_VLAN`, `CTX_TRUNK_MISMATCH`, `CTX_DUP_IP`).

## 2. Đã đọc

`src/harness_agent/models/invariants.py` (stub), `models/context.py` (v0.2), `harness/context_store.py`
(`_index_and_validate`), `docs/design/contract-v0.2-plan.md` mục 3 và 12, DECISIONS D-005 và D-040,
`runner/pipeline.py` (bước 1 của `run_case`), `harness/server.py` (`main`), các test dùng context sai
(`tests/test_harness_pipeline.py::test_corrupted_context_rejected`, `tests/test_usage.py::test_context_rejected_before_any_stage_has_empty_usage`).

## 3. Danh sách invariant đề xuất

Đường dẫn ghi theo dạng `loc` của pydantic (chỉ số trong list, tên khoá trong dict), cùng định dạng với lỗi của
contract. Cột "Ví dụ vi phạm" là đúng đột biến đã thử: mỗi đột biến cho ĐÚNG MỘT vi phạm, đúng mã.

### 3.1. Ba kiểm tra đang có trong `context_store`

| Mã | Mô tả | Trường | Ví dụ vi phạm | File thật |
|---|---|---|---|---|
| `CTX_DANGLING_VLAN` | `access_vlan` của một interface phải có trong `vlans` của thiết bị | `interfaces[].access_vlan`, `vlans` | cổng access đầu tiên của SW2 → VLAN 30 | 0 |
| `CTX_TRUNK_MISMATCH` | mọi VLAN trong `trunk_allowed` phải có trong `vlans` của thiết bị | `interfaces[].trunk_allowed` | thêm 30 vào trunk của SW2 | 0 |
| `CTX_DUP_IP` | một địa chỉ IPv4 không dùng ở hai interface (trên toàn context) | `interfaces[].ipv4` | `Vlan99` của SW2 → `10.99.0.1/24` (trùng SW1) | 0 |

Khác `context_store`: `CTX_DANGLING_VLAN` hiện chỉ kiểm khi thiết bị có ít nhất một VLAN; bản mới kiểm mọi `access_vlan`
(một thiết bị không có VLAN nào mà có cổng gán VLAN cũng là vi phạm).

### 3.2. Tham chiếu

| Mã | Mô tả | Trường | Ví dụ vi phạm | File thật |
|---|---|---|---|---|
| `CTX_ACL_REF` | `acl_in` / `acl_out` phải là tên trong `acls` của thiết bị | `interfaces[].acl_in`, `.acl_out`, `acls` | R1 `GigabitEthernet0/0.acl_in` → `NO-SUCH-ACL` | 0 |
| `CTX_SECRET_REF` | `aaa.local_users[].secret_ref` và `aaa.tacacs_servers[].key_ref` phải là tên khoá trong `secrets` | `aaa`, `secrets` | `key_ref` của TAC1 trên R1 → `no_such_key` | 0 |
| `CTX_LOGIN_LIST_REF` | `aaa.vty_login_list` phải là tên trong `aaa.login_lists` | `aaa.vty_login_list` | R1 → `NO-LIST` | 0 |
| `CTX_LINK_REF` | hai đầu của mỗi `links[]` là thiết bị và interface có thật | `links[]` | `links.0.b_interface` → `GigabitEthernet0/9` | 0 |
| `CTX_PROTECTED_REF` | mỗi mục `policy.protected_interfaces` là thiết bị và interface có thật | `policy.protected_interfaces` | thêm `GigabitEthernet0/9` cho SW2 | 0 |
| `CTX_SVI_VLAN_MISSING` (thêm) | interface `VlanN` thì VLAN N phải có trên thiết bị | `interfaces[].name`, `vlans` | thêm `Vlan77` cho SW1 | 0 |

### 3.3. VLAN và policy

| Mã | Mô tả | Trường | Ví dụ vi phạm | File thật |
|---|---|---|---|---|
| `CTX_NATIVE_VLAN_MISSING` | `native_vlan` của trunk phải có trong `vlans` của thiết bị | `interfaces[].native_vlan` | native của trunk SW2 → 998 | 0 |
| `CTX_UNUSED_NOT_PARKED` | cổng `unused` có `access_vlan` thì phải là `policy.parking_vlan` (khi policy có `parking_vlan`) | `interfaces[].access_vlan`, `policy.parking_vlan` | cổng unused đầu tiên của SW2 → VLAN 10 | 0 |
| `CTX_RESERVED_VLAN_IN_USE` (thêm) | cổng access không được gán VLAN trong `policy.reserved_vlans` | `interfaces[].access_vlan` | thêm VLAN 1002 vào SW2 rồi gán cho một cổng access | 0 |

Cổng `unused` của router không có `access_vlan` nên không thuộc luật parking.

### 3.4. Địa chỉ và định tuyến

| Mã | Mô tả | Trường | Ví dụ vi phạm | File thật |
|---|---|---|---|---|
| `CTX_SUBNET_OVERLAP` (thêm) | hai interface có IP trên cùng thiết bị không được chồng subnet | `interfaces[].ipv4` | thêm `Loopback5 10.10.10.200/32` cho SW1 (nằm trong `Vlan10`) | 0 |
| `CTX_NEXT_HOP_UNREACHABLE` (thêm) | `next_hop` của static route phải nằm trong một subnet kết nối trực tiếp của thiết bị | `static_routes[].next_hop` | default route của SW1 → `10.0.13.1` | 0 |
| `CTX_GATEWAY_UNREACHABLE` (thêm) | `default_gateway` phải nằm trong một subnet của thiết bị | `default_gateway` | SW2 → `10.98.0.1` | 0 |
| `CTX_MGMT_UNREACHABLE` | khi policy có `mgmt_prefix`: thiết bị có interface nằm trong `mgmt_prefix`, **hoặc** có static route (không phải default) bao trùm `mgmt_prefix` | `interfaces[].ipv4`, `static_routes`, `policy.mgmt_prefix` | bỏ route `10.99.0.0/24` của R1 | 0 (sau khi nới, xem dưới) |

**`CTX_MGMT_UNREACHABLE` theo đúng câu chữ yêu cầu ("mỗi thiết bị có ít nhất một interface trong `mgmt_prefix`") vi
phạm ngay trên file thật**: R1 không có interface nào trong `10.99.0.0/24`. R1 được quản trị qua `Loopback0`
(`10.255.0.1/32`) và có route `10.99.0.0/24 via 10.0.12.2`. Tôi đề xuất dạng nới ở bảng trên (có interface trong
`mgmt_prefix` HOẶC có route cụ thể tới nó); dạng nới cho 0 vi phạm trên file thật. Cần nhóm chọn (câu hỏi Q1).

"(thêm)" là invariant ngoài danh sách tối thiểu, lấy từ docstring hiện có của `models/invariants.py` (next-hop, chồng
subnet, VLAN dành riêng) hoặc thấy khi đọc dữ liệu (SVI, gateway). Nhóm có thể bỏ bớt; mỗi cái độc lập.

Tổng: 16 mã. Trên `examples/valid/network_context.json`: 0 vi phạm.

## 4. Đối chiếu với 8 mutator của tập corrupted (D-040)

**Không làm được trong repo.** D-040 chỉ ghi "corrupted 150 (8 mutator)", không mô tả từng mutator; repo không có
`corpus_gen.py` hay mô tả nào khác (đã tìm "mutator", "corrupted", "đột biến" trong mọi file `.md`/`.py`). Theo D-040,
mô tả nằm trong file zip ngoài repo (`corpus-campus-small-01-seed491-prompt-vi.zip`). Muốn đối chiếu thì cần nhóm cho
phép đọc file đó (chỉ đọc), hoặc chép danh sách 8 mutator vào DECISIONS (câu hỏi Q2).

Điều nói được ngay, không cần danh sách mutator:

- **Bắt được:** mọi đột biến làm một tham chiếu trỏ vào khoảng trống (VLAN, ACL, secret, login list, interface, thiết
  bị không có), làm trùng IP, chồng subnet, hay làm next-hop/gateway/mgmt ra khỏi các subnet của thiết bị.
- **Không invariant nào bắt (silent failure mà RQ2 sẽ đo):** đột biến sai mà vẫn TỰ NHẤT QUÁN. Ví dụ:
  - đổi tên VLAN, đổi `description`, đổi `banner_motd`;
  - đổi IP của một interface sang địa chỉ khác còn trống trong cùng subnet, hoặc đổi cả subnet lẫn mọi thứ trỏ vào nó;
  - đổi `next_hop` sang một địa chỉ khác vẫn nằm trong subnet kết nối trực tiếp;
  - lật `action` của một dòng ACL, đổi thứ tự `seq`, đổi `dst_port`;
  - đổi `privilege` của user; đổi `login_lists` (bỏ `group tacacs+`);
  - bỏ một interface khỏi `policy.protected_interfaces` (gỡ bảo vệ là hợp lệ về cấu trúc);
  - đổi `parking_vlan` cùng lúc với mọi cổng unused.
  Các đột biến này chỉ có thể lộ ra khi so với một nguồn khác (cấu hình thật trên thiết bị, sensor (iv) sau render),
  hoặc không lộ ra. Không cố "bắt cho đủ" bằng invariant: đó là kết quả cần đo, không phải lỗi cần sửa.

## 5. API

```python
# models/invariants.py
def check_invariants(ctx: NetworkContext) -> list[Violation]
```

- Hàm thuần: không đọc file, không raise vì dữ liệu (dữ liệu đã qua contract). Trả MỌI vi phạm, không dừng ở cái đầu.
- Thứ tự ổn định: theo thứ tự trong file. Mỗi thiết bị theo thứ tự `devices`: interfaces theo thứ tự (với mỗi interface,
  các kiểm theo một thứ tự cố định), rồi static routes, gateway, AAA, mgmt; sau đó `links`, rồi `policy`. Dict (`acls`,
  `secrets`, `login_lists`, `protected_interfaces`) duyệt theo thứ tự khoá trong file.
- `Violation` (đã có): `code`, `device`, `detail`. **Đề xuất thêm `path: str`** (dạng `loc`, vd
  `devices.2.interfaces.1.access_vlan`), để test kiểm được "đúng chỗ" và để trace chỉ đúng trường. Thêm trường là đổi
  contract trong `models/` (câu hỏi Q3).
- Hằng `INVARIANT_CODES: tuple[str, ...]`: danh sách mã, để test kiểm mỗi mã đều có một đột biến (giống
  `test_every_error_code_has_a_producer` của `ErrorCode`).
- `detail` không chứa giá trị secret: với `CTX_SECRET_REF` chỉ ghi TÊN khoá bị thiếu. Có test kiểm.

## 6. Chỗ gọi trong runtime

Đề xuất ở bước 1 của `run_case` (`runner/pipeline.py`), TRƯỚC `ContextStore.load` và trước mọi lời gọi LLM:

1. Đọc byte của file context → `NetworkContext.model_validate_json`. Lỗi contract → mỗi lỗi thành một vi phạm
   `CTX_SCHEMA` với `path` = `loc`, `detail` = loại lỗi + thông báo đã lọc (dùng lại `safe_error_message` của
   `models/operations.py`, không chép giá trị).
2. `check_invariants(ctx)`.
3. Có vi phạm → trace `context_invalid` (cùng tên event đang dùng, danh sách `{code, device, path, detail}`), kết cục
   **giống hiện nay**: `ok(verdict="reject", stopped_by="context_validation")`, `reason` là danh sách mã + đường dẫn.
   Không có change package, không gọi dsh.
4. Không vi phạm → `ContextStore.load` như cũ (vẫn cần cho `secret_values()` và projection).

`ContextStore`, projection, `harness/server.py`, gate: giữ nguyên. Server vẫn tự nạp `ContextStore` với ba kiểm tra cũ;
khi runner đã chặn trước thì server không được khởi động với context sai. Đổi server sang contract là việc của PR
chuyển `ContextStore`.

Hệ quả:

- 16 case chạy khô dùng `campus-small-01.json` (0 vi phạm): kết cục không đổi, golden không đổi (golden không gồm
  `trace.jsonl`).
- `tests/test_usage.py::test_context_rejected_before_any_stage_has_empty_usage` (context `{}`): vẫn reject
  `context_validation`, nay do lỗi contract thay vì `ContextStore`; `reason` đổi chữ, test không kiểm `reason`.
- `tests/test_harness_pipeline.py::test_corrupted_context_rejected` gọi thẳng `ContextStore.load`: không đổi.
- Một context mà `ContextStore` cho qua nhưng invariant mới chặn (vd ACL không tồn tại) nay bị reject ở runner: đây là
  thay đổi hành vi có chủ đích.

## 7. Kế hoạch test

| Test | Kiểm | Đối chứng |
|---|---|---|
| file thật hợp lệ | `check_invariants(campus-small-01)` == `[]`; cũng với `examples/valid/network_context.json` | — |
| mỗi invariant một đột biến | 16 đột biến của file thật (bảng mục 3), mỗi cái ra ĐÚNG MỘT vi phạm, đúng `code` và `path` | đối chứng âm: cùng file không đột biến → 0 vi phạm |
| mọi mã đều có đột biến | tập mã trong bảng đột biến == `INVARIANT_CODES` | thêm mã mà không thêm đột biến thì đỏ |
| gom đủ, không dừng ở cái đầu | áp 3 đột biến cùng lúc → đúng 3 vi phạm, theo thứ tự file | |
| thứ tự ổn định | gọi hai lần trên cùng context → hai danh sách bằng nhau | |
| không lộ secret | `CTX_SECRET_REF`: `detail` chỉ có tên khoá; `str()` của mọi vi phạm không chứa giá trị nào của `secret_values()` | đối chứng: giá trị đó đúng là có trong file |
| runner chặn trước LLM | context có một vi phạm → `reject`, `stopped_by=context_validation`, trace `context_invalid` có đúng mã, `usage` rỗng, không có `cba.ndjson` | context sạch → chạy tiếp |
| lỗi contract ở runner | context sai kiểu → `CTX_SCHEMA` với `path` đúng, `detail` không chép giá trị | |

Không test nào gắn `slow` trừ test runner (sinh subprocess khi context sạch; khi bị chặn thì không).

## 8. Không làm trong PR này

- Không chuyển `ContextStore` / projection / server sang contract.
- Không xoá ba kiểm tra cũ trong `context_store` (chúng vẫn là lớp thứ hai cho server).
- Không viết bộ sinh corpus hay mutator; không đổi `data/cases`.

## 10. Để sau

- Ánh xạ 8 mutator của tập corrupted (D-040) sang 16 mã: làm ở bước corpus, khi mô tả mutator vào repo (không đọc file
  zip ngoài repo, Q2). Với mỗi mutator ghi: mã bắt được, hay "không mã nào" (silent failure, RQ2 đo, không cố bắt).
- Harness server (`server.py`) và projection vẫn dùng `ContextStore` với ba kiểm tra cũ: chuyển sang contract +
  invariants ở PR chuyển `ContextStore`.
- Công tắc ablation: tắt `context_violations` cho B0/B1 ở PR ablation.

## 9. Câu hỏi cho nhóm

- **Q1.** `CTX_MGMT_UNREACHABLE`: dùng dạng nới (interface trong `mgmt_prefix` HOẶC route cụ thể tới nó), hay bỏ hẳn?
  Dạng chặt vi phạm ngay trên file thật (R1).
- **Q2.** Cho phép đọc mô tả 8 mutator từ file zip ngoài repo (D-040) để đối chiếu, hay nhóm chép danh sách vào
  DECISIONS?
- **Q3.** Thêm trường `path` vào `Violation` (đổi contract trong `models/`)?
- **Q4.** Giữ cả 5 invariant "(thêm)" (`CTX_SVI_VLAN_MISSING`, `CTX_RESERVED_VLAN_IN_USE`, `CTX_SUBNET_OVERLAP`,
  `CTX_NEXT_HOP_UNREACHABLE`, `CTX_GATEWAY_UNREACHABLE`), hay chỉ danh sách tối thiểu?
- **Q5.** Kết cục khi vi phạm: giữ `reject` / `stopped_by=context_validation` như hiện nay (đề xuất), hay tách thành
  `error_code` riêng? D-017 coi reject bởi control (i) là một kết cục được đếm cho control (i).
