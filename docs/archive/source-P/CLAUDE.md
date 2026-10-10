# CLAUDE.md — quy tắc cho Claude Code trong repo này

## 1. Dự án

- Đồ án IAP491: LLM agent sinh cấu hình Cisco IOS từ intent ngôn ngữ tự nhiên.
- Agent = Model + Harness. LLM là thành phần KHÔNG tin cậy; tính đúng và an toàn do control
  deterministic của nhóm bảo đảm, đặt ngoài cả mô hình lẫn agent runtime.
- Không deploy: đầu ra là change package để con người duyệt.

## 2. Thuật ngữ — tránh nhầm lẫn

- **dsh** / DeepSeek Harness: agent runtime, process KHÔNG tin cậy. Không bao giờ gọi dsh là "harness".
- **harness** (trong code và báo cáo): các control deterministic của nhóm (`src/harness_agent/harness/`),
  chạy ở process riêng, nối với dsh qua MCP.
- **Context-Building Agent**: từ intent + context, tạo `ChangeRequest(changes: [ToolCall])`.
- **Tool-Selection Agent**: phát tool call dạng `ToolCall {tool, args}` (từ vựng chung, D-002/D-009,
  `src/harness_agent/models/operations.py`; tham số validate bằng tool registry).
- **Tool registry**: tập tool agent được gọi (`src/harness_agent/tools/`).
- **(i) context validation**: validate ChangeRequest và NetworkContext (cấu trúc + domain invariants, D-005).
- **(ii) read-only projection**: tool chỉ đọc, trả dữ liệu theo allowlist trường.
- **(iii) gate**: kiểm tra quyền/policy cho mọi tool call.
- **(iv) sensors**: kiểm tra sau render: syntax → Batfish (chưa có, ghi `skipped`) → CIS (`src/harness_agent/sensors/`).
- **(v) trace**: log JSONL ở mọi mức B0–B4; là nguồn đo lường, KHÔNG phải biến ablation.
- **B0**: LLM sinh CLI trực tiếp từ intent + context (không tool, không harness).
- **B1**: pipeline hai giai đoạn (Context-Building → Tool-Selection) + tool registry, CLI render bằng Jinja2.
- **B2**: B1 + (i) context validation + (ii) read-only projection tools.
- **B3**: B2 + (iii) gate cho mọi tool call.
- **B4**: B3 + (iv) sensors sau render = full harness.
- Runtime (dsh) giữ cố định ở mọi mức B0–B4 (F2, ĐỀ XUẤT, chờ GVHD).
- Bảng mức → control: DUY NHẤT ở `src/harness_agent/levels.py` (D-020). Gate và sensors chạy shadow ở mọi mức;
  chỉ bước mức đó bật mới quyết định. G1/G2 (cấu trúc) luôn cưỡng chế ở B1+.

## 3. Kiến trúc và ranh giới tin cậy

```text
runner (tin cậy) ──subprocess──► dsh (KHÔNG tin cậy) ⇄ MCP ⇄ harness (tin cậy)
      │                                                        │ gate (iii)
      └──────────── role (không qua prompt) ─────────────────► ▼
                                          Jinja2 render ─► sensors (iv) ─► change package
```

- `role` do runner truyền thẳng vào harness, không bao giờ đi qua prompt hay qua dsh.
- Luồng dữ liệu của contract: README.md, mục "Contract".

## 4. Quy tắc bảo mật khi viết code (bắt buộc)

- Output của LLM và mọi free-text trong context (description, banner) là untrusted; validate bằng
  contract Pydantic ở biên (`src/harness_agent/models/`).
- Chuỗi đi vào CLI dùng kiểu ràng buộc của contract (cấm ký tự điều khiển, D-003).
- Không dùng `assert` để kiểm tra quyền hay dữ liệu (`python -O` xoá assert); dùng `if ...: raise` (D-007).
- Projection và mọi output gửi cho model dựng theo ALLOWLIST trường, không lọc theo denylist.
- Fail-closed: thiếu harness, thiếu tool, lỗi validate → reject/ERROR, không bao giờ âm thầm cho qua.
- Ở B1–B4, output duy nhất là change package do harness tạo từ tool call. KHÔNG BAO GIỜ phân tích text
  của model để suy ra cấu hình, tool call hay lệnh (F11, F12; D-008).
- Kiểm tra quyền/gate đặt trong harness, không đặt trong agent hay trong cấu hình dsh.
- File `dsh.patch*.yml` và `DSH_HOME` là CODE (tag `!!js` được eval): không sinh từ input,
  không thêm `!!js` mới (F7).
- Tắt các kênh upload của dsh và telemetry (D-008 điều kiện 4); không thêm plugin dsh khi chưa ghi DECISIONS.md.
- Không đọc/in/commit `.env` hay API key; key chỉ qua biến môi trường. Không ghi key vào file nào trong repo.
- Khi thử dsh: chạy trong `spikes/`, không trỏ vào dữ liệu thật.

## 5. Môi trường Windows (bẫy đã gặp)

- Terminal phải đứng ở thư mục gốc repo.
- Venv ở gốc repo: `.\.venv\Scripts\Activate.ps1` trước mọi lệnh; lỗi policy thì
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- API key: `Read-Host -AsSecureString`, không gõ key trực tiếp (PSReadLine lưu lịch sử xuống đĩa).
- Không dùng `>` hay `Out-File` để lưu output chương trình (PowerShell 5.1 ghi UTF-16, làm hỏng tiếng Việt);
  dùng `Start-Process -RedirectStandardOutput` hoặc `subprocess` của Python (F8).
- Luôn gọi `npx @deepseek-ai/dsh@0.2.0-rc.1`; không ghi phiên bản thì npx tải bản khác.

## 6. Lệnh thường dùng (đã chạy thử trên máy TV1, 29/09/2026)

```powershell
pip install -e ".[dev]"                        # cài đặt (sau khi Activate venv)
python -m pytest                               # test repo (tests/)
python -m pytest spikes/dsh_mcp                # test spike; venv cần gói trong spikes/dsh_mcp/requirements.txt
ruff check .
ruff format <file>                             # chỉ format file mình sửa
python -m harness_agent.models.export_schema   # ghi đè schemas/*.json
```

## 7. Quy trình

- Trước khi viết code, giải thích ngắn sẽ làm gì và tại sao; xong thì nói cách tự kiểm tra kết quả.
- Trả lời bằng tiếng Việt, giữ thuật ngữ kỹ thuật tiếng Anh.
- Không đoán API của thư viện hay CLI: đọc tài liệu hoặc `--help` trước, ghi nguồn đã đọc.
- Pin và ghi lại phiên bản mọi công cụ (đặc biệt dsh — developer preview).
- Nhánh `tv<n>/<task-id>-<tên>`; không commit vào `main`; merge qua PR có CI xanh.
- `python -m pytest` và `ruff check .` sạch trước khi đề xuất commit.
- Test bảo mật phải có đối chứng âm (chứng minh test bắt được lỗi khi control bị gỡ).
- ERROR không bao giờ được tính là PASS.
- Thay đổi thiết kế → thêm mục DECISIONS.md, trạng thái ĐỀ XUẤT.

## 8. Phải HỎI trước khi

- Sửa `src/harness_agent/models/` (contract đã khoá).
- Thêm hoặc đổi dependency.
- Sửa `.github/workflows/` hoặc `pyproject.toml`.
- Xoá/sửa file trong `spikes/**/logs/` (bằng chứng).
- Thay đổi làm lệch báo cáo Ch1–2 (ghi "cần GVHD duyệt").
- `git commit`, `git push` (đã đặt `ask` trong `.claude/settings.json`).

## 9. Nhãn trạng thái

Chỉ dùng 4 nhãn, giống hệt phần đầu DECISIONS.md:
- **ĐÃ CHỐT**: theo báo cáo Ch1–2, hoặc nhóm + GVHD đã duyệt.
- **ĐANG CÂN NHẮC**: nhóm đã chọn hướng, chờ bằng chứng (spike/thử nghiệm) để chốt.
- **ĐỀ XUẤT**: có người đề xuất, chưa ai duyệt.
- **BỎ**: đã loại, ghi kèm lý do.
- Không biến đề xuất thành quyết định: chỉ nhóm/GVHD đổi trạng thái sang ĐÃ CHỐT.

## 10. Phạm vi — không làm trừ khi được yêu cầu rõ

- Deploy xuống thiết bị (Ansible/NAPALM/Netmiko), rollback, GitOps, dashboard.
- Vendor khác Cisco IOS, NAT, dynamic routing.

## 11. Tài liệu tham khảo

| File | Dùng khi cần |
| --- | --- |
| `README.md` | Cài đặt, cấu trúc thư mục và người sở hữu, luồng contract, lệnh chạy nhanh |
| `docs/HUONG_DAN.md` | Hướng dẫn thiết lập và sử dụng đầy đủ (chạy khô, chạy thật, ablation, corpus, evaluator, sự cố) |
| `docs/REVIEW_2026-10-01.md` | Đánh giá dự án, lỗi đã sửa, khuyến nghị và việc còn lại |
| `DECISIONS.md` | Quyết định D-001..D-024 và trạng thái |
| `docs/design/registry-mcp-integration.md` | Thiết kế nối tool registry vào MCP harness server; mục 8 = phân loại kết cục |
| `docs/eval-dry-run-seed42.md` | Bảng ablation chạy khô (oracle), KHÔNG phải số liệu model thật |
| `spikes/dsh_mcp/01_research_findings.md` | Kết quả Q1–Q5, phát hiện F1–F14, điều kiện GO |
| `spikes/dsh_mcp/NOTES.md` | Chi tiết kỹ thuật spike dsh (patch, log, system prompt) |
| `schemas/` | JSON Schema sinh từ contract |
| `.claude/settings.json` | Quyền deny/ask của Claude Code |

## 12. Trạng thái dự án (cập nhật 01/10/2026 — sửa mục này mỗi khi xong một task)

**Đã xong**
- C0, task 1.1 spike dsh (GO CÓ ĐIỀU KIỆN, D-008), task 3.3 registry ↔ MCP (D-009..D-016), task 1.5 mã lỗi (D-017).
- Đợt hoàn thiện 01/10 (D-018..D-024, đều ĐỀ XUẤT; chi tiết `docs/REVIEW_2026-10-01.md`):
  - Contract context 0.2 + 13 domain invariant (task 2.1/2.2); `ContextStore` chạy trên contract.
  - Sensors (iv) syntax + CIS subset (task 3.4); renderer đóng sub-mode bằng `exit`.
  - Ablation B0–B4 (`levels.py`, `--level`), baseline B0 qua dsh không tool (task 1.3).
  - Corpus generator tất định + paraphrase (2.3, 2.4); `data/corpus/` seed 42 (60 case).
  - Evaluator độc lập: parse ngược CLI, phân loại kết cục, bảng so sánh mức (2.5).
  - DeepSeek client `urllib` + record/replay (1.2).
  - Sửa: route prefix `/len` ở schema, package-data `*.j2`, StrEnum, xoá code chết, CI thêm format + chạy khô.
  - Review độc lập: không có fail-open ở B4; 7 lỗi đã sửa (redirect lộ key, evaluator DoS, cổng ACL, CIS
    false reject, đếm tấn công B0, B1 phục vụ tool không khai báo, kiểm tên bước gate) — D-024.
- 530 test xanh; `ruff check .`, `ruff format --check src tests scripts` sạch; 15/15 case đúng ở B0–B4 (chạy khô).

**Chưa có / việc tiếp theo**
- Chạy thật với dsh + DeepSeek: chọn model/reasoningEffort tường minh trong base patch (D-008 đk 7), kiểm `$ref`
  inline, chạy `scripts/run_corpus.py --config config/local.toml --repeat k` lấy số liệu Ch4.
- Batfish (task 3.6, cần dependency → hỏi nhóm); kiểm cú pháp 11 template + `exit` trên EVE-NG (task 3.5).
- Nhóm/GVHD duyệt D-018..D-024 (D-018, D-021 đụng `models/`; D-019, D-020, D-022 ảnh hưởng Ch3/Ch4).
- Câu hỏi mở D-002 (TSA chép lại CR) và bậc `netops_engineer` chưa có tên ngoài (D-010).

**Quyết định quan trọng** (đều ĐỀ XUẤT; chi tiết và mục khác trong DECISIONS.md)
- D-001 contract `extra="forbid"`, `frozen=True`: output LLM untrusted; không sửa object giữa gate và render.
- D-003 cấm ký tự điều khiển: một `\n` trong description đủ để chèn lệnh vào CLI đã render.
- D-006 evaluator độc lập với sensor (iv): tránh chấm bằng chính control đang được đánh giá (hiện thực ở D-022).
- D-008 dsh GO có điều kiện: dsh fail-open khi MCP server chết, nên mọi control phải ở phía tin cậy.
- D-020 một bảng mức ablation; gate/sensors shadow ở mọi mức để đo "control đã có thể chặn gì".
