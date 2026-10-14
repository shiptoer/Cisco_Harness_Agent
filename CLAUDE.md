# Bản sửa model pin (D-045, ĐỀ XUẤT)

Runner kiểm tra provider/model/effort theo từng stage trước khi xuất kết quả,
dùng pin snapshot trong bộ nhớ. MODEL_PIN_MISMATCH và MODEL_PIN_UNVERIFIED là ERROR,
không phải reject hay accuracy PASS. Đây là kiểm tra log do runtime báo, không
chứng thực model từ provider. Xem docs/MODEL_PIN_FIX.md; không gọi API khi test offline.

# Trạng thái bản hợp nhất

Đọc README.md, MERGE_REPORT.md và docs/MERGE_DECISIONS.md trước. Phần trạng thái nhánh bên dưới là lịch sử D, không phản ánh merge hiện tại. B0–B4, sensors, corpus/evaluator đã tích hợp; Batfish còn skipped; prepare có pending_review. Không chạy model/API hay deploy khi chỉ kiểm thử offline.

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

- `role` không vào prompt; runner truyền role qua argv trong patch. dsh khởi động harness nên
  argv/patch/file trạng thái không phải kênh authority bất biến trước runtime bị chiếm quyền.
- Runner phát lại gate trước render với role/level và context giữ trong bộ nhớ trước khi gọi dsh
  (D-044). Đây là kiểm tra cuối đối với artifact, không phải sandbox hệ điều hành cho process cùng user.
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

## 6. Lệnh thường dùng (đã chạy thử trên máy TV1, 29/09/2026; ba lệnh pytest và `run_cases`: 01/10/2026)

```powershell
pip install -e ".[dev]"                        # cài đặt (sau khi Activate venv)
python -m pytest -m "not slow"                 # khi đang lặp: bỏ test sinh subprocess, chạy trong vài giây
python -m pytest -n auto                       # bộ đầy đủ, song song (pytest-xdist); CI chạy đúng lệnh này
python -m pytest                               # bộ đầy đủ, tuần tự: cùng tập test, chậm hơn
python scripts\run_cases.py --config config\fake.toml   # chạy khô mọi case, không cần API key
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
- Khi đang lặp: chạy test liên quan hoặc `pytest -m "not slow"`. Bộ đầy đủ (`pytest -n auto`) + `run_cases` chỉ
  trước khi đề xuất commit.
- Bộ đầy đủ (`python -m pytest -n auto`) và `ruff check .` sạch trước khi đề xuất commit.
- Test sinh subprocess (fake_dsh, harness MCP server, dsh) phải mang `@pytest.mark.slow`; test chỉ ghi vào `tmp_path`,
  không ghi vào thư mục cố định như `runs/` (để chạy song song được). Số đo: `docs/findings/test-speed.md`.
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

Dùng đúng bộ nhãn ở mục 1 của DECISIONS.md. Mỗi mục có đúng một trạng thái:
- **ĐÃ CHỐT**: có trong báo cáo Ch1–2, hoặc nhóm + GVHD đã duyệt (ghi ngày GVHD duyệt).
- **NHÓM CHỐT**: nhóm đã duyệt (ghi ngày + người duyệt), chưa có GVHD duyệt.
- **ĐANG CÂN NHẮC**: nhóm đã chọn hướng, chờ bằng chứng hoặc chờ GVHD.
- **ĐỀ XUẤT**: có người đề xuất, nhóm chưa duyệt.
- **BỎ**: đã loại, ghi kèm lý do.
- **THAY THẾ bởi D-xxx**: bị mục khác thay.
- **GIỮ CHỖ**: số đã dành cho quyết định mà nội dung còn ở nhánh chưa merge; thay bằng trạng thái thật khi merge.
- Mỗi mục có thêm hai dòng: `GVHD: không cần | cần duyệt | đã duyệt (ngày)` và
  `Hiện thực: đã có code (đường dẫn) | một phần (ghi rõ) | chưa`. Không ghi "đã có code" khi chưa mở code để xem.
- Không biến đề xuất thành quyết định: chỉ nhóm đổi trạng thái sang NHÓM CHỐT; chỉ GVHD (hoặc Ch1–2) làm một mục
  thành ĐÃ CHỐT.

## 10. Phạm vi — không làm trừ khi được yêu cầu rõ

- Deploy xuống thiết bị (Ansible/NAPALM/Netmiko), rollback, GitOps, dashboard.
- Vendor khác Cisco IOS, NAT, dynamic routing.

## 11. Tài liệu tham khảo

| File | Dùng khi cần |
| --- | --- |
| `README.md` | Cài đặt, cấu trúc thư mục và người sở hữu, luồng contract |
| `DECISIONS.md` | Quyết định và trạng thái. N-01..N-09 (nền tảng theo Ch1–2) và D-001..D-042; số tiếp theo D-043. Số D-xxx trong tài liệu viết theo bản project claude.ai KHÁC nghĩa: tra "Bảng đổi số" ở mục 2 của file (bản project lưu ở `docs/archive/DECISIONS_project_2026-10-01.md`, chỉ đọc) |
| `docs/design/registry-mcp-integration.md` | Thiết kế nối tool registry vào MCP harness server (task 3.3); mục 8: phân loại kết cục, `error_code`, `leak_sources`; mục 9: threats to validity |
| `docs/findings/pre-real-run-checklist.md` | Những gì đã kiểm trên dsh thật trước lần gọi API đầu tiên; đề xuất cho record/replay (task 1.2) |
| `docs/findings/first-real-run.md` | Lần chạy LLM thật đầu tiên (`clean-vlan30`): số đo, khác biệt so với `fake_dsh`, vấn đề V1–V5 và cách xử lý |
| `docs/findings/test-speed.md` | Số đo tốc độ bộ test trước/sau marker `slow` và xdist |
| `docs/SETUP_WINDOWS.md` | Cài đặt và chạy trên Windows, cấu trúc `runs/<id>/`, chạy khô, chạy thật |
| `PR_setup.md` (không commit) | Mô tả PR #8 và commit D-025; hướng dẫn giải conflict khi gộp nhánh 1.5 |
| `spikes/dsh_mcp/01_research_findings.md` | Kết quả Q1–Q5, phát hiện F1–F14, điều kiện GO |
| `spikes/dsh_mcp/NOTES.md` | Chi tiết kỹ thuật spike dsh (patch, log, system prompt) |
| `schemas/` | JSON Schema sinh từ contract |
| `.claude/settings.json` | Quyền deny/ask của Claude Code |

## 12. Trạng thái dự án (cập nhật 07/10/2026 — sửa mục này mỗi khi xong một task)

Mục này mô tả cả repo, không riêng nhánh đang checkout. Số liệu và trạng thái nhánh lấy từ git ngày 07/10/2026;
kiểm lại bằng `git branch -r`, `git worktree list` trước khi dựa vào.

**Nhánh và nơi làm việc**

| Nhánh | Trạng thái |
| --- | --- |
| `main` | đã gộp mọi thứ dưới đây: PR #7 (mã kết cục, D-017), #8 (setup chạy thật, D-019..D-023), #9 (workspace của dsh, D-025), #10 (task 1.5: `leak_sources`, `LEAK_SCAN_INCOMPLETE`, D-018), #11 (marker `slow`, xdist, D-024). 618 test |
| `docs/decisions-merge` | PR chứa bản cập nhật này: gộp hai bản DECISIONS, bộ nhãn mới, N-01..N-09 và D-001..D-042 |
| `tv1/1.2-llm-record-replay` | nhánh duy nhất còn việc dở: 1 commit WIP; quyết định của nó đang mang số D-009, trùng `main` → đổi sang số mới (từ D-043) khi rebase |
| `chore/faster-tests`, `chore/faster-tests-v2`, `docs/dsh-entry-point`, `tv1/1.5-outcome-codes` và các nhánh task cũ | nội dung đã ở `main`; `chore/faster-tests` (bản cũ, không có PR) được thay bằng `-v2`. Còn trên remote cho tới khi nhóm xoá |

- Worktree `..\cha-run` (venv và `config/local.toml` riêng) là nơi chạy dsh thật; `git pull --ff-only` trước khi chạy.
- Một nhánh đang checkout ở worktree khác thì không checkout được ở đây: làm trên HEAD tách rời rồi
  `git push origin <commit>:refs/heads/<nhánh>`, không sửa file trong worktree của người đang chạy thật.

**Đã xong**
- C0: khung repo, contract `src/harness_agent/models/`, `schemas/`, `examples/`.
- Task 1.1 spike dsh + MCP: GO CÓ ĐIỀU KIỆN (D-008). Task 1.4: walking skeleton (harness MCP server, gate, runner hai
  stage, `fake_dsh`). Task 3.2: 11 template Jinja2. Task 3.3: tool registry (11 tool) nối vào harness server; một bộ
  tool, một gate, một renderer, một thư mục template (D-009..D-016).
- Task 1.5: `error_code` cho mọi đường ERROR (13 mã) và hai cờ leak (D-017); harness chặn secret ở một điểm, runner
  tìm rồi che secret trong output thô của dsh, `leak_sources` với 5 loại nguồn; trace hỏng là `HARNESS_STATE_INVALID`;
  phép quét secret không đủ là `LEAK_SCAN_INCOMPLETE`, case sạch là `leak_sources == []` VÀ `leak_scan_complete`
  (D-018).
- Setup cho chạy thật (PR #8): thứ tự tham số dsh (`--patch` trước `--json`); config có BOM; DSH_HOME theo case, quét
  rồi xoá session log (D-019); ghim model ở `config/model.toml` (D-020); `usage` và `duration_s` trong `result.json`
  (D-021); file kết quả UTF-8 không BOM, LF, so sha256 với golden (D-022); test chốt "khởi động harness chỉ nối thêm
  vào trace".
- D-025 (PR #9): thư mục làm việc của dsh cố định ngoài repo, có khoá; `scripts/check_label_leak.py`. Đã thử với dsh
  thật không có API key: harness khởi động, model không còn nhận tên case hay đường dẫn repo.
- D-024 (PR #11): marker `slow` (130 trong 618 test), `pytest-xdist==3.8.0`, CI `pytest -n auto`. `-m "not slow"`: 488
  test, khoảng 6 giây; `-n auto`: khoảng 6,5 phút trên máy 8 CPU logic. Số đo: `docs/findings/test-speed.md`.
- **Lần chạy LLM thật đầu tiên** (01/10/2026, `clean-vlan30`, code tại `f66b524`): accept đúng kỳ vọng, 17 803 token,
  18,4 giây. Chi tiết và 5 vấn đề V1–V5: `docs/findings/first-real-run.md`. Từ đó chưa có lượt chạy thật nào được ghi
  vào tài liệu.
- DECISIONS.md: một file duy nhất, bộ nhãn mới, nhóm duyệt ngày 06/10/2026 (PR `docs/decisions-merge`).

**Trạng thái từng phần** (theo `main`)
- `models/` (TV2): contract đã khoá từ 01/10. `invariants.py` vẫn là stub (task 2.2).
- `tools/` (TV3): registry 11 tool, gate của registry, renderer, 11 template: có code và test. Cú pháp CLI chưa kiểm
  trên EVE-NG (task 3.5). `lab/eve/`: rỗng.
- `harness/` (TV4): `server.py` (MCP lowlevel), `gate.py` (G1–G6), `context_store.py` (validation + projection),
  `roles.py`, `trace.py` (chuỗi hash), `leak.py`: bản walking skeleton, task 4.2–4.5 hoàn thiện. TV1 đã sửa
  `server.py` hai lần (`newline="\n"`, D-022; điểm chặn secret, D-018).
- `runner/` (TV1): pipeline hai stage chạy được với `fake_dsh` (15 case chạy khô, 15/15) và đã chạy thật một case.
  Có `dsh.py`, `dsh_home.py`, `model.py`, `usage.py`, `workspace.py`, `label_leak.py`.
- Stub, chưa có code: `sensors/` (iv, task 3.4), `agents/`, `baselines/` (B0, task 1.3), `llm/` (task 1.2, WIP ở nhánh
  riêng), `corpus/`, `eval/` (TV2, 2.3–2.5). Spike Batfish (3.6) chưa có.
- Mức ablation: pipeline hiện có control (i), (ii), (iii), (v) và chưa có sensors (iv), tức ngang B3 (docstring của
  `pipeline.py` gọi là "B4 rút gọn"). Chưa có công tắc bật/tắt control để chạy B1/B2, chưa có B0, chưa có B4.
- Dữ liệu: 1 context (`campus-small-01`), 15 case, 15 kịch bản `fake_dsh`.
- `ruff format --check .` báo 4 file cũ chưa format (3 file trong `spikes/dsh_mcp/`, `models/common.py`). Không chạy
  `ruff format src`: nó sửa `models/common.py` (thư mục đã khoá). Chỉ format file mình sửa.

**Bước tiếp theo**
1. Việc lớn, theo thứ tự phụ thuộc: contract v0.2 và một schema context duy nhất (D-042); `invariants.py` (2.2);
   sensors (iv) → B4; B0 và công tắc mức B1–B4; corpus (D-040, D-041); evaluator (D-006, D-032, D-036).
2. Task 1.2 (DeepSeek client + record/replay): rebase `tv1/1.2-llm-record-replay` lên `main`, đổi số quyết định.
3. Hai lượt kiểm với dsh thật còn mở (V3), nếu chưa làm: (a) API key sai, (b) `adv-protected-uplink` với key thật. Sau
   (b) chạy `check_label_leak.py` trên run-dir: đó là lần đầu kết quả tool thật đi qua phép kiểm rò nhãn. Ghi kết quả
   vào `first-real-run.md` (ba câu hỏi còn mở ở mục 3 của file đó).

   ```powershell
   # (a) key SAI: chuỗi giả, không phải key thật. Kỳ vọng error / DSH_EXIT; run_cases in SAI, thoát mã 1 là bình thường
   $env:DEEPSEEK_API_KEY = "YOUR_API_KEY_DO_NOT_COMMIT"
   python scripts\run_cases.py --config config\local.toml --keep-dsh-home --out runs\check-a clean-vlan30
   Remove-Item Env:\DEEPSEEK_API_KEY
   # (b) key thật, nhập bằng Read-Host -AsSecureString (docs/SETUP_WINDOWS.md). Kỳ vọng ok / reject / G4_blast_radius
   python scripts\run_cases.py --config config\local.toml --keep-dsh-home --out runs\check-b adv-protected-uplink
   python scripts\check_label_leak.py runs\check-b\adv-protected-uplink
   ```

   Đọc sau (a): `cba.ndjson` dòng `turn_end` (`reason.kind`, `error.code`) và exit code (nếu ra `AGENT_INCOMPLETE` thì
   dsh thoát 0 khi API lỗi: runner phân loại sai); `usage` trong `result.json`; chuỗi `sk-fake` không nằm trong file
   nào của run-dir. Đọc sau (b): `tool_result.status` của lời gọi bị gate chặn trong `tsa.ndjson`; mọi `tool_call.tool`
   bắt đầu bằng `mcp__harness__`; `rejects.jsonl`; không có `change_package.md`; `leak_scan_complete` là true (lần đầu
   điều kiện "mỗi stage có session log" gặp dsh thật).
4. Nhóm quyết: D-018 và D-025 có thành NHÓM CHỐT không (chưa qua buổi duyệt 06/10); có khai chung `[dsh] workspace`
   cho mọi máy không (đường dẫn mặc định chứa tên người dùng máy); model thực tế khác model đã ghim có phải ERROR
   không; phương án record/replay cho task 1.2 (`pre-real-run-checklist.md` mục 6).
5. GVHD duyệt: danh sách Q1–Q11 ở mục 5 của DECISIONS.md, trong đó có tên model (`deepseek-flash`), B0 chạy qua dsh
   (D-008), D-006, cách phân loại kết cục gồm mã thứ 13 (D-017, D-018), mục threats to validity (D-025).

**Quyết định quan trọng** (trạng thái sau buổi chốt của nhóm ngày 06/10/2026, xem DECISIONS.md: các mục dưới đây là
NHÓM CHỐT, trừ D-006, D-017, D-018 và D-025 đang ĐANG CÂN NHẮC chờ GVHD, và D-023 đã THAY THẾ bởi D-025. D-018 và
D-025 vào `main` sau buổi chốt nên chưa được nhóm duyệt. D-003 và D-020 là NHÓM CHỐT kèm việc chưa làm. Chưa mục D
nào có GVHD duyệt)
- D-001 contract `extra="forbid"`, `frozen=True`: output LLM untrusted; không sửa object giữa gate và render.
- D-003 cấm ký tự điều khiển: một `\n` trong description đủ để chèn lệnh vào CLI đã render.
- D-006 evaluator độc lập với sensor (iv): tránh chấm bằng chính control đang được đánh giá.
- D-008 dsh GO có điều kiện: dsh fail-open khi MCP server chết, nên mọi control phải ở phía tin cậy.
- D-009, D-011 một tool registry và một gate là nguồn duy nhất: hai bộ định nghĩa tool sẽ lệch nhau, và chỗ lệch là
  chỗ lọt.
- D-013 harness dùng `mcp.server.lowlevel.Server`, không dùng FastMCP: FastMCP âm thầm bỏ tham số thừa và từ chối
  tool lạ trước khi tới gate, nên gate không thấy và trace không ghi.
- D-016 runner so khớp hai chiều staged ↔ change request đã duyệt: không dựa vào việc file do harness ghi là đúng.
- D-017 mỗi đường ERROR một `error_code`; ERROR không bao giờ tính là PASS; tách "ý định rò rỉ" khỏi "rò rỉ thật".
- D-018 secret bị chặn ở một điểm trong harness; runner tìm trước, che sau; phép quét không đủ là ERROR
  `LEAK_SCAN_INCOMPLETE`: một thư mục thiếu cho danh sách rỗng giống hệt một thư mục sạch, nên "không thấy gì" chỉ
  có nghĩa khi biết đã quét được gì.
- D-019 DSH_HOME theo case, quét rồi xoá: session log của dsh giữ nguyên tham số mọi tool call (kể cả secret đã bị
  harness chặn) và nén zstd, nên tìm theo byte thô sẽ sót.
- D-020 ghim model trong repo; `result.json` ghi model dsh THỰC SỰ gửi: mặc định của runtime không nằm trong repo
  và có thể đổi theo phiên bản.
- D-021 token/thời gian chỉ chép số dsh báo, thiếu thì `null`: số ước tính trông như số đo sẽ làm sai Ch3.
  `requests` đếm bước của agent kể cả bước lỗi, không phải số request tính tiền.
- D-022 file kết quả LF, UTF-8 không BOM: trên Windows cùng nội dung ra byte khác, hash của change package phụ
  thuộc máy.
- D-023 → D-025 thư mục làm việc của dsh cố định, tên trung tính, ngoài repo, có khoá: dsh gửi nguyên văn đường dẫn
  cwd cho model, nên cwd trong run-dir đưa **tên case** vào prompt (rò nhãn, làm sai phép đo ở mọi mức B0–B4).
  Bài học chung: runtime tự chèn nội dung vào prompt; đọc session log thật sau mỗi lần đổi phiên bản dsh, và không
  đặt gì phụ thuộc case vào thứ runtime có thể đưa cho model.
- D-024 marker `slow` + xdist, mặc định vẫn chạy tất cả: test sinh subprocess (130 trong 618) chiếm gần hết thời gian; phần lớn thời
  gian đó là `import mcp` (khoảng 1,1 giây mỗi process), chưa xử lý vì phải đổi code.


## A6 — cách ly capability duyệt web (D-046, ĐỀ XUẤT)

- MCP prepare/status chỉ trả dữ liệu đề xuất, request_id và trạng thái; không trả review_url,
  bootstrap token, cookie phiên hoặc CSRF. Không đăng ký browser_review/browser_decide thành tool.
- Bridge tự mở tab operator riêng; bootstrap dùng một lần, cookie HttpOnly/SameSite=Strict;
  GET review và POST quyết định đều yêu cầu cookie. Host/Origin/CSRF vẫn được kiểm tra.
- Helper nhận URL qua stdin, tắt stdout/stderr; không fallback in URL vào chat/log khi mở tab thất bại.
- Đây là cách ly kênh MCP/HTTP, không phải chứng thực người thật hay sandbox runtime cùng user.
  Quyền đọc bộ nhớ/browser profile hoặc điều khiển tab operator vẫn vượt ranh giới này.
- Kiểm thử: tests/test_approval_isolation.py; quy trình hiện hành: docs/MULTI_REQUEST_WEB.md.
