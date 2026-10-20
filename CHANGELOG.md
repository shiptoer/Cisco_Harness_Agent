# CHANGELOG

Ghi các thay đổi lớn theo đợt. Chi tiết và trạng thái từng quyết định: `DECISIONS.md`. Hướng dẫn sử dụng:
`docs/HUONG_DAN_SU_DUNG.md` (bản Word: `docs/HUONG_DAN_SU_DUNG.docx`).

## 2026-10-20 — hardening (đề xuất D-067)

Sửa collector VLAN/trunk; bổ sung quan sát security, guard cho context thiếu ngữ nghĩa;
command contract sau render, kiểm lại trước apply; SSH strict host key và legacy opt-in;
discovery giới hạn và xác minh họ OS qua show version; missing required Batfish fail-closed.
Chưa có Batfish backend hoặc kiểm nghiệm lab thật. Xem `docs/HARDENING_2026-10-20.md`.

## 2026-10-17 — bản hoàn thiện (nhánh feat/complete-2026-10-16)

Nền: `master` tại `aa0ea49` (bản upload 15/10/2026 + sửa UI 16/10, U1–U4: nhận `localhost`, traceback lỗi 500 ra
terminal, cảnh báo thiếu key ở chế độ thật, thông báo cổng bận). Kế hoạch: `docs/superpowers/plans/2026-10-16-complete-project.md`
(`89e449e`). Mọi quyết định mới (D-058..D-066) đều **ĐỀ XUẤT**: nhóm chưa duyệt, GVHD chưa duyệt. Chưa có lượt LLM thật
nào sau các thay đổi dưới đây; mọi số liệu là chạy khô bằng `config/fake.toml`.

| Task | Thay đổi | Commit | Quyết định |
| --- | --- | --- | --- |
| 1 | Port MCP bridge nhiều yêu cầu từ shiptoer: `src/harness_agent/web/`, `scripts/web_multi_server.py`, `scripts/prepare_web.py`, `scripts/start_web.ps1`, chế độ một contract `scripts/web_pipeline_server.py`, dữ liệu `data/confirmed_intents/`, `data/web-injection-if-001/`. Link duyệt có token không còn trả cho model. Sửa review: preflight hỏng trước dsh giữ approval, thư mục kết quả theo `CHA_WEB_OUT`, preflight ở B4 | `1434162`, `56f4e06` | — |
| 2 | Luật an toàn chung cho mọi mức hai giai đoạn ở một file `prompts/_safety_rules.md`; `case_start.prompt_sha256` ghi hash prompt từng stage | `7ea6203` | D-058 |
| 3 | Model thực tế khác pin (hoặc không đọc được) là ERROR `MODEL_MISMATCH` (nhóm infra); kiểm file `TERMINATED` sau stage TSA (lời gọi chứa secret sau finalize thành `SECRET_IN_ARGS`) | `d664c99` | D-059 |
| 4 | Runner là control cuối: `server_start` ghi role/level và runner đòi khớp; runner phát lại gate trên từng thao tác đã staged trước render; trace ký HMAC trường `sig` bằng khoá ngẫu nhiên mỗi lượt (`CHA_TRACE_KEY`, qua env và khối env của MCP client, che trong patch sau stage); `verify_chain` trả `ok`/`unsigned`/`bad_signature`/`broken`; giới hạn same-user ghi rõ | `33fd7be`, `a6fb915`, `1a9dd25` | D-060 |
| 5 | Intent grounding tất định ở runner (`src/harness_agent/harness/grounding.py`): thao tác có giá trị không neo được vào intent → `clarify`, `stopped_by: intent_grounding`; cưỡng chế B3–B4, shadow B1–B2 | `aad6e29` | D-061 (hiện thực D-051, D-030) |
| 6 | Contract nhãn: `ConfigType.OUT_OF_SCOPE`, `AttackLabel.predicate` (tập đóng); evaluator gom lượt lặp theo case (majority, hoà lấy kết cục tệ hơn; `--aggregate pass_k`), Wilson 95% cho mọi tỉ lệ, mẫu số ASR theo case A1/A2 đo được, cột lenient, `intent_grounding` → `blocked`; `run_dir` tương đối trong `index.json`; sửa D-050 có ngày | `fc1d61f` (kế hoạch 6b), `4435cf1`, `93573df` | D-062, D-063, sửa D-050 |
| 6b | Benchmark `data/benchmark/v1` (108 case, gold tool call + CLI, split dev/test 78/30 theo template); `scripts/gen_benchmark.py`, `evaluate.py --benchmark` (ops strict/lenient, CLI exact/lenient); inventory smb-01 khai `iosvl2` cho năm switch IOL L2; context dẫn xuất `smb-01-collected-ref-l2.json` (`--derive`); cột CLI đòi không rò secret, thiết bị thừa là sai | `a33af21`, `55a039e`, `1377a10` | D-065, D-055 |
| 7 | `cha-check`: kiểm một file cấu hình bất kỳ với context, không LLM (phân tích ngược, phát lại gate, sensors, quét secret); che secret trong thông báo lỗi, escape ký tự điều khiển; entry point `cha-ui`, UI thành module `python -m harness_agent.ui` | `982b90b`, `492d76e` | — |
| 8 | `cha-apply`: áp cấu hình đã duyệt xuống một thiết bị qua SSH, mặc định dry-run, commit cần `--commit --confirm <run_id>`, backup/verify/rollback best-effort, không `write memory`, ngoài lô đo; allowlist dạng lệnh đúng hình dạng 11 template, chỉ B3/B4, chặn ký tự điều khiển/ngoài ASCII, kiểm snapshot trước khi áp | `13d433e`, `741946a`, `693cbb1` | D-064 |
| 9 | UI hiện bước intent grounding trong dải pipeline, trạng thái chuỗi/chữ ký trace, lệnh `cha-apply` dry-run sau khi duyệt | `ee401cb` | D-057 (bổ sung) |
| 10 | Xoá stub `agents/`, `baselines/`, `tools/registry/audit.py`; CI thêm `ruff format --check src tests scripts`, chạy khô B0 và B4, job `windows-latest` (`pytest -m "not slow"`); cập nhật CLAUDE.md §12, DECISIONS, `docs/SETUP_WINDOWS.md` mục 11–16 | `cf17c76` | — |
| 11 | `docs/HUONG_DAN_SU_DUNG.md` (13 mục, mọi lệnh chạy lại 17/10/2026 bằng `config/fake.toml`) và bản docx; `docs/build/md2docx.js` (chép từ bản 01/10); CHANGELOG này; README trỏ tới hướng dẫn | `76cc2ad` | — |
| sửa | Sau review cuối nhánh (`.superpowers/sdd/2026-10-16-complete-project/final-findings.md`, 13 mục; báo cáo `final-fix-report.md` cùng thư mục): `cha-apply` đọc `show vlan brief` khi cấu hình có `vlan N`, kiểm VLAN ngoài running-config bằng bảng, rollback chỉ `no vlan N` khi bảng trước khi áp xác nhận chưa có (không xoá VLAN vốn có trên switch VTP server/client), cảnh báo `aaa new-model` không có username; case `known_gap: D-051` dừng ở `intent_grounding` là `xpass`; threat to validity của benchmark (gold chọn để qua gate/grounding nên không đo false reject); `__jsExpr` duy nhất của `web.patch.yml` được ghi chép và có test quét; `scripts/evaluate.py` nạp benchmark qua `eval/benchmark_io.py` (không import gate/grounding), test quét import; UI không báo "đã qua" khi grounding có vi phạm; JSON của `cha-check` escape DEL/C1/U+2028/2029; `index.json` của `run_corpus` và cache của `fake_dsh` ghi LF; CI cài tường minh ruff 0.16.9; tài liệu khớp | `916af04` (apply) + commit sửa còn lại | D-064 (sửa), D-061, D-063 mục 4 (sửa), D-065 (bổ sung), D-066 |

Số liệu 17/10/2026 (Linux, chạy khô): 1910 test (361 `slow`); `run_cases.py --config config/fake.toml` 16/16 đúng kỳ
vọng fixture ở cả năm mức B0–B4 (chạy lại trong Task 11); benchmark B4 108/108 `cli_exact` (oracle, số của Task 6b ở
D-065; Task 11 chạy lại 10 case đầu: 10/10); `ruff check .` sạch (ruff 0.15.11). Sau đợt sửa sau review cuối: 1931 test (362 `slow`), `run_cases.py --level B4` 16/16, `ruff check .` và
`ruff format --check .` sạch (ruff 0.15.11).

### Việc còn lại

Cần dsh thật, API key hoặc thiết bị (không làm được trong môi trường hiện thực):

- **Lượt chạy dsh thật đầu tiên sau các thay đổi**: kiểm `CHA_TRACE_KEY` tới được harness qua khối env của MCP client
  (nếu không, mọi lượt ra `HARNESS_STATE_INVALID`, D-060); kiểm `provider` và `reasoning_effort` (kể cả `off`) trong
  session log để không ra `MODEL_MISMATCH` (D-059); kiểm dsh có lưu cấu hình MCP (kèm khoá) trong DSH_HOME không;
  chạy lại B1 thật vì prompt đã đổi (D-058).
- **Lô đo Ch4 với model thật**: corpus v2 và benchmark v1 ở B0–B4, `--repeat 3`. Chưa có số liệu model thật nào: mọi
  bảng trong `reports/` và trong tài liệu là oracle.
- **Windows CI**: job `windows-latest` (thêm ở Task 10) chưa chạy lần nào: checkout bàn giao không có remote, nhánh
  chưa push; các khối PowerShell trong hướng dẫn chưa chạy lại ngày 17/10.
- **Netmiko trên lab smb-01**: `NetmikoTransport` của `cha-apply` chưa chạy với Netmiko thật; rollback (`default
  interface`, số thứ tự ACL) chưa kiểm trên IOL L2 và router; collector `--ssh` chưa chạy lại sau khi sửa inventory
  (`iosvl2`).
- **ruff 0.16.9 / CI**: `[dev]` ghim 0.16.9 và CI cài tường minh bản đó trước `ruff format --check src tests scripts`,
  nhưng máy hiện thực chỉ có 0.15.11 và **chưa chạy CI thật lần nào** (checkout không có remote): lần đẩy nhánh đầu
  tiên là lần kiểm đầu tiên với bản ghim.
- **`__jsExpr` trong `web.patch.yml`** (D-066): chưa kiểm bằng `dsh --dump-config` hay lượt web thật rằng loader của
  dsh đánh giá dạng mapping này.

Việc code còn mở:

- **Nối McNemar / bootstrap / Holm vào `scripts/evaluate.py`** (đã có trong `eval/stats.py`, có test): cần chốt các cặp
  mức so sánh (B0↔B4, B2↔B3, B3↔B4) trước khi chạy thật (D-063).
- **G4 không kiểm platform** (D-055): lệnh L2 trên router chỉ bị sensor syntax chặn ở B4; ở B3 lọt qua gate.
- Batfish (task 3.6) vẫn `skipped`. (Test quét import giữ `scripts/evaluate.py` độc lập với `harness/`, `sensors/`,
  `corpus/`, `runner/`: đã thêm trong đợt sửa sau review cuối, D-006.)

Việc nhỏ đã hoãn qua review (`.superpowers/sdd/2026-10-16-complete-project/progress.md`, các dòng "minor (deferred)"),
tóm tắt:

- Bridge (Task 1): một số thông báo còn bằng tiếng Anh; test launcher import fixture chéo module; kiểm "trả package"
  lặp ở `web_pipeline_server.py` và `reviews.py`; `_keep_approval` raise trong `except` không `from None`.
- Prompt và model pin (Task 2–3): (đã sửa: `registry-mcp-integration.md` ghi cả `MODEL_MISMATCH` đứng cuối); đường
  prepare kiểm model trước khi kiểm credential (cần ghi ngoại lệ vào D-059);
  `cba_b1.md` còn "at this level"; chưa có test `MODEL_MISMATCH` ở B0.
- Trace (Task 4): cắt bớt đuôi đã ký, hoặc giả mạo đuôi `reviewer`/`apply`, vẫn cho `ok` (đã ghi là giới hạn ở D-060);
  `HARNESS_STATE_INVALID` vẫn ở nhóm `infra` dù có thể do bị sửa (câu hỏi Q8).
- Grounding (Task 5): D-061 chưa ghi lý do nới luật neo (luật chặt gây 17 báo nhầm trên case clean); test corpus B4
  nên ghim `stopped_by` theo case; hàm `_short` thừa; vị trí dự phòng `-1` hiện thành "thao tác 0".
- Evaluator (Task 6): `policy_violation` đang là actor N (ngoài mẫu số ASR); TV2 cần duyệt.
- Benchmark (Task 6b): picker hết pool nay raise thay vì bỏ template (chặt hơn, có chủ đích).
- `cha-check` (Task 7): (đã sửa: JSON escape DEL/C1); Batfish `skipped` vẫn cho mã thoát 0; secret
  ngắn hơn 8 ký tự không được quét (giới hạn của `ContextStore`).
- `cha-apply` (Task 8): kiểm snapshot có thể khớp một dòng `hostname X` không thụt nằm trong banner; dòng gửi đi có thể
  dài hơn dòng đã chuẩn hoá khi kiểm (khoảng trắng); cho phép AAA method trùng lặp.
- UI và CI (Task 9–10): thiếu test cho thẻ "trace đã ký"; `shell_arg` để backslash trần (sai trên Git Bash); test 404
  của `/apply` yếu. (Đã sửa: CLAUDE.md §12 nói rõ CI chỉ kiểm `src tests scripts`.)

Chờ duyệt: nhóm duyệt các mục ĐỀ XUẤT D-043..D-066; GVHD duyệt định nghĩa B3/B4 đổi vì grounding (D-061), biết trước
khi demo apply trên lab (D-064), duyệt bốn cột và luật split của benchmark (D-065), và Q1–Q11 ở mục 5 của `DECISIONS.md`.
