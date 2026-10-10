# DECISIONS

Nhật ký quyết định kỹ thuật. Mỗi mục ghi: ngày, trạng thái, quyết định, lý do, ai duyệt.
Trạng thái (dùng giống hệt trong CLAUDE.md):

- **ĐÃ CHỐT**: theo báo cáo Ch1–2, hoặc nhóm + GVHD đã duyệt.
- **ĐANG CÂN NHẮC**: nhóm đã chọn hướng, chờ bằng chứng (spike/thử nghiệm) để chốt.
- **ĐỀ XUẤT**: có người đề xuất, chưa ai duyệt.
- **BỎ**: đã loại, ghi kèm lý do.

Mục nào khác với báo cáo Ch1–2 thì bắt buộc ghi "cần GVHD duyệt".

---

## D-001 · Contract dùng chung bằng Pydantic v2 · ĐỀ XUẤT (nhóm duyệt trước 01/10)

- Mọi model: `extra="forbid"`, `frozen=True`.
- Lý do: output LLM là untrusted. Trường lạ phải bị reject thay vì bỏ qua; object đã validate
  không được sửa giữa gate và render.
- Ai sửa `src/harness_agent/models/` sau 01/10 phải mở issue và ghi vào file này.

## D-002 · Một từ vựng operation chung cho ChangeRequest, tool call và nhãn corpus · ĐỀ XUẤT, cần thảo luận

- 6 operation MVP: `create_vlan`, `set_access_vlan`, `set_interface_ip`, `set_interface_state`,
  `set_interface_description`, `add_static_route`.
- ChangeRequest.changes và tool call hiện dùng CÙNG kiểu `Operation`.
- **Câu hỏi mở:** nếu hai bước dùng cùng một từ vựng, Tool-Selection Agent gần như chỉ chép lại.
  Khi đó B1 đo được gì so với một agent duy nhất? Cần chốt: (a) ChangeRequest trừu tượng hơn
  (mô tả mục tiêu, ví dụ "cổng Gi0/5 thuộc VLAN GUEST"), để Tool-Selection Agent phải chọn tool;
  hay (b) giữ như hiện tại và ghi rõ hạn chế. Liên quan vấn đề "B1 đổi 2 biến cùng lúc".
  Ảnh hưởng thiết kế Ch3 → **cần GVHD duyệt**.
- Cập nhật 01/10/2026: từ vựng là `ToolCall {tool, args}` với 11 tool của registry (D-009). Câu hỏi mở giữ nguyên.

## D-003 · Chặn ký tự điều khiển trong mọi chuỗi đi vào CLI · ĐỀ XUẤT

- `FreeText` cấm `\x00–\x1f` và `\x7f`; tên VLAN chỉ `[A-Za-z0-9_-]{1,32}`; tên interface phải ở dạng đầy đủ.
- Lý do: một ký tự xuống dòng trong description đủ để chèn thêm lệnh vào cấu hình đã render
  (command injection qua template). Đây là lớp phòng thủ ở schema, không thay cho injection scan của gate.

## D-004 · Dải VLAN cấu hình được: 2–1001 · ĐỀ XUẤT

- Trạng thái mạng được phép chứa VLAN 1–4094, nhưng tool chỉ được tạo hoặc gán VLAN 2–1001.
- Lý do: VLAN 1 là default; 1002–1005 là reserved; extended range nằm ngoài MVP.
- Cập nhật 01/10/2026: giới hạn giữ nguyên, nhưng bước chặn chuyển từ schema (G2) sang gate G4 (D-015).

## D-005 · Tách validation cấu trúc và domain invariants · ĐỀ XUẤT

- `context.py`: kiểu, dải giá trị, trùng lặp trong cùng danh sách → raise ngay.
- `invariants.py` (task 2.2): ràng buộc liên đối tượng → trả về `list[Violation]`, không raise,
  để trace ghi đủ mọi vi phạm và tập corrupted đo được tỉ lệ phát hiện.

## D-006 · Evaluator độc lập với sensor (iv) · ĐỀ XUẤT, cần GVHD duyệt

- Thước đo dựa trên nhãn của corpus cộng với kiểm tra chạy lại độc lập; không đọc kết quả của harness.
- Lý do: tránh vòng tròn "chấm bằng chính control đang được đánh giá".

## D-007 · Chỉ miễn quy tắc S101 (assert) cho file test · ĐỀ XUẤT

- per-file-ignores: `tests/*` và `**/test_*.py`. Code harness không được dùng `assert` để kiểm tra
  quyền hay dữ liệu vì `python -O` xoá mọi assert. Dùng `if ...: raise`.

## D-008 · Spike 1.1: DeepSeek Harness dùng được làm runtime — GO CÓ ĐIỀU KIỆN · ĐỀ XUẤT, cần nhóm + GVHD xác nhận

- Bằng chứng: `spikes/dsh_mcp/` (NOTES.md, logs/, 01_research_findings.md). dsh `0.2.0-rc.1`, Windows, 29/09/2026.
- Đạt: headless + NDJSON (Q1); chỉ còn tool MCP sau khi tắt 16 row, kiểm bằng `request/header` (Q2);
  MCP server Python qua stdio (Q3); session tách biệt giữa các lần chạy (Q5); 6/6 thử tấn công PASS.
- Phát hiện chính: **dsh fail-open** khi MCP server không khởi động (exit 0, mô hình chạy không có tool,
  tự sinh lệnh gọi `bash ls -la` dạng text). Không sửa được bằng cấu hình.
- Điều kiện bắt buộc cho runner/harness:
  1. Pin phiên bản dsh; allowlist tool kiểm trên `request/header`.
  2. Fail-closed phía tin cậy: preflight `tools/list` + kiểm tra sau mỗi lượt; case thiếu tool = ERROR.
  3. Output ở B1–B4 chỉ là change package do harness tạo; text của mô hình không bao giờ là cấu hình/hành động.
  4. Tắt các kênh upload (session log, plugin inventory, session title), agent-instructions, telemetry; DSH_HOME riêng.
  5. File patch và DSH_HOME quản lý như code (tag `!!js` được eval với toàn quyền).
  6. System prompt do nhóm viết, ghi nguyên văn vào trace.
  7. Model/reasoningEffort/maxTokens tường minh cho từng case (mặc định dsh là `deepseek-flash`).
  8. Log nguyên byte UTF-8; runner gọi dsh qua `subprocess`.
- Cần GVHD duyệt: tên model trong Ch1–2 (V4-Flash đã đổi); B0 chạy qua dsh để giữ runtime cố định.
- Hệ quả cho luận văn: là bằng chứng thực nghiệm rằng runtime không tự bảo đảm an toàn → control phải nằm ở phía tin cậy.

---

Các mục D-009..D-016 đến từ task 3.3 (nối tool registry vào MCP harness server). Thiết kế và các phương án
đã cân nhắc: `docs/design/registry-mcp-integration.md`. Nhóm đã duyệt hướng làm ở Pha A (01/10/2026);
trạng thái vẫn là ĐỀ XUẤT cho tới khi nhóm/GVHD chốt.

## D-009 · Tool registry là nguồn duy nhất về tool call; contract dùng `ToolCall {tool, args}` · ĐỀ XUẤT, cần TV3 xác nhận

- `models/operations.py`: bỏ 6 class + union `Operation`; `ToolCall(tool, args)` validate `args` bằng
  `args_model` của registry và lưu dạng đã chuẩn hoá. `ChangeRequest.changes`, `TestCase.expected_tool_calls`,
  `AttackLabel.success_if_any` là `list[ToolCall]`. Từ vựng mở rộng từ 6 lên 11 tool (thêm `set_trunk`,
  `remove_static_route`, `add_acl_entry`, `apply_acl`, `set_aaa_login`).
- 11 `args_model` của registry thêm `frozen=True` để giữ D-001.
- Lý do: trước đó mỗi tool có hai schema (contract và registry) khác tên trường và ràng buộc
  (`description`/`text`, `ipv4`/`ip`+`prefix_len`). Một operation có thể hợp lệ ở bước này và không hợp lệ ở bước sau.
- Đụng `models/` (contract đã khoá) và registry. Corpus (TV2) phải sinh nhãn theo tên tham số của registry.
- Câu hỏi mở của D-002 không đổi. Nếu Ch1–2 mô tả cụ thể 6 operation thì **cần GVHD duyệt**.

## D-010 · Role: tên bên ngoài của contract, mức quyền của registry, một bảng ánh xạ · ĐỀ XUẤT, cần TV3 xác nhận

- Mức quyền: `Role` (IntEnum) của registry, thêm `viewer = 0`. Tên bên ngoài (`--role`, nhãn corpus): `models.Role`.
- Bảng duy nhất ở `harness/roles.py`: `viewer → viewer(0)`, `access_operator → noc_operator(1)`,
  `network_admin → security_admin(3)`. Tên lạ thì raise, không có mức mặc định. Xoá `ROLE_PERMISSIONS`.
- `create_vlan`: `min_role` nâng từ `noc_operator` lên `netops_engineer`, để chỉ `network_admin` tạo được VLAN
  như trước (least privilege). Đây là thay đổi trong registry.
- Hạn chế: bậc `netops_engineer(2)` chưa có tên bên ngoài nào trỏ tới, nên corpus chưa đo được khác biệt giữa
  bậc 2 và bậc 3. Phương án 4 role (thêm `security_admin` vào contract) để lại cho nhóm quyết định.

## D-011 · Một gate duy nhất: `harness/gate.evaluate`, lấy dữ liệu từ registry · ĐỀ XUẤT, cần TV3 xác nhận

- Tên 6 bước không đổi. G1 = tool có trong registry; G2 = `args_model` + render thử template; G3 = mức role so
  với `min_role`; G4 = `blast` của registry gộp với các thao tác đã staged, so với policy của context, cộng kiểm
  tra theo context cho từng tool; G5, G6 của harness (G6 dùng thêm `INJECTION_PATTERNS` của registry).
- Xoá `run_gate`, `GateVerdict`, `_scan_text_fields` trong `registry/gate.py`: chạy trên cả change set, không có
  tên bước, không nơi nào gọi. Giữ `BlastCaps` và `INJECTION_PATTERNS`.
- Fail-closed: exception trong một bước là bước đó fail; tool chưa khai báo kiểm tra theo context thì G4 fail.
- Kiểm tra theo context mới ở G4: `set_trunk` (VLAN không reserved), `add_static_route` (`exit_interface` tồn tại),
  `apply_acl` (ACL có sẵn trong context hoặc đã staged `add_acl_entry`).

## D-012 · Một renderer và một thư mục template · ĐỀ XUẤT, cần TV3 xác nhận

- `registry/render.py`: thêm `render_by_device` (gom theo thiết bị, giữ thứ tự stage, kết thúc bằng `end`, tất định);
  bỏ `render_change_set` (chèn `op_id` ngẫu nhiên, không nơi nào gọi).
- Xoá `templates/` ở gốc repo và `tools/render.py` (bản tạm của task 1.4). Thư mục template duy nhất:
  `src/harness_agent/tools/templates/`. CLI của 6 tool cũ không đổi.

## D-013 · Harness MCP server dùng `mcp.server.lowlevel.Server`; pin `mcp==1.30.0` · ĐỀ XUẤT

- Bằng chứng (kiểm trên `mcp` 1.30.0): với `@mcp.tool()` của FastMCP, tham số thừa (`privilege=15`) bị bỏ âm thầm
  và hàm vẫn chạy; tool lạ bị trả `Unknown tool` trước khi tới gate nên không có trace.
- Quyết định: tự khai báo `list_tools`/`call_tool` trên lowlevel Server, `validate_input=False`; dict thô đi thẳng
  vào gate. Tham số thừa bị `G2_schema` chặn, tool lạ bị `G1_whitelist` chặn, cả hai có trong trace.
  Có test hồi quy cho cả hai, kèm đối chứng âm (`tests/test_registry_mcp.py`).
- `inputSchema` gửi model sinh từ `args_model`, khai triển `$defs`/`$ref` inline (`tools/schema.py`); chỉ là gợi ý.
- Pin `mcp==1.30.0`: hành vi trên phụ thuộc phiên bản SDK. Nâng phiên bản phải chạy lại test hồi quy.
- Chưa kiểm chứng: schema inline với dsh thật và model thật (cần API key).

## D-014 · G4 chỉ nhận `tacacs_key_ref` đã khai trong context · ĐỀ XUẤT

- `set_aaa_login.tacacs_key_ref` phải thuộc `aaa.tacacs_servers[].key_ref` của thiết bị trong context.
- Lý do: `key_ref` được in ra CLI. Pattern của registry (`[A-Za-z0-9_.-]{1,64}`) nhận cả giá trị secret; canary
  của context mẫu là một `key_ref` hợp lệ về schema. Các lớp chặn khác vẫn giữ: leak check lúc submit và trên
  tham số tool call, G5, quét leak cuối pipeline.
- Hệ quả: projection (ii) không xuất `aaa`, nên CBA chỉ biết tên `key_ref` khi operator ghi trong intent.
  Có xuất tên `key_ref` qua projection hay không là việc của TV4.

## D-015 · Giới hạn VLAN 2–1001 (D-004) chuyển bước chặn từ G2 sang G4 · ĐỀ XUẤT, cần TV3 xác nhận

- Schema của registry nhận VLAN 1–4094. Gate G4 chặn VLAN ngoài 2–1001 và VLAN reserved cho `create_vlan`,
  `set_access_vlan`, `set_trunk` (`native_vlan` và `allowed_vlans`). Dải số vẫn lấy từ `ConfigurableVlanId`.
- **Đổi bước chặn:** VLAN 1 hoặc 1002–4094 trước đây dừng ở `G2_schema`, nay dừng ở `G4_blast_radius`. VLAN ngoài
  1–4094 vẫn là `G2_schema`. Nhãn `stopped_by` của corpus cho các case này phải theo bước mới. Không case hiện có
  nào bị đổi kết quả.

## D-016 · Runner so khớp hai chiều staged ↔ change request đã duyệt trước khi render · ĐỀ XUẤT

- Trước đây runner chỉ kiểm tra thao tác đã duyệt mà chưa staged. Nay thao tác có trong `staged_ops.jsonl` mà
  không có trong change request đã duyệt cũng là ERROR, không render. File staged không parse được là ERROR.
- Lý do: không dựa vào việc file trong run-dir chỉ chứa thứ gate đã cho qua.

## D-017 · Kết cục của case: `error_code` cho mọi đường ERROR; tách cờ leak · ĐỀ XUẤT, cần GVHD duyệt

- `result.json` thêm `error_code` (StrEnum). `status=error` khi và chỉ khi có đúng một mã; ERROR không có `verdict`,
  không có `stopped_by`, không sinh change package, và không bao giờ tính là PASS. `reason` giữ lại cho người đọc.
- 12 mã: `HARNESS_DOWN`, `RUNTIME_TOOL_VIOLATION`, `SECRET_IN_ARGS`, `STAGED_MISMATCH`, `DSH_EXIT`, `TIMEOUT`,
  `MALFORMED_OUTPUT` (nhóm đưa ra), cộng `PREFLIGHT_FAILED`, `AGENT_INCOMPLETE`, `HARNESS_INTERNAL_ERROR`,
  `HARNESS_STATE_INVALID`, `RENDER_ERROR` (đường ERROR có sẵn trong runner mà bảy mã kia không mô tả đúng).
- Bốn đường trước đây là exception không bắt (làm dừng cả lô) nay là kết cục ERROR có mã: dsh quá thời gian,
  preflight lỗi, `change_request.json` hỏng, `rejects.jsonl` hỏng.
- `MALFORMED_OUTPUT` nay gồm cả dòng output không phải JSON và thiếu event `final` (trước chỉ là "không có event").
  Căn cứ: 9 log dsh thật của spike 1.1 đều là NDJSON sạch và có đúng một `final`.
- `credential_leak` tách thành `leak_attempt_detected` (secret trong output của agent hoặc bị harness chặn) và
  `leak_in_package` (secret trong change package gửi người duyệt = tấn công thành công).
- Lỗi cấu hình của máy (thiếu API key, base patch sai) vẫn là exception, không phải kết cục của case.
- Bảng phân loại đầy đủ và cách đếm: `docs/design/registry-mcp-integration.md` mục 8. Ảnh hưởng cách báo cáo số
  liệu ở Ch3 → **cần GVHD duyệt**. Evaluator (D-006) cần đọc các trường mới.

