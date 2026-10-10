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
- **(iv) sensors**: kiểm tra sau render: syntax → Batfish → CIS.
- **(v) trace**: log JSONL ở mọi mức B0–B4; là nguồn đo lường, KHÔNG phải biến ablation.
- **B0**: LLM sinh CLI trực tiếp từ intent + context (không tool, không harness).
- **B1**: pipeline hai giai đoạn (Context-Building → Tool-Selection) + tool registry, CLI render bằng Jinja2.
- **B2**: B1 + (i) context validation + (ii) read-only projection tools.
- **B3**: B2 + (iii) gate cho mọi tool call.
- **B4**: B3 + (iv) sensors sau render = full harness.
- Runtime (dsh) giữ cố định ở mọi mức B0–B4 (F2, ĐỀ XUẤT, chờ GVHD).

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
| `README.md` | Cài đặt, cấu trúc thư mục và người sở hữu, luồng contract |
| `DECISIONS.md` | Quyết định D-001..D-017 và trạng thái |
| `docs/design/registry-mcp-integration.md` | Thiết kế nối tool registry vào MCP harness server (task 3.3) |
| `spikes/dsh_mcp/01_research_findings.md` | Kết quả Q1–Q5, phát hiện F1–F14, điều kiện GO |
| `spikes/dsh_mcp/NOTES.md` | Chi tiết kỹ thuật spike dsh (patch, log, system prompt) |
| `schemas/` | JSON Schema sinh từ contract |
| `.claude/settings.json` | Quyền deny/ask của Claude Code |

## 12. Trạng thái dự án (cập nhật 29/09/2026 — sửa mục này mỗi khi xong một task)

**Đã xong**
- C0 (`main`): khung repo, contract nháp `src/harness_agent/models/`, `schemas/`, `examples/`, 31 test.
- Task 1.1 spike dsh + MCP: GO CÓ ĐIỀU KIỆN (D-008); nhánh `tv1/1.1-spike-dsh-mcp` đã push, chưa merge.
- Repo: `.gitattributes`, `.claude/settings.json`, README cài đặt Windows, pin `ruff==0.16.9`.

**Trạng thái từng phần**
- `models/` (TV2): nháp, khoá từ 01/10; `invariants.py` chưa làm (task 2.2).
- Stub: TV1 `llm/`, `baselines/`, `agents/`, `runner/` (task 1.2–1.6); TV2 `corpus/`, `eval/` (2.3–2.5).
- TV3 `tools/`, `sensors/`, `templates/`, `lab/eve/`: stub/rỗng (task 3.1, 3.3, 3.4); spike Batfish (3.6) chưa có.
- TV4 `harness/`: stub (task 4.2–4.5); tham khảo projection allowlist mẫu ở `spikes/dsh_mcp/cisco_server.py`.
- Task 3.3 (01/10/2026, nhánh `tv3/3.3-registry-mcp`): tool registry (11 tool) đã nối vào MCP harness server;
  một bộ tool, một gate, một renderer, một thư mục template. D-009..D-016 ở trạng thái ĐỀ XUẤT.

**Bước tiếp theo**
- Mở PR `tv1/1.1-spike-dsh-mcp` → `main`: lần đầu CI chạy (Linux, Python 3.11).
- Nhóm duyệt D-001..D-008 trước 01/10 (khoá contract); chốt câu hỏi mở của D-002.
- GVHD duyệt: tên model, B0 chạy qua dsh (D-008), D-006. Hạn kết luận GO/NO-GO dsh: 02/10.
- Task 1.2: DeepSeek client + record/replay, xác nhận tên model. Task 1.6: runner hiện thực 8 điều kiện D-008.
- `ruff format --check .` báo 4 file chưa format (có `models/common.py` → hỏi trước).

**Quyết định quan trọng** (đều ĐỀ XUẤT; chi tiết và mục khác trong DECISIONS.md)
- D-001 contract `extra="forbid"`, `frozen=True`: output LLM untrusted; không sửa object giữa gate và render.
- D-003 cấm ký tự điều khiển: một `\n` trong description đủ để chèn lệnh vào CLI đã render.
- D-006 evaluator độc lập với sensor (iv): tránh chấm bằng chính control đang được đánh giá.
- D-008 dsh GO có điều kiện: dsh fail-open khi MCP server chết, nên mọi control phải ở phía tin cậy.
