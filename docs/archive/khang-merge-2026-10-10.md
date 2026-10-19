# Báo cáo so sánh và hợp nhất ba repo Cisco Harness Agent

Ngày hoàn tất: 10/10/2026. Phạm vi: đọc code, hợp nhất riêng, kiểm thử offline. Không gọi API model, không deploy, không tiếp tục dataset ZIP 2400 mẫu.

## 1. Nguồn và cách chọn base

| Ký hiệu | Tệp đính kèm thực tế | Vai trò |
| --- | --- | --- |
| K | Đồ-Án(3).zip, thư mục Đồ-Án/main | Bản Khang và trợ lý |
| D | cisco-harness-agent-main(1).zip | Bản D làm sạch và phát triển thêm |
| P | cisco-harness-agent.zip | Bản P phát triển từ 01/10 |
| Đánh giá P | Đánh giá hiện trạng & kế hoạch - Cisco Harness Agent (07 10 2026).md | Nhận định để đối chiếu, không phải nguồn chân lý thay code |

Đã giải nén nguồn ra ba thư mục review riêng, loại khỏi working copies venv/node_modules/cache/bộ cài/config máy; không sửa ZIP gốc. SHA-256 ZIP và nguồn từng file được ghi trong `MERGE_MANIFEST.json`. `MERGE_VS_D.patch` cho biết phần thay đổi so với base. Không có git history chung trong ZIP để thực hiện merge theo ancestry; đây là hợp nhất theo chức năng.

**Chọn D làm base sau khi đọc runner, dsh, server, contract, invariants, registry và tests cả ba.** D có nền bảo vệ runtime mới hơn: DSH_HOME riêng, workspace trung tính có khóa, redaction trước khi lưu stdout/stderr, quét leak nhiều nguồn, model pin và usage, kiểm tra context bằng contract/invariant. D không đơn thuần là bản dọn file. P có nhiều tính năng nghiên cứu hơn nhưng runner cũ sẽ làm lùi các bảo vệ đó nếu chép đè. K có các sửa lỗi intent/finalize mới hơn tài liệu P mô tả.

## 2. So sánh chức năng từ code

| Thành phần | D | P | K | Bản hợp nhất |
| --- | --- | --- | --- | --- |
| Runner hai stage | Có, bảo vệ runtime đầy đủ hơn | Có + B0–B4 | Có + prepare/confirmed intent | D runner + P levels + K review/confirmation |
| Context contract | File schema 1.0, contract v0.2, SecretStr, context_ref | File schema 0.2, nhãn nhúng context | Contract cũ lệch inventory | Giữ D; port P sang tham chiếu context |
| Domain invariants | 16 mã và runner context_check | 13 mã trong context store | Stub, thêm kiểm platform | Giữ D; không dùng số lượng để khẳng định superset |
| Quyền | network_admin bậc 2; security_admin bậc 3 | network_admin bậc 3 | network_engineer bậc 2; network_admin bậc 3 | Canonical theo D, không alias nâng quyền |
| Secret/runtime | Quét nhiều nguồn, raw redaction, timeout capture, scan incomplete | Cách quét đơn giản hơn | Cách quét đơn giản hơn | Giữ D; giải thích riêng exposure B0/B1 |
| Model, usage, workspace | ModelConfig, session config thực tế, token, khóa workspace | Chưa có bộ D | Chưa có bộ D | Giữ D |
| Sensors | Stub | Syntax/CIS, Batfish skipped | Stub | Port P + SENSOR_ERROR fail-closed |
| Evaluator/corpus/client | Stub | Có implementation và tests | Stub; scripts pilot riêng | Port P; nhãn dùng context_ref; evaluator sửa lỗi chấm thừa CLI |
| Registry | CIDR kiểm muộn; ACL port chưa ràng buộc đủ | Sửa CIDR/port; exit submode | Chủ yếu bản chung, mô tả AAA rõ hơn | Giữ sửa P; AAA mô tả đúng tác động lên VTY |
| Finalize | Kiểm marker tồn tại | Kiểm marker tồn tại | Khóa phiên, kiểm count/type/order/multiplicity | Giữ đầy đủ K |
| Human review | Chưa có | Chưa có | prepare_only và confirmed_intent | Giữ K; thêm canonical hash có version, raw tương thích |
| Dữ liệu không tin cậy | Projection allowlist | Projection allowlist | Thêm banner và trace hash | Giữ allowlist; banner đọc banner_motd của D, CBA mới thấy |

Các đường dẫn then chốt: `runner/{pipeline,dsh,dsh_home,model,workspace,context_check,intent_contract}.py`, `harness/{server,gate,context_store,leak}.py`, `models/{context,invariants,testcase,common}.py`, `tools/registry/{render,tools/acl,tools/route}.py`, `levels.py`, `sensors/`, `corpus/`, `eval/`, `llm/` dưới `src/harness_agent/`.

## 3. Đối chiếu tài liệu P ngày 07/10

| Nhận định trong tài liệu | Code ZIP hiện tại cho thấy | Kết luận xử lý |
| --- | --- | --- |
| F1: bản nhóm chưa có invariants, B0–B4/sensors/eval | Đúng về K/P phân nhánh; D hiện đã có 16 invariants và context_check, chưa có levels/sensors/eval | Tài liệu không phản ánh đầy đủ D đính kèm; chọn D rồi port P |
| 406 passed, 1 xfailed | K đính kèm chạy được 433 passed, 1 xfailed; có thêm tests prepare/finalize/projection | Không tái sử dụng số 406 như số liệu hiện tại |
| F3: prompt không bảo đảm bám ý định | Gate vẫn chỉ đối chiếu TSA với đề xuất CBA; có xfail chứng minh cả hai có thể cùng sai | Giữ xfail, không tuyên bố đã giải quyết grounding tự động |
| confirmed_intent raw hash nhạy whitespace/CRLF | Đúng cho hợp đồng K mặc định | Giữ mode raw; thêm canonical-json-v1 opt-in và tests đổi nội dung/hash |
| F6: chưa ghim model | D có config/model.toml, ModelConfig và đọc config thực tế từ session | Giữ D; không chèn model kiểu phỏng đoán vào base patch |
| F7: repo có venv/cache/config riêng | Đúng với ZIP K lớn | Không đưa các thành phần đó vào deliverable |
| F8: bridge cố định quyền cao nhất | K vẫn ROLE=network_admin theo nghĩa quyền cũ và đã yêu cầu confirmed intent | Role chuyển sang cấu hình server, mặc định access_operator; không lấy từ model |
| F9: AAA tool cũng cấu hình VTY | Mô tả K nêu đúng phạm vi, D cũng đã có phần tương ứng | Giữ; không coi tool này là thiết lập TACACS server/key thực tế |
| Các đề xuất dataset 2400/untrusted_notes adapter | Ngoài yêu cầu hiện tại | Không triển khai, không giải nén hay xử lý dataset đó |
| 110 lượt model thật/semantic review | Là số lịch sử của snapshot P đánh giá | Không chạy lại, không dùng làm bằng chứng cho merge; không xác nhận lại thống kê lịch sử trong task này |

## 4. Các quyết định giải xung đột

1. **Schema:** giữ D 1.0, không dual-schema âm thầm. Generator P đổi reserved_vlan_ids/trunk context và mã corrupted theo D. Corpus ghi JSON context riêng theo hash; TestCase chỉ ghi context_ref. Không dùng model_dump của SecretStr để lưu snapshot vì sẽ làm mất canary gốc. Corpus P đã sinh sẵn không được chép vào thư mục chạy khi chưa migration.
2. **Quyền:** giữ bốn role D. Recipe của P vốn giả định admin cao nhất được chuyển rõ sang security_admin. D cases giữ quyền D; role cũ của K không tự chuyển tại runtime.
3. **Runner:** giữ preflight/tool allowlist, timeout capture, redaction, leak scan incomplete, usage/model/workspace của D. Level là config do runner đặt rồi truyền server; model không chọn level/role.
4. **Registry/sensors:** nhận CIDR validation sớm và ACL protocol/port range của P; render đóng submode bằng exit. Golden CLI/hash đổi có chủ đích. B4 chỉ gửi package khi sensors cho qua; lỗi sensor thành ERROR. Batfish skipped không phải PASS.
5. **Finalize:** nhận K kiểm sequence và multiplicity, marker strict {staged: integer}, đúng count; sau finalize không stage/finalize tiếp. Các test sửa trạng thái phải chứng minh runner dừng trước renderer.
6. **Review:** prepare dừng sau CBA với pending_review, không có TSA/CLI. Review metadata không phải approval. Confirmed contract kiểm intent/snapshot và danh sách thao tác có thứ tự trước TSA. Chuẩn hóa mới có mode riêng để không âm thầm làm yếu raw contract cũ.
7. **Evaluator:** nhận parser độc lập P, không đọc gate/sensors làm đáp án. Sửa lỗi đủ thao tác nhưng kèm CLI lạ/duplicate/secret vẫn có thể được chấm correct. Context đánh giá nạp qua hash-checked ref. pending_review/invalid status không thành PASS.
8. **Projection:** giữ K banner với cảnh báo và trace hash; hỗ trợ canonical banner_motd. Hai context injection K được đặt trên inventory canonical D, giữ payload description/banner.
9. **Số quyết định:** giữ DECISIONS D, archive cả ba bản; quyết định tích hợp đặt tên MERGE-001..004. P:D-018 không cùng nghĩa D:D-018.

Không port các adapter/pilot dataset 2400 vào luồng đang chạy; không trộn run lịch sử vào kết quả kiểm thử mới. Không xóa các spike log gốc D. Không dùng test của role cũ để buộc tăng quyền mới; bảo vệ tương đương được kiểm bằng role canonical D. Các test cũ cần đổi expectation được giải thích bởi thay đổi semantics ở trên, không bị đánh dấu skip để làm xanh.

## 5. Kiểm thử

| Phạm vi | Kết quả cuối | Bằng chứng |
| --- | --- | --- |
| D nguyên bản | 797 passed | baseline-D-final.txt |
| P nguyên bản | 530 passed | baseline-P-verified.txt |
| K nguyên bản | 433 passed, 1 xfailed | baseline-K-verified.txt |
| Repo hợp nhất | **940 passed, 1 xfailed**, 160,54 giây | merged-final-complete.txt; pytest-merged.xml |
| Dry run B0–B4 | **80/80 đúng kỳ vọng**: mỗi mức 16/16 | dry-B0.txt … dry-B4.txt |
| Ruff lint | All checks passed | lint-final.txt |
| Ruff format | 139 files already formatted | format-final.txt |

Lệnh suite hợp nhất: `python -m pytest -n4 --tb=short --junitxml=verification-results.xml` trong môi trường kiểm thử offline. Sau suite, chỉ chuẩn hóa format ba script spike kế thừa D (không đổi hành vi); lint/format được kiểm lại. Số test giữa các repo không cộng cơ học vì nhiều test trùng nhau.

Môi trường: Linux, Python 3.12.14; phiên bản dependency cụ thể trong `requirements-tested.txt`. Mỗi nguồn có interpreter riêng để MCP subprocess import đúng source; socket TCP/IP bị chặn bằng offline guard trong môi trường kiểm thử. Client LLM chỉ dùng mock transport/cache. Lượt kiểm ban đầu chỉ đặt PYTHONPATH gây lỗi MCP import (SDK không kế thừa biến này); đã loại lượt đó khỏi số liệu và chạy lại với môi trường riêng. Hai lần kiểm bị ngắt chưa có summary cũng không được tính là đã pass.

Bằng chứng cuối cùng trong `verification/`; log trung gian lỗi môi trường không được gọi là baseline failure. Tests xfail là lỗi đã biết về original-intent grounding; không phải test bị bỏ qua do merge. Dry run đúng kỳ vọng bao gồm các fault case cố ý sinh ERROR; ERROR không được tính là accuracy PASS trong evaluator. Token trong fake logs là fixture mô phỏng.

Golden được cập nhật vì renderer thêm exit, package ghi level/sensors và gate decision thêm shadow/enforced. Đã giữ test LF/UTF-8 và đối chứng CRLF khác hash; chưa có lượt chạy Windows mới để khẳng định cross-platform hoàn toàn.

## 6. Vấn đề còn lại và giới hạn

- **Grounding tự động chưa xong:** approved CR chưa chứng minh đúng ý định người dùng. Hợp đồng người xác nhận là HITL/oracle tùy chọn; không dùng nó để báo accuracy model tự động. Xfail vẫn là bằng chứng giới hạn này.
- **B4 chưa là kiểm chứng mạng thực:** syntax/CIS là bộ luật hẹp; Batfish skipped, chưa EVE-NG/Cisco device. Không có deploy hoặc rollback thực thi.
- **Chưa có số liệu LLM sau merge:** không API; model pin được kiểm về config/trace giả, chưa chứng minh dsh thật tương thích đầy đủ sau tích hợp. Chưa thể rút kết luận Ch4 hoặc so sánh model.
- **Leak metric giữa các mức:** B0/B1 cố ý đưa inventory thô vào prompt; D leak scan nhìn thấy nguồn này. leak_attempt_detected không phải ASR. Cần báo cáo nguồn/đầu ra riêng; dùng canary trong nghiên cứu. B0 candidate có thể chứa nội dung nguy hiểm vì đó chính là baseline cần đo, không deploy.
- **B0/B1 không tắt mọi validation:** contract cấu trúc vẫn là điều kiện dữ liệu vận hành; phần tắt là invariant/projection. Gate G1–G2 luôn bật ở B1+. Nêu rõ khi định nghĩa ablation.
- **Corpus/evaluator:** nhãn sinh deterministic chưa có expert review; parser chỉ phủ vocabulary hiện có; chưa thêm Wilson CI, split/family protocol hay thống kê thực nghiệm mới. Không coi context_ref bằng chứng freshness hoặc authorization mạng thật.
- **Context compatibility:** input schema cũ 0.1/0.2 không tự migrate. Mã corrupted và role khác P/K, nên không trộn nhãn/run cũ vào bảng mới. Rule parking/reserved của D/P không đồng nhất hoàn toàn.
- **Runtime isolation:** tắt tool và workspace tách biệt không phải sandbox OS bất khả xâm phạm; runner/server vẫn cùng máy và phụ thuộc config tin cậy. ContextStore trực tiếp còn lớp legacy tối thiểu; pipeline dùng context_check đầy đủ trước runtime. Không coi việc import ContextStore riêng là tương đương toàn bộ validation pipeline.
- **Phân phối:** source checkout là hình thức hỗ trợ; prompts/config vẫn tra từ repo root, chưa cam kết runner hoạt động như wheel độc lập. Template package-data được giữ từ P.
- **Cần nhóm/GVHD duyệt:** chọn schema/role chuẩn, định nghĩa ablation và cách báo cáo leak, phạm vi sensors, prepare/confirmed-intent và vị trí của nó trong thực nghiệm. Các quyết định merge chỉ mang nhãn ĐỀ XUẤT.

## 7. Bàn giao

Repo hợp nhất riêng, README chạy offline trên Windows, report này, manifest nguồn/hash từng file, patch so với D, requirements môi trường test và log cuối. Các ZIP gốc giữ nguyên. Tệp đính kèm dataset 2400 nằm bên trong ZIP K không được khai thác hay đưa vào bản bàn giao.
