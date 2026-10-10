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
python -m pytest              # phải xanh trước khi làm bất cứ việc gì (530 test)
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

| Thư mục | Nội dung | Trạng thái (01/10/2026) | Vai |
| --- | --- | --- | --- |
| `src/harness_agent/models/` | **Contract dùng chung** (Pydantic): context 0.2, ChangeRequest, ToolCall, TestCase, invariants | có, 13 invariant `CTX_*` (D-018) | TV2, cả nhóm duyệt |
| `src/harness_agent/levels.py` | Bảng mức ablation B0–B4: mức nào bật control nào | có (D-020) | TV1 |
| `src/harness_agent/agents/` | CBA, TSA = prompt (`prompts/`) + dsh + tool của harness (xem docstring) | không có code Python | TV1 |
| `src/harness_agent/llm/` | DeepSeek client (`urllib`), record/replay | có (D-023) | TV1 |
| `src/harness_agent/baselines/` | B0: LLM sinh CLI trực tiếp qua dsh không tool | có (D-020) | TV1 |
| `src/harness_agent/runner/` | Runner một case ở mọi mức, gọi dsh, mã lỗi D-017 | có | TV1 |
| `src/harness_agent/corpus/` | Corpus generator tất định theo seed, paraphrase | có (D-021) | TV2 |
| `src/harness_agent/eval/` | Evaluator độc lập: parse ngược CLI, phân loại kết cục | có (D-022) | TV2 |
| `src/harness_agent/tools/` | Tool registry (11 tool) | có | TV3 |
| `src/harness_agent/tools/templates/` | Jinja2 templates (thư mục template duy nhất) | có | TV3 |
| `src/harness_agent/sensors/` | Sensors (iv): syntax, CIS subset; Batfish chưa có | có (D-019) | TV3 |
| `lab/eve/` | Lab EVE-NG, log kiểm tra | chưa có (task 3.5) | TV3 |
| `src/harness_agent/harness/` | Context store (i, ii), gate (iii), trace (v), MCP server | có | TV4 |
| `prompts/` | Chỉ dẫn của CBA, TSA, B0 (nhóm viết, hash ghi vào trace) | có | TV1 |
| `data/contexts/`, `data/cases/`, `data/scenarios/` | Inventory mẫu, 15 case có kỳ vọng theo mức, kịch bản fake_dsh | có | cả nhóm |
| `data/corpus/` | Corpus tham chiếu seed 42 (60 case) + oracle scenario | có | TV2 |
| `scripts/` | `check_setup`, `run_cases`, `gen_corpus`, `run_corpus`, `evaluate`, `fake_dsh` | có | cả nhóm |
| `spikes/` | Thử nghiệm dùng một lần (dsh + MCP, Batfish) | dsh xong | TV1, TV3 |
| `examples/` | JSON mẫu hợp lệ và không hợp lệ cho contract | có | TV2 |
| `schemas/` | JSON Schema sinh tự động từ contract | tự động | — |
| `tests/` | 530 test pytest (CI chạy thư mục này) | xanh | người viết code tương ứng |
| `docs/` | `HUONG_DAN.md` + `HUONG_DAN.docx` (hướng dẫn thiết lập & sử dụng), `REVIEW_2026-10-01.md`, thiết kế, kết quả chạy khô | có | — |

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

## Chạy nhanh

Hướng dẫn đầy đủ (Windows và Linux/macOS, chạy khô, chạy thật, ablation, corpus, evaluator, xử lý sự cố):
**`docs/HUONG_DAN.md`**.

```bash
python scripts/run_cases.py --config config/fake.toml                  # 15 case, mức B4, không cần API key
python scripts/run_cases.py --config config/fake.toml --level B0       # cùng case ở mức B0 (B1, B2, B3 tương tự)
python scripts/gen_corpus.py --seed 42 --out data/corpus               # sinh corpus tất định
python scripts/run_corpus.py --config config/fake.toml --level B0 B4   # chạy corpus ở nhiều mức
python scripts/evaluate.py --runs runs/corpus                          # chấm theo nhãn → reports/eval/summary.md
python -m harness_agent.runner --config config/local.toml --level B4 \
    --context data/contexts/campus-small-01.json --role network_admin \
    --intent "Tạo VLAN 30 tên FINANCE trên SW2"                       # chạy thật qua dsh + DeepSeek
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
