# Runner trust-boundary fix — 11/10/2026

## Nguồn và phạm vi

Đầu vào: `Cisco_Harness_Agent_before_runner_fix.zip` do Khang xuất bằng `git archive`.
Git archive comment: `c59589cd721f02f893dfb8886deca84fe67b2c2e`.
SHA-256 ZIP: `efbf7873508186fc790bee725e2052fe2e410d5e5b6c888f8dd18b8ea56544a9`.
Sửa trên bản sao riêng; ZIP gốc giữ nguyên. Không commit/push thay người dùng.

Phát hiện tham chiếu: báo cáo kỹ thuật ngày 11/10/2026 và `repro_scripts.zip`.
Các fixture đã được viết lại để không có đường dẫn tuyệt đối của máy đánh giá,
chỉ sử dụng fake_dsh, context canary sao chép vào tmp_path, không gọi model/API hay thiết bị.

## Thay đổi

| File | Vai trò |
| --- | --- |
| `src/harness_agent/runner/pipeline.py` | Chặn TSA termination trước publication; replay gate trước render bằng authority gốc |
| `tests/fixtures/untrusted_dsh.py` | Runtime giả: forge staged/finalized, sửa riêng role hoặc level, thay context trên đĩa |
| `tests/test_runner_trust.py` | 24 test: khai thác, đối chứng âm, ca sạch, giữ semantics ablation, marker hỏng |
| `DECISIONS.md` | D-044, trạng thái ĐỀ XUẤT; mô tả control cuối và giới hạn |
| `CLAUDE.md` | Sửa nhận định role/patch không thể bị runtime thay đổi |
| `docs/RUNNER_TRUST_FIX.md` | Báo cáo này |

Runner tái sử dụng `gate.evaluate`, không sao chép policy sang một bộ luật khác.
Nó giữ context đã nạp trước dsh trong bộ nhớ, role ban đầu và enforced gate steps của mức ban đầu.
Replay dùng prefix `staged[:index]`, nên thao tác sau vẫn có thể tham chiếu VLAN/ACL vừa được tạo trước đó.
Không nạp lại authority từ context trên đĩa, patch, argv hoặc trace sau khi dsh chạy.

Một reject thật của harness vẫn giữ `status=ok`, `verdict=reject`, `stopped_by=G3/G4/...`.
Nếu staging/finalize vượt qua các kiểm tra cũ nhưng không qua replay, kết quả là
`status=error`, `error_code=HARNESS_STATE_INVALID`, không verdict, không candidate/package.
Không đổi lỗi giả mạo thành một reject nghiệp vụ bình thường.

Sau TSA, các lỗi stage và gate reject đã có vẫn giữ ưu tiên. Nếu không có gate reject, một marker
TERMINATED luôn ngăn publication: credential_leak → SECRET_IN_ARGS; marker lạ/rỗng/hỏng →
HARNESS_STATE_INVALID. Không đưa nội dung marker không tin cậy vào thông báo lỗi.
Đã finalize rồi mới gọi tool chứa secret cũng không được sinh package.

B0 không đổi. B1/B2 replay G1–G2, B3/B4 replay G1–G6. Có event mới `runner_gate_replay`.
Không đổi contract/schema, prompt, model, dependency, CI, template hoặc golden output.
Một số khoảng trắng trong pipeline được chuẩn hoá bởi Ruff khi sửa file.

## Kiểm thử

Môi trường: Linux / Python 3.12.14; mcp 1.30.0; pydantic 2.14.0;
pytest 9.1.1; pytest-xdist 3.8.0; Ruff 0.16.9.

- Test mới: **24 passed in 13.39s** (`pytest tests/test_runner_trust.py -n 4`).
- Ruff toàn bộ repo: **All checks passed**.
- Chạy khô: **16/16 đúng kỳ vọng ở từng B0, B1, B2, B3, B4 (80/80)**.
- Toàn bộ suite: **996 passed, 1 xfailed** (153.35s, pytest -n 4); XML kèm gói.

Đối chứng âm gỡ riêng replay hoặc kiểm TERMINATED bằng monkeypatch bên trong test:
probe tương ứng phải quay lại accept. Khi control bật, cùng probe phải ERROR và không có CLI package.
Đây là test tái hiện lỗ hổng, không phải ví dụ được phép áp cấu hình.

Các ca sạch create_vlan rồi set_access_vlan vẫn accept ở B1–B4. B1/B2 vẫn không cưỡng chế RBAC.
Harness thật trong lượt fake vẫn trả reject G3/G4 bình thường. Sửa riêng role và riêng level đều
không qua được kiểm tra cuối ở B4. Sửa context trên đĩa ở TSA không đổi snapshot runner dùng để replay.

## Giới hạn còn lại

- Đây là kiểm tra artifact trước render, không phải chứng thực harness hoặc sandbox hệ điều hành.
  dsh vẫn có thể thay argv; sửa này chặn đầu ra trái policy do argv/file bị giả mạo. Nếu runtime cùng
  user có thể sửa code runner, config hoặc output sau khi runner kết thúc, cần cách ly OS để bảo vệ.
- Không tuyên bố trace/session log được xác thực. File phiên cùng quyền không tự tạo ranh giới tin cậy.
- Chưa sửa model mismatch A3, link token web A6, retry A7, evaluator, intent grounding hoặc dataset.
- Grounding xfail cũ giữ nguyên. CLI correctness/sensors ngoài phạm vi patch; Batfish vẫn skipped.
- Chưa chạy trên Windows hoặc dsh/model thật với patch này. Các lượt web trước patch không thay thế
  kiểm thử mới. Người vận hành cần chạy hướng dẫn offline trên Windows trước khi PR/merge.
- D-044 là ĐỀ XUẤT, không tự nâng thành quyết định của nhóm/GVHD.

## Windows validation — 2026-10-13

- Targeted Ruff: All checks passed.
- Runner trust regression tests: 24 passed in 88.74s.
- Fixed UTF-8 encoding in the Python executable selection test.
- Python executable selection tests: 4 passed, 48 deselected.
- Full suite: 995 passed, 1 skipped, 1 xfailed in 1106.71s.
- Results reported from the operator's Windows machine.
- No model API calls or deployment during validation.