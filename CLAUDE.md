# CLAUDE.md — quy tắc cho Claude Code trong repo này

## 1. Dự án

- Đồ án IAP491: LLM agent sinh cấu hình Cisco IOS từ intent ngôn ngữ tự nhiên.
- Agent = Model + Harness. LLM là thành phần KHÔNG tin cậy; tính đúng và an toàn do control
  deterministic của nhóm bảo đảm, đặt ngoài cả mô hình lẫn agent runtime.
- Không deploy: đầu ra là change package để con người duyệt (ngoại lệ duy nhất, thủ công: §10, D-064).

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
      ├──────────── role (không qua prompt) ─────────────────► ▼
      │                                          staged_ops.jsonl + trace (HMAC)
      └── kiểm lại: role/level của server_start, chữ ký trace, phát lại gate ──► Jinja2 render ─► sensors (iv)
                                                                                  ─► change package
```

- `role` không bao giờ vào prompt; role vào harness qua argv trong patch do runner sinh **và** được runner kiểm lại
  trước render (D-060): model không đọc hay sửa được argv, nhưng dsh (process khởi động harness) thì có thể. Vì vậy
  runner đòi mọi `server_start` mang đúng role/level, trace ký đúng khoá của lượt chạy (`CHA_TRACE_KEY`), và phát
  lại gate trên từng thao tác đã staged với role/level/context của chính runner. Giới hạn same-user: D-060.
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
  không thêm `!!js` mới (F7). Ngoại lệ duy nhất có ghi chép: biểu thức hằng `__jsExpr` đọc `DEEPSEEK_API_KEY` trong
  `web.patch.yml` của `scripts/prepare_web.py` (D-066, có test quét).
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
ruff format --check .                          # phải sạch ở máy dev; CI chỉ kiểm src tests scripts (ruff 0.16.9)
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

- Deploy xuống thiết bị (Ansible/NAPALM/Netmiko), rollback, GitOps, dashboard. Ngoại lệ duy nhất: apply chỉ qua
  `harness_agent.apply` (`cha-apply`, D-064), không bao giờ tự động: mặc định dry-run, commit cần `--commit
  --confirm <run_id>` và bản duyệt trong trace; không gọi từ runner/evaluator/UI, không chạy trong lô đo.
- Vendor khác Cisco IOS, NAT, dynamic routing.

## 11. Tài liệu tham khảo

| File | Dùng khi cần |
| --- | --- |
| `README.md` | Cài đặt, cấu trúc thư mục và người sở hữu, luồng contract |
| `DECISIONS.md` | Quyết định và trạng thái. N-01..N-09 (nền tảng theo Ch1–2) và D-001..D-066 (D-046 đã dành, chưa viết); số tiếp theo D-067. Số D-xxx trong tài liệu viết theo bản project claude.ai KHÁC nghĩa: tra "Bảng đổi số" ở mục 2 của file (bản project lưu ở `docs/archive/DECISIONS_project_2026-10-01.md`, chỉ đọc) |
| `docs/design/registry-mcp-integration.md` | Thiết kế nối tool registry vào MCP harness server (task 3.3); mục 8: phân loại kết cục, `error_code`, `leak_sources`; mục 9: threats to validity |
| `docs/findings/pre-real-run-checklist.md` | Những gì đã kiểm trên dsh thật trước lần gọi API đầu tiên; đề xuất cho record/replay (task 1.2) |
| `docs/findings/first-real-run.md` | Lần chạy LLM thật đầu tiên (`clean-vlan30`): số đo, khác biệt so với `fake_dsh`, vấn đề V1–V5 và cách xử lý |
| `docs/findings/test-speed.md` | Số đo tốc độ bộ test trước/sau marker `slow` và xdist |
| `docs/SETUP_WINDOWS.md` | Cài đặt và chạy trên Windows, cấu trúc `runs/<id>/`, chạy khô, chạy thật, corpus → run → evaluate, benchmark, `cha-check`/`cha-apply` |
| `docs/design/ops-ui-mvp.md`, `docs/design/apply-controlled.md` | Thiết kế UI vận hành (D-057) và apply có kiểm soát (D-064) |
| `docs/MULTI_REQUEST_WEB.md`, `docs/SHARED_WEB_LAUNCHER.md` | MCP bridge cho khách của dsh |
| `docs/findings/evidence-log.md` | Nhật ký bằng chứng các lượt chạy và phát hiện |
| `docs/superpowers/plans/2026-10-16-complete-project.md` | Kế hoạch hoàn thiện 16/10 (Task 1–11) |
| `spikes/dsh_mcp/01_research_findings.md` | Kết quả Q1–Q5, phát hiện F1–F14, điều kiện GO |
| `spikes/dsh_mcp/NOTES.md` | Chi tiết kỹ thuật spike dsh (patch, log, system prompt) |
| `schemas/` | JSON Schema sinh từ contract |
| `.claude/settings.json` | Quyền deny/ask của Claude Code |

## 12. Trạng thái dự án (cập nhật 17/10/2026 — sửa mục này mỗi khi xong một task)

Số liệu lấy từ checkout ngày 17/10/2026. Checkout này chỉ có `master` và nhánh đang làm; trạng thái nhánh của repo
nhóm kiểm bằng `git branch -r`, `git worktree list` trước khi dựa vào.

**Nhánh và nơi làm việc**

| Nhánh | Trạng thái |
| --- | --- |
| `master` | bản upload 15/10/2026 + sửa UI 16/10 (U1–U4), commit `aa0ea49`: nền của mọi việc dưới đây |
| `feat/complete-2026-10-16` | kế hoạch hoàn thiện 16/10 (`docs/superpowers/plans/2026-10-16-complete-project.md`), Task 1–11 và đợt sửa sau review cuối đã commit, chưa merge |

- Worktree `..\cha-run` (venv và `config/local.toml` riêng) là nơi chạy dsh thật; `git pull --ff-only` trước khi chạy.
- Một nhánh đang checkout ở worktree khác thì không checkout được ở đây: làm trên HEAD tách rời rồi
  `git push origin <commit>:refs/heads/<nhánh>`, không sửa file trong worktree của người đang chạy thật.

**Đã có trên nền `master`** (chi tiết và trạng thái từng mục: DECISIONS.md)
- Contract `models/` (khoá từ 01/10) và `invariants.py` (16 invariant, D-043); registry 11 tool, 11 template, một gate,
  một renderer (D-009..D-016); harness MCP server, gate G1–G6, projection, trace chuỗi hash, chặn secret (D-017, D-018).
- Runner hai stage qua dsh, B0 (D-044), mức B0–B4 ở một nơi (`levels.py`, D-049), freshness binding (D-054);
  sensors (iv): syntax và CIS, Batfish khai `skipped` (`sensors/batfish.py`, task 3.6 chưa làm); LLM client + cache
  (`llm/`, D-049); corpus generator và `data/corpus/v2` (82 case, D-056); evaluator `eval/` (D-006); collector SSH
  chỉ đọc (D-047, GVHD đã duyệt 13/10); UI vận hành `ui/` (D-057).
- Dữ liệu: context `campus-small-01` (+ 2 bản injection), `smb-01` (snapshot SSH, `-prov`, dẫn xuất `-l2`); 14 case
  tay + 2 fixture lỗi; kịch bản `fake_dsh` cho B0, B1, B2, B3/B4.
- Lượt LLM thật có ghi chép: 01/10/2026 `clean-vlan30` (`docs/findings/first-real-run.md`) và 12/10/2026 B3 vs B0
  trên `clean-vlan30`, `adv-protected-uplink` (`docs/findings/evidence-log.md` mục 9). Chưa có lượt thật nào sau
  các thay đổi của nhánh dưới đây.

**Nhánh `feat/complete-2026-10-16`** (D-058..D-066, đều ĐỀ XUẤT; số tiếp theo D-067)
- Task 1: port MCP bridge nhiều yêu cầu (`web/`, `scripts/web_multi_server.py`, `scripts/start_web.ps1`), dữ liệu
  confirmed-intent và injection (`docs/MULTI_REQUEST_WEB.md`, `docs/SHARED_WEB_LAUNCHER.md`).
- Task 2: luật an toàn chung cho B1–B4 (`prompts/_safety_rules.md`), sha256 prompt trong trace (D-058).
- Task 3: model thực tế khác pin là ERROR `MODEL_MISMATCH`; kiểm `TERMINATED` sau TSA (D-059).
- Task 4: runner phát lại gate trước render, role/level trong `server_start`, trace ký HMAC trường `sig` bằng
  `CHA_TRACE_KEY`; `verify_chain` → ok / broken / unsigned / bad_signature (D-060).
- Task 5: intent grounding tất định ở runner (`harness/grounding.py`), cưỡng chế B3–B4, shadow B1–B2 (D-061).
- Task 6: contract nhãn `ConfigType.OUT_OF_SCOPE`, `AttackLabel.predicate` (D-062); gom lặp theo case, Wilson CI,
  mẫu số ASR, cột lenient (D-063).
- Task 6b: benchmark `data/benchmark/v1` chấm độ chính xác đầu ra, gold hai mức, split dev/test (D-065).
- Task 7: `cha-check` (kiểm một file cấu hình bất kỳ, không LLM); entry point `cha-ui`.
- Task 8: `cha-apply` áp cấu hình đã duyệt xuống một thiết bị, mặc định dry-run, ngoài lô đo (D-064).
- Task 9: UI hiện bước grounding, trạng thái chuỗi/chữ ký trace, lệnh `cha-apply` dry-run sau khi duyệt.
- Task 10: xoá stub `agents/`, `baselines/`, `tools/registry/audit.py`; CI thêm `ruff format --check`, chạy khô B0
  và B4, job `windows-latest` (`pytest -m "not slow"`); tài liệu này, DECISIONS, `docs/SETUP_WINDOWS.md`.
- Task 11: `docs/HUONG_DAN_SU_DUNG.md` + bản docx, CHANGELOG (`76cc2ad`).
- Sửa sau review cuối (17/10/2026, `.superpowers/sdd/2026-10-16-complete-project/final-fix-report.md`): apply đọc
  `show vlan brief` và không xoá VLAN vốn có trên switch VTP server/client, cảnh báo `aaa new-model` không có
  username (D-064); case `known_gap: D-051` dừng ở `intent_grounding` là `xpass` (D-061, D-063 mục 4); threat to
  validity của benchmark (D-065); `__jsExpr` của `web.patch.yml` (D-066); `scripts/evaluate.py` không import
  generator (gate, grounding) nữa (`eval/benchmark_io.py`, test quét import, D-006); JSON của `cha-check` escape
  DEL/C1; `index.json` LF; CI ghim ruff 0.16.9.

**Số liệu** (17/10/2026, máy Linux của phiên hiện thực)
- 1931 test, trong đó 362 `slow` (`python -m pytest --collect-only -o addopts="" -q`; addopts có sẵn `-q` nên
  `--collect-only -q` trần không in dòng tổng).
- Chạy khô `run_cases.py --config config/fake.toml` cả năm mức B0–B4: 16/16 case đúng kỳ vọng fixture ở mỗi mức.
- `ruff check .` và `ruff format --check .` (cả repo) sạch, kiểm bằng ruff 0.15.11; CI cài tường minh ruff 0.16.9
  (bản `[dev]` ghim) và chỉ chạy `ruff format --check src tests scripts`. Chưa chạy CI thật lần nào (checkout không có
  remote), nên chưa kiểm format với bản ghim.

**Bước tiếp theo**
1. Đẩy nhánh, cho CI chạy lần đầu (ruff 0.16.9, job Windows), rồi merge qua PR.
2. Nhóm duyệt các mục ĐỀ XUẤT trong D-043..D-066 (D-047 đã chốt); GVHD: định nghĩa B3/B4 đổi vì grounding
   (D-061), apply trước khi demo trên lab (D-064), Q1–Q11 ở mục 5 của DECISIONS.md.
3. Lượt với dsh thật còn nợ: `CHA_TRACE_KEY` qua khối env của MCP client (D-060), provider và
   `reasoning_effort` (D-059), `NetmikoTransport` trên lab smb-01 (D-064); câu hỏi còn mở ở
   `docs/findings/first-real-run.md` mục 3 (đối chiếu `evidence-log.md` mục 8–9 trước khi chạy lại).
4. Lô đo Ch4 với model thật: `run_corpus.py --level B0 B1 B2 B3 B4 --repeat 3` rồi `evaluate.py`
   (`docs/SETUP_WINDOWS.md` mục 11–12). Trước đó nối McNemar/bootstrap/Holm vào `scripts/evaluate.py` (D-063).
5. Việc nhỏ đã hoãn (`.superpowers/sdd/2026-10-16-complete-project/progress.md`): Batfish (3.6); G4 không kiểm
   platform (D-055); `snapshot_ok` của apply có thể khớp `hostname` trong banner; `__jsExpr` của `web.patch.yml`
   chưa kiểm bằng `dsh --dump-config` (D-066).

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
- Trên nhánh `feat/complete-2026-10-16` (ĐỀ XUẤT, chưa nhóm duyệt): D-060 runner là control cuối (phát lại gate
  trước render, trace HMAC): dsh khởi động harness nên có thể đổi argv; D-061 grounding tất định: gate kiểm tool
  call với change request, không kiểm change request với intent gốc; D-064 apply chỉ qua `cha-apply`, mặc định
  dry-run, cần bản duyệt trong trace, không bao giờ trong lô đo.


### Bổ sung 20/10/2026 — D-067 (ĐỀ XUẤT)

Có bản sửa collector/sensor/SSH trên nhánh `tv1/collector-sensor-hardening`; chưa push/merge.
Command contract mới bắt 27/27 đột biến cố định; syntax/CIS riêng vẫn 22/27.
Batfish backend và lab thật chưa xác minh; context security là quan sát có guard, chưa mô hình
forwarding đầy đủ. Chi tiết: `docs/HARDENING_2026-10-20.md`.
