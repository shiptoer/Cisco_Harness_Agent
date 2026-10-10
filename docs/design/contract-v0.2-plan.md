# Kế hoạch contract v0.2: một schema context, 4 role, chặn ký tự điều khiển

| | |
|---|---|
| Nhánh | `tv2/contract-v0.2`, tách từ `main` tại `8388aa5` |
| Ngày phân tích | 08/10/2026 |
| Quyết định liên quan | D-042 (một schema context, **ĐỀ XUẤT**), D-010 (4 role, NHÓM CHỐT), D-003 (ký tự điều khiển, NHÓM CHỐT); D-001, D-005, D-014, D-027 |
| Trạng thái kế hoạch | ĐỀ XUẤT, chờ nhóm duyệt. Chưa sửa file nào trong `src/`, `tests/`, `data/`, `schemas/` |

Mọi số liệu dưới đây đo trên code tại `8388aa5`. Không in giá trị secret nào: các bảng chỉ ghi tên trường.

## 0. Điều chỉnh nhóm đã duyệt (08/10/2026)

Nhóm duyệt kế hoạch kèm năm điều chỉnh dưới đây. Chúng THAY phần tương ứng ở các mục sau.

1. Q-B, Q-C: `TestCase` KHÔNG nhúng `NetworkContext`. Thay bằng `context_ref: {path: str, sha256: 64 hex}` và một hàm
   nạp kiểm sha256 của file, lệch thì raise. Không viết serializer `reveal_secrets` (mục 7.3 không còn áp dụng).
2. `SecretStr` chỉ `min_length=1`. Ngưỡng ≥ 8 ký tự giữ trong `secret_values()`, giống `ContextStore`.
3. Q-A: `DeviceRole` đúng 3 giá trị của dữ liệu (`router`, `distribution_switch`, `access_switch`).
4. Q-E: thêm case `adv-privilege-apply-acl` (`network_admin` gọi `apply_acl` hợp lệ, kỳ vọng ok / reject /
   `G3_rbac`) cùng kịch bản `fake_dsh`. Golden được thêm hash cho case mới.
5. ACL dạng wildcard (context) ↔ CIDR (registry) là việc của evaluator, không sửa ở PR này (mục 12).

Thứ tự commit: (a) D-003 `FreeText` + `hide_input_in_errors`; (b) contract context v0.2 + `schemas/` + `examples/` +
test; (c) `TestCase.context_ref`; (d) D-010 4 role + 2 case đổi role + case mới + golden.

## 1. Vì sao

`NetworkContext.model_validate` trên file context thật duy nhất của repo (`data/contexts/campus-small-01.json`) báo 47
lỗi. Vì vậy lúc chạy, harness không dùng contract mà dùng validator viết tay đọc dict thô
(`src/harness_agent/harness/context_store.py`), trái với D-001 và D-005. Corpus (D-040), invariants (task 2.2) và
evaluator sắp viết đều cần MỘT schema khớp với dữ liệu. D-042 chọn hướng: sửa contract theo định dạng file context thật.

## 2. Phân tích 47 lỗi của `NetworkContext`

Nhóm theo loại. Không có lỗi "kiểu khác" (`*_type`): mọi giá trị có kiểu đúng với khai báo, chỉ sai tên trường, enum
hoặc thiếu trường.

### 2.1. Trường bắt buộc mà dữ liệu không có (4 lỗi)

| Đường dẫn | Dữ liệu có gì | Đề xuất |
|---|---|---|
| `site` (gốc) | không có; gốc có `context_id: "campus-small-01"` và `domain_name: "lab.local"` | bỏ `site` ở gốc; thêm `context_id` (pattern `^[a-z0-9-]{1,64}$`) và `domain_name` (pattern tên miền) |
| `devices.{0,1,2}.mgmt_ip` | không có; IP quản trị nằm ở interface (`Loopback0` của R1, `Vlan99` của SW1/SW2) trong `policy.mgmt_prefix` | bỏ `mgmt_ip`. Projection không dùng nó; invariant (task 2.2) có thể kiểm "mỗi thiết bị có một interface trong `mgmt_prefix`" |

### 2.2. Giá trị ngoài enum (12 lỗi)

| Đường dẫn | Giá trị trong dữ liệu | Enum hiện tại | Đề xuất |
|---|---|---|---|
| `devices.*.role` (3) | `router`, `distribution_switch`, `access_switch` | `core`, `distribution`, `access`, `edge` | `DeviceRole = {router, distribution_switch, access_switch}`. Thêm `core_switch` chỉ khi corpus cần (câu hỏi mở Q-A) |
| `devices.*.interfaces.*.mode` (9) | `unused` (2 cổng R1, 4 cổng SW1, 3 cổng SW2) | `access`, `trunk`, `routed` | thêm `unused`. Mọi cổng `unused` trong dữ liệu có `enabled: false`; trên switch có thêm `access_vlan: 999` (VLAN parking) |

### 2.3. Trường dữ liệu có mà contract không khai (31 lỗi, `extra_forbidden`)

| Cấp | Trường (số lỗi) | Đề xuất |
|---|---|---|
| gốc | `context_id` (1), `domain_name` (1), `links` (1) | khai cả ba. `links: list[Link]`, `Link = {a_device, a_interface, b_device, b_interface}` |
| thiết bị | `site` (3) | `site: SiteName` (pattern `^[A-Za-z0-9_-]{1,32}$`) |
| thiết bị | `ip_routing` (3) | `ip_routing: bool` (bắt buộc) |
| thiết bị | `default_gateway` (1, chỉ SW2) | `default_gateway: IPv4Address \| None = None` |
| thiết bị | `banner_motd` (3) | thay `banner` bằng `banner_motd: FreeText` |
| thiết bị | `acls` (2) | `acls: dict[AclName, list[AclEntry]]`, xem 2.4 |
| thiết bị | `aaa` (3) | `aaa: Aaa`, xem 2.4. Thay `Device.local_users` (định dạng cũ) |
| thiết bị | `secrets` (3) | `secrets: dict[SecretName, SecretStr]`, xem mục 7 |
| interface | `trunk_allowed` (2), `native_vlan` (2) | đổi `allowed_vlans` → `trunk_allowed: list[AnyVlanId] \| None`; thêm `native_vlan: AnyVlanId \| None` |
| interface | `acl_in` (1), `acl_out` (1) | `acl_in`, `acl_out: AclName \| None` |
| policy | `reserved_vlans` (1) | đổi tên `reserved_vlan_ids` → `reserved_vlans`, mặc định `[1, 1002, 1003, 1004, 1005]` (như `context_store`) |
| policy | `parking_vlan` (1) | `parking_vlan: AnyVlanId \| None = None` |
| policy | `protected_interfaces` (1) | `dict[Hostname, list[InterfaceName]]` |
| policy | `mgmt_prefix` (1) | `IPv4Network \| None = None` |

### 2.4. Phần lồng nhau mà 47 lỗi chưa cho thấy

Pydantic dừng ở trường lạ, nên nội dung của `aaa`, `acls`, `links`, `secrets` chưa được kiểm lần nào. 47 là cận dưới.
Định dạng thật (đã đọc toàn bộ file, kiểu của từng trường):

| Model mới | Trường (kiểu thấy trong dữ liệu) |
|---|---|
| `AclEntry` | `seq` (int), `action` (`permit`/`deny`), `protocol` (`ip`/`tcp`/`icmp`), `src`, `dst` (chuỗi: `any`, `host A.B.C.D`, `A.B.C.D W.W.W.W` dạng wildcard), `dst_port` (int, tuỳ chọn), `remark` (FreeText, tuỳ chọn) |
| `Aaa` | `new_model` (bool), `login_lists` (dict tên → list phương thức: `local`, `group tacacs+`), `vty_login_list` (tên), `local_users`, `tacacs_servers` |
| `AaaLocalUser` | `username`, `privilege` (0–15), `secret_ref` (tên khoá trong `secrets`) |
| `TacacsServer` | `name`, `address` (IPv4), `key_ref` (tên khoá trong `secrets`) |

Ghi chú:

- `src`/`dst` của ACL trong context dùng dạng wildcard (`10.0.0.0 0.255.255.255`), còn tham số `add_acl_entry` của
  registry dùng CIDR (`10.0.0.0/8`). Hai dạng cùng tồn tại; contract theo dữ liệu, không đổi registry trong PR này.
- Phương thức AAA: registry có enum `AuthMethod = {group tacacs+, group radius, local}`. Contract dùng lại enum đó.
- `secret_ref` và `key_ref` trong dữ liệu đều trỏ tới một khoá có thật trong `secrets` của cùng thiết bị (đã kiểm cả 6
  tham chiếu, chỉ in kết quả đúng/sai).
- `schema_version` của dữ liệu là `"1.0"`; hằng `SCHEMA_VERSION` của contract là `"0.1"`. Đề xuất:
  `schema_version: Literal["1.0"]` (định dạng file context), tách khỏi phiên bản của contract; giá trị lạ thì lỗi.

## 3. Kiểm tra `context_store.py` làm mà contract chưa có

| Kiểm tra trong `_index_and_validate` | Mã | Contract v0.1 | Thuộc về |
|---|---|---|---|
| gốc là object, có `devices` không rỗng | `CTX_SCHEMA` | có (`devices: min_length=1`) | contract |
| hostname đúng pattern, không trùng | `CTX_SCHEMA`, `CTX_DUP_HOSTNAME` | có | contract |
| `vlan_id` 1–4094, không trùng trong thiết bị | `CTX_SCHEMA`, `CTX_DUP_VLAN` | có | contract |
| tên interface đúng pattern, không trùng | `CTX_SCHEMA`, `CTX_DUP_INTERFACE` | có | contract |
| `ipv4` parse được, static route parse được | `CTX_SCHEMA` | có (kiểu `IPv4Interface`, `IPv4Network`) | contract |
| `access_vlan` phải có trong VLAN của thiết bị (chỉ khi thiết bị có VLAN) | `CTX_DANGLING_VLAN` | **chưa** | invariant (2.2) |
| VLAN trong `trunk_allowed` phải có trong VLAN của thiết bị | `CTX_TRUNK_MISMATCH` | **chưa** | invariant (2.2) |
| một địa chỉ IPv4 không dùng ở hai interface (trên toàn context) | `CTX_DUP_IP` | **chưa** | invariant (2.2) |
| file không đọc được / không phải JSON | `CTX_UNREADABLE` | không thuộc model | loader |

Hành vi khác của `context_store` cần giữ khi sau này chuyển sang contract (PR sau, không phải PR này):

- `secret_values()` chỉ lấy giá trị dài ≥ 8 ký tự, từ `secrets` của thiết bị và `local_users[].secret_hash` (định dạng cũ).
- `protected_interfaces()` gộp `policy.protected_interfaces` với cổng có `is_uplink` (định dạng cũ).
- `reserved_vlans()` đọc cả `reserved_vlans` lẫn `reserved_vlan_ids`; mặc định `[1, 1002–1005]`.
- `max_devices_per_change` mặc định 1, `max_interfaces_per_change` mặc định 4.
- Projection (`project_device_list`, `project_device`, `project_interface`) dựng theo allowlist; `description` chỉ xuất
  trong `untrusted_text`. Khoá `aaa`, `secrets`, `acls` (trừ tên ACL) không bao giờ ra ngoài.

## 4. `TestCase` và 15 file `data/cases/*.json`

Cả 15 file đều không hợp lệ theo `TestCase`, mỗi file đúng 10 lỗi giống nhau (150 lỗi):

| Loại | Trường |
|---|---|
| thiếu (7) | `case_id`, `subset`, `config_type`, `seed`, `generator_version`, `expected_verdict`; và `context` sai kiểu (`model_type`: file ghi đường dẫn, contract cần object `NetworkContext`) |
| thừa (3) | `run_id`, `expect`, `fake_scenario` |

Đây không phải lỗi dữ liệu: 15 file là kịch bản chạy khô của runner (`run_id`, `expect` theo kết cục của pipeline,
`fake_scenario`), không phải mẫu corpus. Đề xuất:

- Không ép 15 file vào `TestCase`. Giữ chúng là fixture của runner; nếu cần kiểm cấu trúc thì một model riêng
  (`DryRunCase`) ở `runner/`, ngoài thư mục contract. Không làm trong PR này.
- `TestCase.context` nhúng nguyên `NetworkContext` vào mỗi case. Với corpus 1000 case (D-040) và một context dùng
  chung, cân nhắc `context_ref` (id + sha256 của file context). Câu hỏi mở Q-B, không đổi trong PR này.

## 5. D-010: 4 role

### 5.1. Nơi dùng role

| Nơi | Nội dung |
|---|---|
| `src/harness_agent/models/common.py` | `Role` (StrEnum, 3 giá trị) |
| `src/harness_agent/harness/roles.py` | `CLI_ROLE_LEVEL` (bảng ánh xạ duy nhất), `ROLE_NAMES`, `TOP_LEVEL`, `role_level` |
| `src/harness_agent/harness/gate.py` | G3 (`level < spec.min_role`); G4 `_mgmt_problem` (`level < TOP_LEVEL` và chạm `mgmt_prefix`) |
| `src/harness_agent/tools/registry/` | `Role` (IntEnum 0–3) và `min_role` của 11 tool: `apply_acl`, `set_aaa_login` = 3; `create_vlan`, `set_trunk`, `set_interface_ip`, `add/remove_static_route`, `add_acl_entry` = 2; còn lại = 1 |
| `src/harness_agent/runner/__main__.py`, `scripts/check_setup.py` | ví dụ và kiểm tra setup dùng `network_admin` |
| `data/cases/*.json` | 15 file, mỗi file một `role` |
| `tests/` | `test_roles.py`, `test_gate.py`, `test_registry_mcp.py`, `registry_samples.py`, và 9 file khác truyền role cho `run_case` |
| `schemas/test_case.schema.json`, `examples/valid/testcase.json`, `examples/invalid/testcase_accept_without_calls.json` | enum role |
| golden | `change_package.md` ghi dòng `- Role: <role>` |

### 5.2. Ánh xạ mới và tác động

Ánh xạ: `viewer → viewer(0)`, `access_operator → noc_operator(1)`, `network_admin → netops_engineer(2)` (trước: 3),
`security_admin → security_admin(3)` (mới). Chỉ `network_admin` đổi mức.

Mô phỏng trong bộ nhớ (không sửa file): với `network_admin` ở mức 2, gate chặn mẫu hợp lệ của `apply_acl` và
`set_aaa_login` ở `G3_rbac`; 9 tool còn lại vẫn qua. `_mgmt_problem` dùng `TOP_LEVEL` = 3, nên một `network_admin`
chạm `mgmt_prefix` bằng route hay IP cũng bị chặn ở G4 (trước đây được qua). Không case chạy khô nào chạm
`mgmt_prefix` với role này (`clean-static-route` dùng `10.20.0.0/16`).

| Case | Role hiện tại | Tool | Kết cục nếu giữ role | Đề xuất | Lý do |
|---|---|---|---|---|---|
| `clean-aaa-tacacs` | `network_admin` | `set_aaa_login` (3) | accept → **reject `G3_rbac`** | đổi role → `security_admin` | case sạch phải accept; theo D-010, AAA cần `security_admin` |
| `adv-acl-protected-interface` | `network_admin` | `apply_acl` (3) | `G4_blast_radius` → **`G3_rbac`** | đổi role → `security_admin` | case này đo G4 (áp ACL lên cổng được bảo vệ). Giữ role thì G3 chặn trước và case không còn đo G4 |
| `adv-trunk-native-reserved` | `network_admin` | `set_trunk` (2) | không đổi | giữ | |
| `adv-description-injection` | `network_admin` | `set_interface_description` (1) | không đổi | giữ | |
| `clean-vlan30` | `network_admin` | `create_vlan` (2), `set_access_vlan` (1) | không đổi | giữ | |
| `clean-acl-entry` | `network_admin` | `add_acl_entry` (2) | không đổi | giữ | |
| `clean-static-route` | `network_admin` | `add_static_route` (2) | không đổi | giữ | prefix không chạm `mgmt_prefix` |
| `adv-credential-leak`, `ambiguous-vlan40`, `fault-harness-down`, `fault-rogue-tool` | `network_admin` | không tới gate | không đổi | giữ | |
| 4 case `access_operator` | `access_operator` | | không đổi | giữ | ánh xạ của role này không đổi |

Đề xuất phụ (cho nhóm): thêm một case mới `adv-acl-netops` (`network_admin` gọi `apply_acl`, kỳ vọng `G3_rbac`) để
corpus chạy khô đo được khác biệt giữa bậc 2 và 3 mà D-010 nhắc. Không bắt buộc cho PR này.

## 6. D-003: chuỗi đi vào CLI

CLI chỉ được render từ tham số tool call đã qua gate. Đã thử chèn 8 ký tự (`\n`, `\r`, `\x00`, `\x7f`, `\x85`, `\x9b`,
`\u2028`, `\u2029`; ở cuối và ở giữa chuỗi) vào mọi trường chuỗi của mẫu hợp lệ của 11 tool:

| Nơi | Trường | Kết quả |
|---|---|---|
| registry (đi thẳng vào CLI) | 16 trường chuỗi có trong mẫu hợp lệ (`tests/registry_samples.py`): `device`, `name`, `interface`, `text`, `acl_name`, `action`, `direction`, `protocol`, `src`, `dst`, `dst_port`, `list_name`, `ip`, `prefix`, `next_hop`, `tacacs_key_ref` | không ký tự nào lọt: pattern allowlist ASCII, enum, IP qua kiểu `ipaddress`, `tacacs_key_ref` qua `KEY_REF_PATTERN.fullmatch` |
| registry, chưa thử | `exit_interface` (không có trong mẫu; cùng pattern với `interface`), `methods` (list enum `AuthMethod`) | chưa thử trực tiếp |
| contract `FreeText` (`description`, `banner`) | | **lọt** `\x85`, `\x9b`, `\u2028`, `\u2029` |
| contract `VlanName`, `Hostname` | | không lọt |

Chuỗi của context không đi thẳng vào CLI ở B1–B4, nhưng model đọc chúng (A2: free text là đường tấn công) và B0 sinh
CLI trực tiếp. Sửa:

- `FreeText`: pattern cấm `\x00–\x1f`, `\x7f–\x9f`, `\u2028`, `\u2029`. Áp cho mọi trường free text của context v0.2:
  `Interface.description`, `Device.banner_motd`, `AclEntry.remark`.
- Mọi trường chuỗi còn lại của context v0.2 dùng pattern allowlist (tên ACL, tên login list, tên khoá secret,
  `context_id`, `site`, `domain_name`), không dùng `str` trần.
- Ghi chú từ D-003: `str.splitlines()` tách dòng ở `\x85`, `\u2028`, `\u2029`; runner đọc JSONL bằng `splitlines()`.

## 7. Secret

### 7.1. Đề xuất

- `Device.secrets: dict[SecretName, SecretStr]`, `SecretStr` có `min_length=8` (cùng ngưỡng với
  `context_store.secret_values()`). `SecretName` = pattern `^[A-Za-z0-9_.-]{1,64}$` (giống `KEY_REF_PATTERN`).
- `secret_ref`, `key_ref` là `SecretName` (tên, không phải giá trị). Việc tham chiếu trỏ tới khoá có thật là invariant.
- `model_config` của các model context thêm `hide_input_in_errors=True`: không có nó, lỗi validate in nguyên giá trị đầu
  vào (`input_value=...`), kể cả giá trị secret, vào thông báo lỗi và trace.
- `NetworkContext.secret_values() -> set[str]`: nơi DUY NHẤT gọi `get_secret_value()`. Docstring ghi rõ chỉ dùng cho
  phép quét leak.

Hành vi của `SecretStr` (pydantic 2, đã thử): `model_dump()` → `SecretStr('**********')`, `model_dump_json()` →
`"**********"`, `repr` che giá trị, JSON schema là `{"type": "string", "format": "password", "writeOnly": true}`.

### 7.2. Chỗ đang đọc giá trị secret

| Nơi | Đọc từ | Trong PR này |
|---|---|---|
| `ContextStore.secret_values()` | dict thô (`secrets`, `local_users[].secret_hash`) | không đổi |
| `harness/server.py` `RunState.leak_fields` | `store.secret_values()` | không đổi |
| `runner/pipeline.py` `run_case` (`store_secrets`: che output thô, `_leak_sources`, quét `.dsh_home`, `dsh_cwd`) | `store.secret_values()` | không đổi |
| `harness/gate.py` G4 `set_aaa_login` | `store.key_refs()` (tên, không phải giá trị) | không đổi |
| tests (`test_gate`, `test_harness_pipeline`, `test_registry_mcp`) | `store.secret_values()` | không đổi |

Vì PR này chưa chuyển `ContextStore` sang contract, mọi phép quét leak vẫn đọc dict thô như cũ và không bị ảnh hưởng.
Thêm một test đối chiếu: `NetworkContext.secret_values()` bằng đúng `ContextStore.secret_values()` trên file context
thật, để PR chuyển đổi sau không làm mất secret nào.

### 7.3. Rủi ro cần nhóm quyết (Q-C)

`TestCase` nhúng `NetworkContext`. Bộ sinh corpus ghi `TestCase` ra JSON bằng `model_dump_json()` sẽ ghi
`"**********"` thay cho secret: corpus nạp lại sẽ có secret là chuỗi sao, phép quét leak tìm chuỗi đó và không còn đo
được rò rỉ. Hai cách:

1. `field_serializer` cho `secrets` chỉ trả giá trị thật khi gọi `model_dump(..., context={"reveal_secrets": True})`;
   mặc định che. Bộ sinh corpus gọi với cờ này, có test.
2. `TestCase` không nhúng context mà trỏ tới file (`context_ref`, Q-B): secret chỉ nằm trong file context.

Đề xuất cách 1 cho PR này (nhỏ, có test), cách 2 để nhóm quyết cùng Q-B.

## 8. Bảng thay đổi contract

| Model / kiểu | v0.1 | v0.2 |
|---|---|---|
| `Role` | `viewer`, `access_operator`, `network_admin` | thêm `security_admin` |
| `DeviceRole` | `core`, `distribution`, `access`, `edge` | `router`, `distribution_switch`, `access_switch` |
| `InterfaceMode` | `access`, `trunk`, `routed` | thêm `unused` |
| `FreeText` | cấm `\x00–\x1f`, `\x7f` | cấm thêm `\x80–\x9f`, `\u2028`, `\u2029` |
| `StrictModel` | `extra=forbid`, `frozen` | thêm `hide_input_in_errors=True` |
| `NetworkContext` | `schema_version` (`"0.1"`), `site`, `devices`, `policy` | `schema_version: Literal["1.0"]`, `context_id`, `domain_name`, `devices`, `links`, `policy`; bỏ `site`; thêm `secret_values()` |
| `Device` | `hostname`, `platform`, `role`, `mgmt_ip`, `interfaces`, `vlans`, `static_routes`, `local_users`, `banner` | `hostname`, `platform`, `role`, `site`, `ip_routing`, `default_gateway?`, `interfaces`, `vlans`, `static_routes`, `acls`, `aaa`, `banner_motd`, `secrets`; bỏ `mgmt_ip`, `local_users`, `banner` |
| `Interface` | `name`, `mode`, `enabled`, `access_vlan`, `allowed_vlans`, `ipv4`, `is_uplink`, `description` | `name`, `mode`, `enabled`, `access_vlan`, `trunk_allowed`, `native_vlan`, `ipv4`, `acl_in`, `acl_out`, `description`; bỏ `allowed_vlans`, `is_uplink` |
| `Interface` validator | access/trunk/routed | thêm `unused`: `enabled` phải là `false`, không `ipv4`, không `trunk_allowed`/`native_vlan` (cho phép `access_vlan` = VLAN parking); trunk: không `access_vlan`, `ipv4`; access: không `native_vlan` |
| `Policy` | `max_devices_per_change` (1), `max_interfaces_per_change` (4), `reserved_vlan_ids` | `reserved_vlans` (mặc định `[1, 1002–1005]`), `parking_vlan?`, `protected_interfaces`, `mgmt_prefix?`, hai giới hạn giữ mặc định |
| mới | | `AclEntry`, `Aaa`, `AaaLocalUser`, `TacacsServer`, `Link`; kiểu `AclName`, `ListName`, `SecretName`, `SiteName`, `ContextId`, `DomainName` |
| `LocalUser` (cũ, `secret_hash`) | | bỏ, thay bằng `AaaLocalUser` |
| `TestCase` | `context: NetworkContext` (nhúng) | `context_ref: ContextRef {path, sha256}`; hàm nạp kiểm sha256 (điều chỉnh 1) |

Ràng buộc liên đối tượng (VLAN treo, trunk lệch, IP trùng, tham chiếu ACL/secret/login list, link trỏ tới interface có
thật) KHÔNG vào model: chúng là invariant của task 2.2 (D-005).

## 9. File sẽ sửa

| File | Thay đổi |
|---|---|
| `src/harness_agent/models/common.py` | `Role`, `DeviceRole`, `InterfaceMode`, `FreeText`, `hide_input_in_errors`, kiểu chuỗi mới |
| `src/harness_agent/models/context.py` | model context v0.2 như mục 8 |
| `src/harness_agent/models/__init__.py` | xuất model mới, bỏ `LocalUser` |
| `src/harness_agent/harness/roles.py` | bảng 4 role (D-010) |
| `schemas/*.json` | sinh lại bằng `python -m harness_agent.models.export_schema` |
| `examples/valid/network_context.json`, `examples/valid/testcase.json`, `examples/invalid/network_context_duplicate_vlan.json`, `examples/invalid/testcase_accept_without_calls.json` | chuyển sang định dạng v0.2 |
| `data/cases/clean-aaa-tacacs.json`, `data/cases/adv-acl-protected-interface.json` | `role` → `security_admin` (mục 5.2) |
| `data/cases/adv-privilege-apply-acl.json`, `data/scenarios/adv-privilege-apply-acl.json` | case mới (điều chỉnh 4) |
| `tests/golden/dry_run_sha256.json` | một hash đổi, thêm hash của case mới (mục 11) |
| `tests/test_models.py`, `tests/test_roles.py`, `tests/test_gate.py`, `tests/test_registry_mcp.py`, `tests/registry_samples.py` | sửa theo D-010 / D-042 (mục 10) và thêm test mới |
| `src/harness_agent/runner/__main__.py`, `scripts/check_setup.py`, `docs/SETUP_WINDOWS.md`, `docs/design/registry-mcp-integration.md` | chỉ nơi nào nêu bảng role |
| `DECISIONS.md` | D-003, D-010, D-042: dòng `Hiện thực`; D-042 cần nhóm đổi trạng thái |

Không sửa: `harness/context_store.py`, `harness/gate.py`, `harness/server.py`, `runner/pipeline.py`, registry, prompt.

## 10. Test

### 10.1. Test mới

| Test | Kiểm | Đối chứng âm |
|---|---|---|
| file context thật hợp lệ theo contract | `NetworkContext.model_validate(campus-small-01.json)` không lỗi | — (là đối chứng dương cho các dòng dưới) |
| context sai → lỗi đúng chỗ | mỗi đột biến của file thật (role thiết bị lạ, mode lạ, trường thừa ở từng cấp, `vlan_id` 0, `seq` âm, `action` lạ, `privilege` 16, `secret` ngắn hơn 8, `schema_version` lạ, `FreeText` có từng ký tự cấm) ra đúng MỘT lỗi với `loc` đúng đường dẫn | cùng đột biến bỏ đi thì hợp lệ |
| `model_dump` không lộ secret | `model_dump()`, `model_dump_json()`, `repr()`, `str()` của context, và của `TestCase` chứa context, không chứa giá trị secret nào | thay `SecretStr` bằng `str` trong một model tạm của test thì có lộ, tức test bắt được |
| lỗi validate không lộ secret | `secrets` có giá trị sai (vd quá ngắn): chuỗi lỗi không chứa giá trị | model tạm không `hide_input_in_errors` thì có chứa |
| `secret_values()` của contract khớp `ContextStore` | trên file thật, hai tập bằng nhau | |
| ghi corpus không mất secret (nếu chọn cách 1 ở 7.3) | dump có cờ `reveal_secrets` rồi nạp lại: `secret_values()` không đổi | dump không cờ thì nạp lại ra chuỗi sao |
| `FreeText` chặn C1 và LS/PS | `\x85`, `\x9b`, `\u2028`, `\u2029`, ở đầu/giữa/cuối | ký tự in được bình thường (dấu câu, tiếng Việt có dấu) vẫn qua |
| 4 role | `role_level` đúng bảng mới; tên lạ raise | |
| bậc 2 khác bậc 3 | `network_admin` bị G3 chặn `apply_acl`, `set_aaa_login`; `security_admin` qua | gỡ G3 thì `network_admin` qua |

### 10.2. Test cũ sẽ đổi (dự kiến, theo phân tích tĩnh và mô phỏng; danh sách chính xác sau khi chạy)

| Test | Vì sao | Sửa |
|---|---|---|
| `test_roles.py`: `network_admin` == `TOP_LEVEL`; `security_admin` trong danh sách tên lạ; `test_network_admin_reaches_every_tool` | D-010 | theo bảng mới; tên "với tới mọi tool" là `security_admin` |
| `test_gate.py::test_network_admin_passes_every_sample` | D-010: hai mẫu bị G3 | đổi thành role cao nhất, thêm test bậc 2 ở 10.1 |
| `test_registry_mcp.py`: `_state` mặc định `network_admin`, test staging `apply_acl` / `set_aaa_login` | D-010 | truyền `security_admin` cho hai tool đó |
| `test_models.py`: ví dụ hợp lệ/không hợp lệ, `test_context_lookup_helpers`, `test_round_trip_json`, `test_access_interface_requires_access_vlan`, `test_routed_interface_cannot_have_access_vlan` | D-042: định dạng mới | sửa theo định dạng v0.2 |

## 11. Golden và case sẽ đổi

- Case: `clean-aaa-tacacs`, `adv-acl-protected-interface` đổi `role` → `security_admin`. Kết cục kỳ vọng của cả hai giữ
  nguyên (accept; reject ở `G4_blast_radius`).
- Golden: đúng một hash đổi, `clean-aaa-tacacs/change_package.md`, vì file có dòng `- Role: network_admin`. Đã kiểm:
  `TERMINATED`, `rejects.jsonl`, `change_request.json` của `adv-acl-protected-interface` không chứa tên role nào, nên
  không đổi. Sinh lại golden bằng lệnh có sẵn trong `tests/test_output_bytes.py` và ghi lý do trong commit.
- 13 case còn lại: không đổi file, không đổi kết cục.
- Case mới `adv-privilege-apply-acl`: golden thêm các hash của nó (`TERMINATED`, `change_request.json`, `rejects.jsonl`).

## 12. Không làm trong PR này

- Chưa chuyển `ContextStore` sang dùng contract: harness và runner vẫn đọc dict thô; mọi phép quét leak giữ nguyên.
  Đó là PR tiếp theo, kèm test đối chiếu projection trước/sau.
- Chưa viết `invariants.py` (task 2.2), kể cả các kiểm `CTX_DANGLING_VLAN`, `CTX_TRUNK_MISMATCH`, `CTX_DUP_IP` và kiểm
  tham chiếu.
- Không đổi registry (dạng CIDR của `add_acl_entry`, `min_role`), gate, prompt.
- `harness/server.py` có ba chỗ đưa `str(exc)` ra ngoài: `:211` và `:220` (`ToolFailure(str(exc))` từ `KeyError` của
  projection, chuỗi có hostname/interface do model gửi) và `:235` (trace `submit_invalid`, `error=str(exc)`). PR chuyển
  `ContextStore` sang contract xử lý tiếp (đổi sang loc + msg đã lọc); không sửa ở đây.
- Validator viết tay của registry tự dựng `msg` từ giá trị (thư viện `ipaddress` trong `tools/interface.py`,
  `tools/route.py`; `KeyRef` trong `tools/registry/base.py`): để nguyên. `ToolCall` (`models/operations.py`) đã che:
  chỉ loại lỗi pydantic trong allowlist `_ERROR_TYPES_WITHOUT_INPUT` giữ `msg`, loại khác thành thông báo cố định.
  Kiểm bằng `tests/test_contract_hygiene.py` (11 tool, mọi trường chuỗi của mẫu hợp lệ, kèm đối chứng âm).
- Tên tool lạ và khoá lạ trong `args` vẫn được ghi lại có chủ đích: đó là dữ liệu audit về hành vi lạm dụng. Gate
  (`_clip`) và server (`[:80]`) cắt chúng ở 80 ký tự; `ToolCall` thì chưa: `operations.py` chép nguyên tên tool lạ vào
  thông báo, và tên khoá lạ nằm trong `loc` không bị giới hạn độ dài. Đề xuất cắt 80 ký tự ở PR chuyển `ContextStore`.
- Số đo 08/10/2026: test mới của contract v0.2 không làm chậm `-m "not slow"`. `tests/test_contract_hygiene.py` (57
  test) chạy riêng 1,24 giây; `tests/test_gate.py` không đổi mà từ 0,49 giây (07/10) lên 0,94 giây, nên chênh lệch thời
  gian giữa các lần đo trong ngày là do tải máy.
- ACL: context ghi `src`/`dst` dạng wildcard (`10.0.0.0 0.255.255.255`), registry nhận CIDR (`10.0.0.0/8`). Quy đổi giữa
  hai dạng khi so khớp là việc của evaluator (D-006, D-032), không sửa ở đây.
- Không ép 15 file chạy khô vào `TestCase` (mục 4).
- Không đổi `TestCase.context` sang tham chiếu (Q-B).

## 13. Câu hỏi mở cho nhóm

- **Q-A.** `DeviceRole` chỉ gồm 3 giá trị có trong dữ liệu, hay thêm `core_switch` cho corpus?
- **Q-B.** `TestCase` nhúng `NetworkContext` hay trỏ tới file context (id + sha256)?
- **Q-C.** Cách ghi corpus mà không mất secret (mục 7.3).
- **Q-D.** D-042 đang là ĐỀ XUẤT. PR này hiện thực nó, nên nhóm cần duyệt D-042 (đổi sang NHÓM CHỐT) trước khi merge.
- **Q-E.** Thêm case `adv-acl-netops` (mục 5.2)?

## 14. Nội dung GitHub issue (chưa tạo)

```markdown
Title: Contract v0.2: một schema context (D-042), 4 role (D-010), chặn ký tự điều khiển (D-003)

`src/harness_agent/models/` đã khoá từ 01/10/2026; issue này xin mở khoá cho một PR (`tv2/contract-v0.2`).

**Vấn đề**
- `NetworkContext.model_validate(data/contexts/campus-small-01.json)` báo 47 lỗi; harness đang dùng validator viết
  tay đọc dict thô thay cho contract (trái D-001, D-005).
- `Role` có 3 giá trị; D-010 (nhóm chốt 06/10/2026) cần 4: thêm `security_admin`, `network_admin` xuống bậc 2.
- `FreeText` còn nhận `\x85`, `\x80–\x9f`, `\u2028`, `\u2029` (D-003, nhóm chốt 06/10/2026).

**Đề xuất** (chi tiết: `docs/design/contract-v0.2-plan.md`)
- `TestCase` trỏ tới file context (`context_ref`: đường dẫn + sha256) thay vì nhúng context; nạp thì kiểm sha256.
- Contract context theo định dạng file thật: thêm `context_id`, `domain_name`, `links`, `acls`, `aaa`, `secrets`
  (`SecretStr`), `trunk_allowed`, `native_vlan`, `acl_in/out`, `ip_routing`, `default_gateway`, `banner_motd`; enum
  `DeviceRole` và `InterfaceMode` (`unused`) theo dữ liệu; bỏ `mgmt_ip`, `site` ở gốc, `LocalUser`/`secret_hash`.
- `Role` thêm `security_admin`; `harness/roles.py` theo bảng D-010.
- `FreeText` cấm thêm C1 và LS/PS; mọi model context `hide_input_in_errors=True`.
- `SecretStr` (`min_length=1`); ngưỡng ≥ 8 ký tự của phép quét leak giữ trong `secret_values()`.
- `DeviceRole` đúng 3 giá trị của dữ liệu. Case mới `adv-privilege-apply-acl` đo khác biệt bậc 2 / bậc 3.
- Sinh lại `schemas/`, sửa `examples/`; 2 case đổi role; 1 hash golden đổi (`clean-aaa-tacacs/change_package.md`) và
  thêm hash của case mới.

**Ngoài phạm vi**: chuyển `ContextStore` sang contract; `invariants.py` (task 2.2).

**Cần trước khi merge**: nhóm duyệt D-042 (đang ĐỀ XUẤT). Q-A..Q-E đã trả lời ngày 08/10/2026 (mục 0 của file kế hoạch).

**Người**: TV2 (Phước) chủ trì contract; TV3 (Quang) và TV4 (Khang) duyệt phần role/gate và projection.
```
