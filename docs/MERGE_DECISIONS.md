# Quyết định tích hợp ba nguồn

Trạng thái: **ĐỀ XUẤT**. Không tự nâng thành NHÓM CHỐT/ĐÃ CHỐT. GVHD: cần duyệt thiết kế thực nghiệm và thay đổi ảnh hưởng Ch1–2. Hiện thực: code trong bản hợp nhất, kết quả test tại MERGE_REPORT.md.

## MERGE-001 — Base và các xung đột

D là base runner, contract, quyền, đo leak/model/usage và workspace. P cung cấp bảng ablation, B0, sensors, corpus, evaluator, client/cache. K cung cấp prepare/confirmed intent, lifecycle finalize, strict VLAN context, projection banner và luật prompt không suy VLAN từ tên.

Giữ schema file 1.0 của D; không dùng schema 0.2 của P đè lên. Port generator/evaluator sang context_ref. Giữ 16 invariant của D; các tên mã corrupted của P được đổi tương ứng. Không tuyên bố 16 invariant là superset chính xác của 13 invariant P: điều kiện parking/reserved có khác phạm vi.

## MERGE-002 — Finalize và ý định

Giữ thứ tự/multiplicity của staged operations bằng so sánh danh sách; marker finalize phải đúng object một trường staged, integer không âm, bằng số thao tác. Chuẩn hóa confirmed intent chỉ theo hash_mode mới; hợp đồng raw cũ giữ nguyên semantics. Prepare không tự tạo approval, dừng ở pending_review. Evaluator không tính pending_review là PASS.

## MERGE-003 — Sensors và kết quả nghiên cứu

B4 chặn syntax/CIS; Batfish skipped. Lỗi thực thi sensor là SENSOR_ERROR, không accept. Error code INTENT_CONTRACT_INVALID bảo tồn K. B0 có thể giữ secret trong candidate để đo đúng output nghiên cứu; capture stdout vẫn redacted. B0/B1 context exposure được trace/quét theo cách rộng của D, khác cách P loại prompt khỏi leak scan. Cần tách chỉ số này khi viết Ch4.

## MERGE-004 — Tests và dữ liệu

Giữ tests D, tích hợp tests P/K có ý nghĩa; điều chỉnh golden cho exit và trường shadow gate/sensor mới, không bỏ kiểm tra. Test enum đổi kỳ vọng vì P dùng StrEnum. Test leak renderer của D chạy ở B3 (sensor shadow); B4 có test riêng chứng minh chặn. Case role canonical theo D; không alias ngầm old network_admin sang bậc cao.

Không chạy model/API, không deploy, không tiếp tục dataset ZIP 2400. Không chuyển các số liệu model thật lịch sử sang evidence của bản merge. Các sửa lỗi mới cho evaluator (unknown CLI/duplicates/secret) có đối chứng sạch rồi cài lỗi.

## Đối chiếu số quyết định trùng

P:D-018..D-024 là số trong nhánh P: contract/invariants, sensors, ablation, corpus, evaluator, client, security review. D:D-018..D-024 chỉ các thay đổi leak/setup/model/usage/bytes/test speed khác. Không gộp bằng số. Trích dẫn trong module được port từ P phải hiểu trong ngữ cảnh `docs/archive/source-P/DECISIONS.md`; DECISIONS.md gốc ở root vẫn là bộ D. Các quyết định merge dùng MERGE-xxx để không chiếm số D chưa được nhóm phân bổ.
