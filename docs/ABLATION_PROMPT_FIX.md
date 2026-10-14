# A5 — thống nhất policy CBA cho ablation — 14/10/2026

## Nguồn và phạm vi

- ZIP: `Cisco_Harness_Agent_before_prompt_fix.zip` do Khang cung cấp.
- Git archive comment: `386991e9cc2c7f72667ee9305a09895d618a4d44` (main đã merge A7).
- SHA-256: `3d45e113827e4702e944901a1444536aba0034f786e407cd3029047434138b9f`.
- Sửa trên bản sao riêng, giữ ZIP gốc; không commit/push thay người vận hành.
- Không gọi model/API, deploy hoặc tiếp tục dataset 2400 mẫu.
- Giữ nguyên các bản runner trust, model pin, approval token, Windows POST và approval retry.

## Chênh lệch tìm thấy

Bản gốc có hai prompt CBA riêng. cba.md dùng cho B2–B4 chứa yêu cầu làm rõ device/VLAN ID/name,
không tự tạo tên VLAN, không suy VLAN ID từ chữ số trong tên, không lấy tên VLAN khác làm mapping,
hỏi lại khi có alternatives chưa chọn và không dùng lỗi tool làm lý do để đoán.
cba_b1.md chỉ có câu ambiguity chung, đồng thời tự khai “level B1” trong dòng đầu.

Vì vậy B1/B2 không chỉ khác context validation/projection như thiết kế: prompt policy cũng khác.
Probe offline trong before-fix.log xác nhận rule 5 khác nhau và nhãn mức xuất hiện ở B1.
Không dùng kết quả của fake_dsh để kết luận ảnh hưởng định lượng tới model thật.

## Sửa đổi

Chỉ còn `prompts/cba.md` là template CBA chung. Xóa `prompts/cba_b1.md` để tránh duy trì hai bộ luật.
Runner gọi `_cba_prompt` cho mọi stage CBA hai giai đoạn; không lựa chọn prompt theo tên mức.

| Thành phần | B1 | B2–B4 |
| --- | --- | --- |
| Identity, mục tiêu và luật thao tác/clarification/submit | Cùng template | Cùng template |
| Luật free text và untrusted_text | Cùng nội dung | Cùng nội dung |
| NETWORK_SOURCE | Inventory trong prompt, không read tools | list_devices/get_device/get_interface |
| INVENTORY block | Raw inventory như trước, kể cả canary | Không nhúng raw inventory |
| Tool exposure, validation, gate/sensor | Theo level spec cũ | Theo level spec cũ |

Chỉ thay NETWORK_SOURCE và INVENTORY khi render; intent giống nhau được giữ nguyên.
Rule 2 thống nhất free text trong state và trường untrusted_text; rule 3–7 lấy nguyên nội dung
cba.md trước sửa. Các luật VLAN nay cũng có ở B1. Không có nhãn B1/B2/B3/B4 trong prompt CBA.

Thay placeholder đúng một lượt, không diễn giải lại chuỗi như `{{INTENT}}` trong dữ liệu vừa chèn.
Đây là tính đúng của ghép template, không phải cơ chế semantic grounding hoặc chống mọi prompt injection.
Markers vẫn là text gửi model; các gate và kiểm tra deterministic tiếp tục là ranh giới thực thi.

B0 và TSA prompt giữ nguyên byte. Không thay stage_tools, level table, model, control validation,
gate/sensors, schema, dependency, config hoặc golden CLI. B1 vẫn không có read tools; B2 vẫn có projection.
Không làm B1/B2 giống hệt toàn bộ prompt vì nguồn network state là khác biệt có chủ đích của ablation.

## File thay đổi

| File | Thay đổi |
| --- | --- |
| `prompts/cba.md` | Template policy chung, chỉ có placeholder nguồn state/inventory/intent |
| `prompts/cba_b1.md` | Xóa bản policy trùng lặp và bị lệch |
| `src/harness_agent/runner/pipeline.py` | CBA renderer chung, thay dữ liệu một lượt |
| `tests/test_prompt_parity.py` | 14 test policy/data/integration/đối chứng |
| `CLAUDE.md`, `DECISIONS.md` | Quy tắc và D-048 ĐỀ XUẤT |
| `docs/ABLATION_PROMPT_FIX.md` | Báo cáo này |

## Kiểm thử

- So sánh policy sau khi loại đúng hai phần được phép khác: source line và inventory block.
- Kiểm tra cùng luật VLAN/clarification/submit cho B1–B4, không có nhãn level trong prompt CBA.
- B1 giữ inventory/canary và không read tools; B2–B4 không nhúng inventory/canary, giữ read tools.
- Intent/inventory chứa placeholder-like text không bị thay thế lại.
- Projection không gọi hàm đọc raw inventory để ghép prompt.
- Đối chứng âm cố ý đổi riêng rule 5 của B1: phép kiểm parity phải phát hiện khác biệt.
- Fake pipeline B1–B4 ghi đúng cba.prompt.txt đã render và ghi hash của template chung trong trace.
- tests/test_levels.py và full suite kiểm tra tiếp các khác biệt control theo mức, kể cả B0.
- Dry cases B1–B4 đối chiếu các kết cục fixture/golden; không đo accuracy của LLM.

## Giới hạn và cách dùng cho thực nghiệm

D-048 là ĐỀ XUẤT; cần GVHD duyệt prompt/thiết kế thực nghiệm. Không khẳng định B1 và B2 hoàn toàn
không còn biến gây nhiễu khác. B2 còn bật cả domain validation và projection theo thiết kế sẵn có.
Không sửa grounding xfail, dataset/evaluator, A8–A13 hoặc khả năng sinh CLI đúng trên thiết bị thật.

Trace giữ cơ chế hash prefix 16 ký tự của file prompt hiện có; hash cba.md là hash template chung,
không phải hash đầy đủ của prompt đã render. Lưu commit, config/context và cba.prompt.txt khi đối chiếu.
Source instruction biến thiên nằm trong runner và được lưu trong prompt thực tế. Không coi trace là
bằng chứng chống giả mạo runtime cùng user.

Các kết quả model trước patch dùng prompt cũ. Không gộp số liệu trước/sau như cùng điều kiện thực nghiệm;
nếu chạy đánh giá thật sau này phải ghi rõ phiên bản và so các mức trên cùng bộ điều kiện đã cố định.
Lượt sửa này không gọi API, không tuyên bố model đã hết đoán VLAN hoặc robustness đã tăng.

Chưa có kiểm thử Windows cho A5; không lấy số liệu Windows A7 làm bằng chứng cho bản này.

## Kết quả Linux

- Full suite: **1093 passed, 1 xfailed in 241.04s (0:04:01)**.
- Test mới A5: **14 passed in 9.19s**.
- Ruff toàn repo: **All checks passed!**
- Dry B1/B2/B3/B4: **16/16 mỗi mức, 64/64 kết cục đúng kỳ vọng**.
- B0 và TSA prompt giữ nguyên byte so với ZIP đầu vào; patch kiểm tra trên bản giải nén sạch.
- Python 3.12.14 / pytest 9.1.1 / pytest-xdist 3.8.0 / Ruff 0.16.9 / mcp 1.30.0.
- Full command: `python -m pytest -n 6 --dist loadfile --tb=short`.
- Fake runners/dsh, guard TCP loopback-only ở tiến trình test; không gọi model/API hoặc deploy.
- Chưa kiểm thử Windows A5. Token/request counts của dry run là số mô phỏng.

## Windows validation — reported 2026-10-15

- Ruff: All checks passed.
- Prompt/levels tests: 35 passed in 116.30s.
- Full suite: 1092 passed, 1 skipped, 1 xfailed in 1512.99s.
- Results reported from the operator's Windows machine.
- Validation used fake runners/dsh; no model API calls or deployment.
- These tests verify prompt/control integration, not real-model accuracy.