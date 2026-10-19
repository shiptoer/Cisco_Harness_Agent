# harness-agent

> **Bắt đầu ở đây:** [`docs/HUONG_DAN_SU_DUNG.md`](docs/HUONG_DAN_SU_DUNG.md) (bản Word:
> `docs/HUONG_DAN_SU_DUNG.docx`) là hướng dẫn sử dụng đầy đủ: cài đặt Windows/Linux, chạy khô, chạy thật, UI duyệt,
> MCP bridge, collector, `cha-check`, `cha-apply`, thí nghiệm, mã lỗi, bảo mật, câu hỏi thường gặp. Thay đổi theo đợt:
> [`CHANGELOG.md`](CHANGELOG.md).

LLM agent được bảo vệ bởi harness, sinh cấu hình Cisco IOS từ intent ngôn ngữ tự nhiên.
Đồ án tốt nghiệp IAP491. **Không deploy**: đầu ra là change package để con người duyệt (ngoại lệ duy nhất, thủ công:
`cha-apply`, mục "Áp cấu hình đã duyệt").

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

### Chạy test

```bash
pytest -m "not slow"          # khi đang sửa code: bỏ các test sinh subprocess, xong trong vài giây
pytest -n auto                # bộ đầy đủ, chạy song song (pytest-xdist); CI chạy đúng lệnh này
pytest                        # bộ đầy đủ, tuần tự: cùng tập test, chậm hơn
```

Test nào sinh subprocess (fake_dsh, harness MCP server, dsh) mang `@pytest.mark.slow`. `pytest` không tham số vẫn
chạy tất cả. Trước khi đề xuất commit: bộ đầy đủ và `ruff check .` phải sạch. Số đo ở `docs/findings/test-speed.md`.

## Cấu trúc và người sở hữu

| Thư mục | Nội dung | Vai |
| --- | --- | --- |
| `src/harness_agent/models/` | **Contract dùng chung** (Pydantic). Khoá từ 01/10 | TV2, cả nhóm duyệt |
| `src/harness_agent/levels.py` | Mức ablation B0–B4: mức nào bật control nào (một nơi duy nhất, D-049) | TV1 |
| `src/harness_agent/llm/` | DeepSeek client, cache record/replay (port từ bản gộp của Khang, D-049) | TV1 |
| `src/harness_agent/runner/` | Experiment runner: pipeline hai stage (Context-Building, Tool-Selection chạy trong dsh với `prompts/cba*.md`, `prompts/tsa*.md`), intent grounding (D-061); B0 ở `runner/b0.py` + `prompts/b0.md`, chạy bằng `--level B0` (D-044) | TV1 |
| `src/harness_agent/ui/` | UI vận hành `cha-ui`: duyệt change package, xuất `.cfg`, không deploy (D-057) | TV1 |
| `src/harness_agent/web/` | MCP bridge nhiều yêu cầu cho khách của dsh (`scripts/web_multi_server.py`) | TV1 |
| `src/harness_agent/check/` | `cha-check`: kiểm một file cấu hình bất kỳ với context, không LLM | TV1 |
| `src/harness_agent/apply/` | `cha-apply`: áp cấu hình đã duyệt xuống một thiết bị, mặc định dry-run, ngoài lô đo (D-064) | TV1 |
| `src/harness_agent/collector/` | Thu network context từ lab qua SSH, chỉ đọc (D-047) | TV1 |
| `src/harness_agent/corpus/` | Corpus generator, paraphrase (`scripts/gen_corpus.py`) | TV2 |
| `src/harness_agent/eval/` | Evaluator độc lập (`scripts/evaluate.py`) | TV2 |
| `src/harness_agent/tools/` | Tool registry | TV3 |
| `src/harness_agent/tools/templates/` | Jinja2 templates (thư mục template duy nhất) | TV3 |
| `src/harness_agent/sensors/` | Syntax checker, CIS subset; Batfish chưa có. Cưỡng chế ở B4, shadow ở B0–B3 | TV3 |
| `lab/eve/` | Lab EVE-NG, log kiểm tra | TV3 |
| `src/harness_agent/harness/` | Projection (ii), gate (iii), trace (v), MCP server | TV4 |
| `spikes/` | Thử nghiệm dùng một lần (dsh + MCP, Batfish) | TV1, TV3 |
| `examples/` | JSON mẫu hợp lệ và không hợp lệ cho contract | TV2 |
| `schemas/` | JSON Schema sinh tự động từ contract | tự động |
| `tests/` | Test pytest cho `src/` (CI chạy thư mục này) | người viết code tương ứng |

## Mức ablation

`--level B0..B4` (mặc định B3) ở `python -m harness_agent.runner` và `scripts/run_cases.py`; `--condition B0|B3`
là tên cũ, vẫn nhận nhưng có cảnh báo. Bảng mức nào bật control nào: docstring của `src/harness_agent/levels.py`.
Kỳ vọng fixture của từng mức: `data/scenarios_b0/`, `data/scenarios_b1/`, `data/scenarios_b2/`, `data/scenarios/`
(B3 và B4). `--confirmed-intent` và `--prepare-only` tắt mặc định và không dùng trong lô đo.

## Benchmark

`data/benchmark/v1` (D-065) là bộ dataset chuẩn chấm **độ chính xác đầu ra**: 108 yêu cầu hợp lệ (88 một thao tác:
11 tool × 4 case × 2 context `campus-small-01`, `smb-01`; 20 đa thao tác, 2–3 thao tác/case), intent VI + EN từ
template có tham số (không LLM), role `security_admin`. Mỗi case có gold ở hai mức: tool call kỳ vọng và CLI
`gold/<case_id>/<thiết bị>.cfg` render bằng chính renderer của hệ thống; sha256 của mọi file nằm trong
`manifest.json`. Split dev/test (78/30) chia theo template: không template nào ở cả hai split. Corpus v2
(`data/corpus/v2`) vẫn là bộ đo an toàn; benchmark không có case tấn công.

```bash
python scripts/gen_benchmark.py                       # sinh lại (seed 42, per_tool 4); test ghim dữ liệu đã commit
python scripts/run_corpus.py --config config/fake.toml --corpus data/benchmark/v1 --level B4 --limit 10 --out runs/bm
python scripts/evaluate.py --benchmark data/benchmark/v1 --runs runs/bm   # bảng markdown + reports/benchmark/summary.json
```

Đọc bảng: mỗi hàng là một nhóm (tổng, `split`, `topology` = context, `tool`; case nhiều thao tác ở `tool: multi_op`),
mỗi cột là một tỉ lệ kèm khoảng Wilson 95% `[lo, hi]`:

| Cột | Đạt khi |
| --- | --- |
| ops strict | accept, thao tác evaluator phân tích ngược từ `candidate/*.cfg` trùng nhãn, không dòng nào ngoài thao tác |
| ops lenient | như trên nhưng bỏ qua dòng vô hại (`configure terminal`, `exit`, `end`, `show …`) |
| CLI exact | accept, từng file cấu hình trùng gold sau khi bỏ khoảng trắng cuối dòng và dòng trống |
| CLI lenient | như trên, bỏ thêm thụt đầu dòng và dòng vô hại |
| ERROR | lượt ERROR: không bao giờ tính là đạt |

Kết cục từng case: `cli_exact` > `cli_lenient` > `ops_only` > `wrong_config`; `not_accepted` (reject/clarify),
`error`. Có `--repeat k` thì các lượt được gom theo case (đa số, hoà lấy kết cục tệ hơn) và bảng thêm phần pass^k.
**Kết quả chạy bằng `config/fake.toml` là oracle** (`fake_dsh` nộp đúng nhãn): nó kiểm đường ống và nhãn, không đo
model; chỉ số đo model khi chạy với `config/local.toml` và model thật. Context smb-01 của benchmark là bản dẫn xuất
`data/contexts/smb-01-collected-ref-l2.json` (`python scripts/gen_benchmark.py --derive`): snapshot SSH gốc với
platform `iosvl2` cho năm switch IOL L2; snapshot gốc giữ nguyên byte (D-047, D-055).

## Hai giao diện duyệt

Cả hai chỉ chạy trên máy cục bộ (bind 127.0.0.1), đầu ra là change package để con người duyệt; **không giao diện
nào deploy xuống thiết bị**. Không dùng trong lô đo.

| Giao diện | Dành cho | Cách chạy | Ghi chú |
| --- | --- | --- | --- |
| UI vận hành (`cha-ui` = `python -m harness_agent.ui`, wrapper cũ `scripts/ui_server.py`; `src/harness_agent/ui/`, D-057) | Người vận hành: nhập intent, xem kết cục và change package, duyệt/từ chối, xuất `.cfg` sau khi duyệt | `cha-ui --config config/fake.toml --context data/contexts/campus-small-01.json --role network_admin --level B3 --reviewer "<tên>"` | Mỗi lượt là một process `python -m harness_agent.runner` (B3/B4); role, mức, context cố định lúc khởi động |
| MCP bridge nhiều yêu cầu (`scripts/web_multi_server.py`, `src/harness_agent/web/`) | Khi khách của dsh (web/IDE) muốn gửi yêu cầu qua chat và người duyệt từng đề xuất trên trình duyệt | `scripts/start_web.ps1 -MultiRequest -Context <file> -Role <role>` (Windows; thêm `-CheckOnly` để kiểm offline) | Model chỉ có 3 tool: chuẩn bị, xem trạng thái, sinh CLI sau khi người duyệt; không có tool duyệt. Link duyệt có token chỉ in ra terminal của bridge, không trả cho model. Chi tiết: `docs/MULTI_REQUEST_WEB.md` |

Chế độ một contract cố định (`start_web.ps1 -Contract <file>`, `scripts/web_pipeline_server.py`) và dữ liệu mẫu
`data/confirmed_intents/`, `data/web-injection-if-001/`: `docs/SHARED_WEB_LAUNCHER.md`.

## Kiểm một file cấu hình bất kỳ (`cha-check`)

Kiểm một file cấu hình Cisco IOS (do người viết, do B0 sinh, hay `candidate/<device>.cfg` của một lượt chạy) với
một network context, **không gọi LLM, không deploy, không ghi file**. Công cụ ghép các control có sẵn, không thêm
control mới (`src/harness_agent/check/`):

```bash
cha-check candidate/SW1.cfg --context data/contexts/campus-small-01.json         # bảng theo device
cha-check SW1.cfg R1.cfg --context ctx.json --role network_admin --json         # {"devices": {...}, "exit_code": n}
python -m harness_agent.check running.txt --device SW1 --context ctx.json       # tên file không phải <device>.cfg
```

`cha-check` có sau `pip install -e .`; không có thì dùng `python -m harness_agent.check`.

- Tên file là `<device>.cfg` (device = tên file bỏ đuôi), hoặc `--device` khi chỉ có một file. Device phải có trong
  context. Mọi file của một lần gọi được kiểm như **một change** (giới hạn số device/interface của G4 tính chung).
- Context được kiểm bằng contract + invariants như runner (control (i)) trước khi dùng.
- Mã thoát: **0** không có finding (ngoài mức `info`), **1** có finding, **2** lỗi dùng: file không đọc được,
  context sai contract, role lạ, device không có trong context; thông báo ở stderr.

| `kind` | `code` | Mức | Nguồn |
| --- | --- | --- | --- |
| `unparsed_line` | `UNPARSED` | info | dòng `eval.cli_parse.parse_config` không phân tích được; dòng vô hại (`configure terminal`, `show …`) bỏ qua |
| `risky_line` | `RISKY_LINE` | critical | dòng khớp `RISKY_LINE` của evaluator (`username …`, `enable secret`, `no aaa`, …), quét mọi dòng |
| `gate` | bước fail đầu tiên, vd `G4_blast_radius` | fail | từng thao tác suy ra được phát lại qua gate (iii), role mặc định `security_admin`, cưỡng chế đủ 6 bước; G5 không có ý nghĩa (không có change request) |
| `sensor` | mã của sensor, vd `SYN_MISSING_END` | theo sensor | `sensors.run_sensors`: syntax → Batfish (bỏ qua nếu chưa có, ghi ở dòng `sensors:`) → CIS |
| `secret_in_config` | `SECRET_IN_CONFIG` | critical | giá trị secret của context xuất hiện trong file: chỉ ghi device và **số dòng** |

Secret của context không bao giờ được in ra stdout/stderr: mọi dòng được trích (kể cả `risky_line`) đi qua
`harness.leak.redact_value` ở một điểm, che trước rồi mới cắt ngắn (`tests/test_check_cli.py`, có đối chứng âm).

## Áp cấu hình đã duyệt (`cha-apply`)

> **Cảnh báo.** Đây là đường DUY NHẤT trong repo gửi cấu hình xuống thiết bị thật (D-064). Chỉ dùng trên lab
> (smb-01) hoặc thiết bị bạn được phép đổi cấu hình, **không bao giờ trong lô đo**, không tự động hoá. Rollback
> chỉ là best-effort, không thay được `configure replace`; apply **không** `write memory`. Đọc
> `docs/design/apply-controlled.md` trước khi commit lần đầu.

Áp `candidate/<device>.cfg` của một lượt B3/B4 **đã được duyệt** trên UI (bản ghi `human_review` approve, sha256
của `.cfg` còn trùng bản duyệt, verdict accept, không rò secret, chuỗi trace liền mạch, mọi dòng đúng dạng lệnh
của template) xuống MỘT thiết bị qua SSH.
Mặc định là dry-run: chỉ in lệnh, không kết nối.

```bash
cha-apply --run-dir runs/ui/<run_id> --device SW2 --inventory lab/smb-01/inventory.json             # dry-run
cha-apply --run-dir runs/ui/<run_id> --device SW2 --inventory lab/smb-01/inventory.json \
          --commit --confirm <run_id>                                                             # áp thật
```

- Trang của lượt đã duyệt trên UI in sẵn lệnh dry-run cho từng thiết bị (run-dir điền sẵn, inventory để
  placeholder `<inventory.json>`); UI không có nút chạy apply.
- Cần `pip install -e ".[demo]"` (Netmiko 4.8.0, paramiko 4.0.0) khi commit; dry-run không cần. `cha-apply` có
  sau `pip install -e .`; không có thì `python -m harness_agent.apply`.
- SSH: `COLLECTOR_USER` / `COLLECTOR_PASSWORD` (như collector), tài khoản privilege 15. Placeholder
  `<SECRET:ref>` trong cấu hình lấy từ `CHA_SECRET_<REF>` lúc gửi (dry-run in tên biến cần đặt); giá trị không
  bao giờ vào file hay trace.
- Commit: lưu `show running-config` vào `<run-dir>/apply/<device>.pre.cfg`, gửi cấu hình, lấy lại running-config
  (`.post.cfg`), kiểm từng dòng; thiếu dòng thì rollback (`.rollback.cfg`). Mọi bước ghi vào `trace.jsonl` của
  lượt chạy (actor `apply`). Backup của lần áp trước không bị ghi đè: muốn áp lại, chuyển thư mục `apply/` đi.
- Áp xong, kiểm thiết bị rồi tự `write memory` nếu muốn giữ cấu hình.
- Mã thoát: **0** dry-run xong hoặc áp xong và kiểm đủ; **1** kiểm thiếu dòng (đã rollback) hoặc lỗi sau khi đã bắt
  đầu gửi; **2** bị từ chối (chưa duyệt, `.cfg` đổi sau duyệt, xác nhận sai, thiếu secret, không kết nối được…)
  hoặc sai tham số.

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
- Merge vào `main` qua PR, cần 1 người duyệt và CI xanh (`ruff check .` + `pytest -n auto`).
- **Definition of Done:** đã merge, có test, đạt tiêu chí nghiệm thu trong kế hoạch, có mục
  trong `DECISIONS.md` nếu thay đổi thiết kế.
- Muốn sửa `src/harness_agent/models/`: mở issue trước, cả nhóm đồng ý, ghi `DECISIONS.md`.
- Chưa có code của người khác thì viết stub theo đúng contract, đừng chờ.

## Bảo mật repo

- API key chỉ nằm trong `.env`. Lỡ commit key thì **thu hồi key ngay** trên trang DeepSeek;
  xoá khỏi lịch sử git là chưa đủ.
- `examples/` và corpus chỉ chứa dữ liệu tổng hợp; `secret_hash` là giá trị giả.
