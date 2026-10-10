# harness-agent

LLM agent được bảo vệ bởi harness, sinh cấu hình Cisco IOS từ intent ngôn ngữ tự nhiên.
Đồ án tốt nghiệp IAP491. **Không deploy**: đầu ra là change package để con người duyệt.

## Cài đặt

Cần Python ≥ 3.11. Mọi lệnh chạy ở gốc repo.

### Windows (PowerShell)

```powershell
git clone https://github.com/hoangngocduan28/cisco-harness-agent.git
cd cisco-harness-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Nếu báo lỗi "running scripts is disabled on this system", chạy dòng dưới một lần rồi Activate lại:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"
Copy-Item .env.example .env   # điền DEEPSEEK_API_KEY, KHÔNG commit file .env
python -m pytest              # phải xanh trước khi làm bất cứ việc gì
ruff check .
```

Nhập API key bằng Read-Host -AsSecureString, không gõ key trực tiếp vào PowerShell.

### Linux / macOS

```bash
git clone https://github.com/hoangngocduan28/cisco-harness-agent.git && cd cisco-harness-agent
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # điền DEEPSEEK_API_KEY, KHÔNG commit file .env
pytest                        # phải xanh trước khi làm bất cứ việc gì
```

## Cấu trúc và người sở hữu

| Thư mục | Nội dung | Vai |
| --- | --- | --- |
| `src/harness_agent/models/` | **Contract dùng chung** (Pydantic). Khoá từ 01/10 | TV2, cả nhóm duyệt |
| `src/harness_agent/agents/` | Context-Building Agent, Tool-Selection Agent | TV1 |
| `src/harness_agent/llm/` | DeepSeek client, record/replay | TV1 |
| `src/harness_agent/baselines/` | B0: LLM sinh CLI trực tiếp | TV1 |
| `src/harness_agent/runner/` | Experiment runner | TV1 |
| `src/harness_agent/corpus/` | Corpus generator, paraphrase | TV2 |
| `src/harness_agent/eval/` | Evaluator độc lập | TV2 |
| `src/harness_agent/tools/` | Tool registry | TV3 |
| `src/harness_agent/tools/templates/` | Jinja2 templates (thư mục template duy nhất) | TV3 |
| `src/harness_agent/sensors/` | Syntax checker, CIS subset | TV3 |
| `lab/eve/` | Lab EVE-NG, log kiểm tra | TV3 |
| `src/harness_agent/harness/` | Projection (ii), gate (iii), trace (v), MCP server | TV4 |
| `spikes/` | Thử nghiệm dùng một lần (dsh + MCP, Batfish) | TV1, TV3 |
| `examples/` | JSON mẫu hợp lệ và không hợp lệ cho contract | TV2 |
| `schemas/` | JSON Schema sinh tự động từ contract | tự động |
| `tests/` | Test pytest cho `src/` (CI chạy thư mục này) | người viết code tương ứng |

## Contract

Luồng dữ liệu:

```text
TestCase(intent, role, NetworkContext)
   │
   ├─ role ─────────────────────────────────► harness (không qua prompt)
   ▼
Context-Building Agent ──► ChangeRequest(changes: [ToolCall])
                               │  (i) validation, gate (iii)
                               ▼
Tool-Selection Agent ────► tool call: {tool, args}
                               │  gate (iii)
                               ▼
                         Jinja2 render ──► CLI ──► sensors (iv) ──► change package
```

Tool call có dạng `{"tool": <tên>, "args": {...}}`. Tool và tham số do tool registry
(`src/harness_agent/tools/registry/`) định nghĩa, là nguồn duy nhất (D-009): `create_vlan`,
`set_access_vlan`, `set_trunk`, `set_interface_ip`, `set_interface_state`, `set_interface_description`,
`add_static_route`, `remove_static_route`, `add_acl_entry`, `apply_acl`, `set_aaa_login`.
Xem ví dụ trong `examples/valid/`.

Xuất JSON Schema (dùng cho structured output của LLM):

```bash
python -m harness_agent.models.export_schema
```

## Quy trình làm việc

- Nhánh: `tv<n>/<task-id>-<tên-ngắn>`, ví dụ `tv3/3.1-tool-registry`.
- Merge vào `main` qua PR, cần 1 người duyệt và CI xanh (`ruff check .` + `pytest`).
- **Definition of Done:** đã merge, có test, đạt tiêu chí nghiệm thu trong kế hoạch, có mục
  trong `DECISIONS.md` nếu thay đổi thiết kế.
- Muốn sửa `src/harness_agent/models/`: mở issue trước, cả nhóm đồng ý, ghi `DECISIONS.md`.
- Chưa có code của người khác thì viết stub theo đúng contract, đừng chờ.

## Bảo mật repo

- API key chỉ nằm trong `.env`. Lỡ commit key thì **thu hồi key ngay** trên trang DeepSeek;
  xoá khỏi lịch sử git là chưa đủ.
- `examples/` và corpus chỉ chứa dữ liệu tổng hợp; `secret_hash` là giá trị giả.
