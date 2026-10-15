# A10a — Strict integer và IPv4 host validation

## Nguồn và phạm vi

Nguồn: Cisco_Harness_Agent_before_validation_fix.zip.
Base: 9cdff8218e7b54e48d8e2b2689b564615e5bdba0, sau PR #9.
Sửa trên bản sao; không sửa ZIP gốc, không commit/push thay người dùng.
D-051 là ĐỀ XUẤT, chưa được nâng thành quyết định nhóm/GVHD.

A10 có ba phần: integer coercion, IPv4 host bits, phạm vi interface.
Gói này chỉ sửa hai phần đầu (A10a). SVI/Loopback (A10b) vẫn cần quyết định phạm vi.
Không sửa contract trong models/; không cần mở khóa thư mục này cho patch A10a.

## Thay đổi

- create_vlan.vlan_id; set_access_vlan.vlan_id; set_trunk.native_vlan và từng allowed_vlans;
  set_interface_ip.prefix_len; add_acl_entry.seq: chỉ nhận integer, không bool/string/float.
- Giữ nguyên giới hạn min/max và loại trùng/sắp thứ tự allowed_vlans.
- set_interface_ip kiểm ip và prefix cùng nhau: với /1–/30, từ chối địa chỉ network/broadcast.
  Giữ /31 và /32. Một địa chỉ .0/.255 vẫn có thể là host theo prefix khác, không chặn bằng suffix.
- Lỗi host address dùng message cố định, không chép raw input vào thông báo.
- Dữ liệu CBA không hợp lệ tiếp tục theo retry/termination hiện có; TSA bị chặn G2_schema.
  Không chuyển dữ liệu sai thành CLI hợp lệ bằng coercion.
- B1–B4 đều cưỡng chế G2 như trước; không thay model/prompt hoặc định nghĩa levels.

Nguồn quy tắc /31: RFC 3021, section 2.1 — hai địa chỉ được coi là host trên point-to-point link:
https://www.rfc-editor.org/rfc/rfc3021.html
Kiểm tra này không tự chứng minh topology hay image IOS hỗ trợ /31.

## Test và đối chứng

48 test mới: 24 đầu vào ép kiểu sai ở JSON boundary, 6 đối chứng integer hợp lệ có render,
4 network/broadcast, 8 host hợp lệ và ngoại lệ /31,/32, 2 G2, 4 fake pipeline B1–B4.
Test A8 duplicate normalization vẫn giữ nhưng dùng allowed_vlans [20,10,20] so [10,20]
thay cho chuỗi VLAN "30" nay phải bị từ chối.
Test models test_args_are_normalised_so_equal_calls_compare_equal cũng đổi kỳ vọng chuỗi số
thành ValidationError, giữ nguyên đối chứng integer/trunk/route và round-trip.
Lượt full đầu bắt được kỳ vọng cũ này (1 failed); đã sửa test và chạy lại toàn bộ.

Đối chứng source trước sửa: cùng 28 test coercion/host-boundary cho 22 failed, 6 passed.
Sáu ca False đã bị range validation cũ từ chối vì ép về 0; các giá trị True/string/float
và network/broadcast tái hiện lỗi. Đây là lượt cố ý chứng minh test phát hiện lỗi cũ.
Log negative-baseline không phải kết quả pass của bản sửa.

Schema sinh từ tool_call_schema() so byte sorted-JSON không đổi; strict parser behavior
và cross-field validation không được mô tả đầy đủ bởi JSON Schema.
Mọi source models/, schemas/, prompts/, config/, data/ giữ byte gốc.

## Phần chưa xử lý

- A10b: chưa mở rộng SVI/Loopback. Context hỗ trợ nhưng set_interface_ip/state/description,
  switchport và route chưa có cùng phạm vi. apply_acl đã nhận Vlan từ trước.
  Không nới regex đồng loạt vì switchport và routed interface có semantics khác nhau.
- Không siết các trường bool khác (enabled), không sửa context validation hoặc dải VLAN policy.
- Không chứng minh CLI đúng trên EVE-NG/IOS thật, không mở hỗ trợ hãng mới.
- Grounding xfail còn nguyên. Không dùng test fake để kết luận accuracy model.
- Chưa chạy Windows patch này; số liệu Linux không thay thế kiểm thử Windows.

## Kết quả Linux

- Targeted registry validation + duplicate + templates + models: 277 passed in 40.89s
- Full suite (JUnit XML; process exit 0): 1163 passed, 1 xfailed; JUnit suite duration 304.418s
- Console log cuối không đầy đủ; số liệu cuối lấy từ XML 1164 testcase, 0 failures/errors, 1 pytest.xfail.
- Ruff toàn repo: All checks passed!
- Dry B4: 16/16 đúng kỳ vọng; test regression mới kiểm G2 B1–B4.
- Python 3.12.14, pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.9.
- Full: python -m pytest -n 6 --dist loadfile --tb=short.
- Fake runners/dsh; parent test dùng guard TCP loopback-only; không API/deploy.
- Source ZIP SHA-256: be62be45853893eda8997ebe9b237d05bd7e378db9b4a659ee6267fb6869a128.
- Patch áp thử trên nguồn sạch; đối chiếu byte toàn bộ nguồn và các vùng không đổi.
- Windows: chưa kiểm thử; xem HUONG_DAN_WINDOWS.md.

## Windows validation — 2026-10-15

- Ruff: All checks passed.
- Targeted tests: 277 passed in 143.28s.
- Full suite: 1162 passed, 1 skipped, 1 xfailed in 3126.66s.
- Results reported from the operator's Windows machine.
- Offline tests; no model API calls or deployment.
- Scope: A10a only; SVI/Loopback support (A10b) remains open.
