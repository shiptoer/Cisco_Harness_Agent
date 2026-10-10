# Checklist trước lần chạy LLM thật đầu tiên

Ngày soát: 01/10/2026. Người soát: TV1 (qua Claude Code). **Chưa gọi API lần nào để viết file này.**

Mỗi kết luận có một trong ba nhãn nguồn:

- **[log spike]**: thấy trong dữ liệu thật của spike 1.1 (dsh `0.2.0-rc.1`, model `deepseek-flash`, 29–30/09):
  9 file `spikes/dsh_mcp/logs/*.ndjson` và 17 session trong `spikes/dsh_mcp/.dsh_home`.
- **[tài liệu]**: đọc từ `--help`, README hoặc mã nguồn của gói `@deepseek-ai/dsh@0.2.0-rc.1` đang cài.
- **[thử không key]**: chạy dsh thật trong thư mục scratch, không có `DEEPSEEK_API_KEY`, `DSH_HOME` riêng. dsh dừng ở
  `MISSING_CREDENTIAL` trước khi gửi request nào.

Chỗ chưa có bằng chứng ghi **cần xác nhận ở lần chạy thật**. Số dòng code tính theo nhánh
`tv1/1.5-outcome-codes` tại `e97c95f`.

> **Cập nhật sau khi soát (cùng ngày), nhánh `docs/dsh-entry-point`:**
>
> | Mục trong file này | Trạng thái trên nhánh đó |
> |---|---|
> | Việc chặn 1 (thứ tự `--patch`) | đã sửa; `fake_dsh` nay kiểm thứ tự như launcher thật |
> | Việc chặn 2 (BOM) | đã sửa: loader đọc `utf-8-sig`. `check_setup` trên `config/local.toml` nguyên trạng nay `[OK]` |
> | Mục 2 (`DSH_HOME`) | runner đặt DSH_HOME theo từng case ở `runs/<id>/.dsh_home`, quét session log sau mỗi stage, ghi nguồn `dsh_home` vào `leak_sources`, rồi xoá. Việc 11 và 13 ở mục 4 nay đọc từ `result.json`; muốn xem session log thì chạy với `--keep-dsh-home` |
> | Mục 3 (model) | đã ghim ở `config/model.toml`; `result.json` có `model` và `reasoning_effort` thực tế. Việc 9 ở mục 4 nay đọc từ `result.json` |
> | Mục 6 (task 1.2) | chưa làm gì. **Số D-009 trên nhánh 1.2 phải đổi khi rebase nhánh đó** (trùng D-009 trên `main`; số trống kế tiếp sau PR setup và PR 1.5 là D-021) |
>
> Phần còn lại của file giữ nguyên như lúc soát, để thấy được trạng thái trước khi sửa. Lần chạy thật đầu tiên phải
> dùng code đã có các bản sửa này (merge PR setup trước).

## 0. Ba việc chặn — phải xử lý trước khi chạy

| # | Vấn đề | Bằng chứng | Hệ quả nếu không sửa |
|---|---|---|---|
| 1 | **Runner đặt `--patch` sau `--json`.** `runner/dsh.py:198` dựng lệnh `--profile headless --json --patch <file> … -`. Launcher của dsh dừng đọc cờ của mình ở token đầu tiên nó không nhận ra (`--json`), nên `--patch` rơi xuống app headless | [tài liệu] README: "The first token the launcher does not recognize starts the app's arguments"; `lib/bin.js` dùng `passThroughOptions()`. [thử không key] thứ tự của runner → stdout `{"type":"error","message":"unknown option '--patch'"}`, exit 1, harness không khởi động. Thứ tự của spike (`--profile headless --patch <file> --json -`) → harness khởi động, dừng ở `MISSING_CREDENTIAL` | Mọi case chạy thật ra ERROR `DSH_EXIT`. Fail-closed, không nguy hiểm, nhưng không case nào chạy được. Lỗi có cả trên `main`. `fake_dsh` không bắt được vì nó nhận cờ theo thứ tự bất kỳ |
| 2 | **`config/local.toml` có BOM UTF-8.** `LocalConfig.load` đọc bằng `utf-8` nên `tomllib` lỗi ở ký tự đầu | `check_setup` in `[LỖI]  đọc config: Invalid statement (at line 1, column 1)`. 3 byte đầu của file là `EF BB BF`. Nội dung còn lại hợp lệ, đủ khoá | Runner và `run_cases.py` dừng ngay với lỗi cấu hình |
| 3 | **Chưa có API key trong terminal** | `check_setup` dòng cuối | Đúng như dự kiến; nhập bằng `Read-Host -AsSecureString` |

Cách sửa đề xuất (chưa làm, chờ bạn quyết):

- (1) Đổi `runner/dsh.py:198` thành `[..., "--profile", "headless", "--patch", str(patch), "--json", *cfg.extra_args]`,
  và thêm vào `fake_dsh.py` kiểm tra thứ tự như launcher thật (từ chối `--patch` đứng sau một cờ lạ), để chạy khô bắt
  được lỗi này. `extra_args` hiện nằm sau `--json` nên là tham số của app, không phải của launcher.
- (2) Lưu lại `config/local.toml` dạng UTF-8 không BOM (VS Code: "Save with Encoding" → UTF-8), hoặc cho
  `LocalConfig.load` đọc bằng `utf-8-sig` như `ContextStore.load` đang làm.

### Kết quả `check_setup`

Chạy nguyên trạng trên `config/local.toml`:

```text
[LỖI]  đọc config: Invalid statement (at line 1, column 1)
```

Chạy trên bản sao tạm đã bỏ BOM (bản sao nằm ở scratch, đã xoá; không sửa file của bạn):

```text
[OK]   đọc config: …\scratchpad\local.nobom.toml
[OK]   python cho harness: D:\FPT\KLTN\Cisco-Harness-Agent\Cisco-Harness-Agent\.venv\Scripts\python.exe
[OK]   tồn tại node.exe: D:/FPT/KLTN/Cisco-Harness-Agent/Cisco-Harness-Agent/spikes/dsh_mcp/.tools/node/node.exe
[OK]   tồn tại bin.js: D:/FPT/KLTN/Cisco-Harness-Agent/Cisco-Harness-Agent/spikes/dsh_mcp/node_modules/@deepseek-ai/dsh/lib/bin.js
[OK]   gọi dsh: dsh --help chạy được
[OK]   context mẫu: 3 thiết bị
[OK]   base patch: cba.patch.yml
[OK]   harness preflight stage cba: get_device, get_interface, list_devices, submit_change_request
[LỖI]  API key trong terminal: chưa có
```

Khoá có trong `config/local.toml` (không đọc giá trị): `[dsh]` `base_patch`, `command`, `extra_args`, `home`,
`prompt_via`, `requires_api_key`, `timeout_s`; `[python]` `exe`. `check_setup` chỉ gọi `dsh --help` của launcher nên
không phát hiện được vấn đề 1.

## 1. Định dạng NDJSON headless thật so với những gì runner đọc

### 1.1. Lệnh gọi

| Runner làm gì | Ở đâu | dsh thật | Kết luận |
|---|---|---|---|
| `--profile headless` | `runner/dsh.py:198` | [tài liệu] "Run one fresh persisted session, print the final answer, and exit" | Khớp |
| `--json` | `runner/dsh.py:198` | [tài liệu] headless `--help`: "write newline-delimited run events to stdout instead of the final message" | Khớp |
| `--patch <file>` sau `--json` | `runner/dsh.py:198` | `--patch` là cờ của launcher, phải đứng trước tham số của app | **Sai thứ tự** (mục 0) |
| Prompt qua stdin, tham số `-` | `runner/dsh.py:201` | [tài liệu] "`-` reads stdin". [thử không key] với thứ tự đúng, session được tạo từ stdin | Khớp |
| `DSH_HOME`, `DSH_TELEMETRY_DISABLED=1` | `runner/dsh.py:206-207` | [log spike] spike chạy với đúng hai biến này | Khớp |
| `DSH_TELEMETRY_MODE=DISABLED` | `runner/dsh.py:208` | Không thấy trong tài liệu đã đọc | Vô hại; cần xác nhận có tác dụng không |
| `cwd` = gốc repo | `runner/dsh.py` (`cwd=REPO_ROOT`) | [tài liệu] thư mục gọi lệnh là workspace root. Spike 1.1 (Q4): dsh tìm key theo thứ tự biến môi trường → `$DSH_HOME/.credentials.yaml` → `.env` ở thư mục chạy lệnh | Gốc repo hiện chỉ có `.env.example`. Nếu sau này có `.env` chứa key thì dsh sẽ tự đọc |
| `failOnStartupError: true` trong patch | `runner/dsh.py:160` | [log spike] F11: không có tác dụng, dsh vẫn exit 0 khi MCP server chết | Runner không dựa vào nó; dùng trace `server_start` (`pipeline.py:423`) |

### 1.2. Loại event

[log spike] 9 file, 0 dòng không phải JSON, mỗi file đúng một `final`. JSON dạng nén (không có khoảng trắng sau `:`
và `,`), UTF-8, xuống dòng LF, không BOM. Một file có BOM và CRLF (`02_probe_nokey.ndjson`) là do lượt đó ghi qua
PowerShell, không phải do dsh.

| `type` | Trường | Runner đọc | Leak scan (`harness/leak.py:56`) |
|---|---|---|---|
| `session` | `sessionId`, `cwd` | không | quét mọi chuỗi |
| `status` | `phase` (`turn_start`, `step_start`, `step_end`, `turn_end`), `turn`, `step`; `usage` ở `step_end` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `totalTokens`); `reason` ở `turn_end` (`{"kind":"completed"}` hoặc `{"kind":"error","error":{"message","code"}}`) | không | quét |
| `thinking` | `text` | không | quét; đường dẫn `text` |
| `tool_call` | `callId`, `tool`, `input` (object) | `runner/dsh.py:245` lấy `tool`; `pipeline.py:373-374` so với `mcp__harness__<tool>` | quét; đường dẫn `input.<trường>` hoặc `tool` |
| `tool_result` | `callId`, `status` (chỉ thấy `completed`), `result` (chuỗi) | không | quét; đường dẫn `result` |
| `text` | `text` | không | quét |
| `final` | `text` | `runner/dsh.py:246`; `pipeline.py:389` đòi phải có | quét |
| `error` | `message` | không đọc riêng | quét |

`error` là loại event chưa có trong log spike; thấy ở [thử không key] khi dsh từ chối tham số dòng lệnh.

Khớp với runner:

- Tên tool trong `tool_call.tool` có dạng `mcp__<serverName>__<tool>` ([log spike]: `mcp__hello__echo`,
  `mcp__cisco__get_device`). Runner dùng `serverName = "harness"` (`harness/server.py:50`), so với
  `mcp__harness__<tool>`. Cần xác nhận ở lần chạy thật: tên thật đúng là `mcp__harness__…`.
- Quy tắc `MALFORMED_OUTPUT` (`pipeline.py:382-389`: phải có event, không có dòng ngoài JSON, có `final`) khớp cả 9 log.
- Runner đọc `utf-8-sig` (`runner/dsh.py:233`) nên BOM không gây lỗi.

Chưa khớp hoặc chưa chắc:

| Điểm | Hiện trạng | Việc cần làm |
|---|---|---|
| Lỗi API giữa chừng (hết hạn mức, 5xx, mạng) | [log spike] thiếu key → `turn_end.reason.kind = "error"`, `final.text = ""`, exit 1 → runner ra `DSH_EXIT` (`pipeline.py:368`). Runner không đọc `reason.kind` | Cần xác nhận ở lần chạy thật: mọi lỗi API đều cho exit ≠ 0. Nếu có lỗi mà exit 0 thì runner sẽ quy thành `AGENT_INCOMPLETE` |
| Model gọi tool không được cấp | Chưa có dữ liệu. [log spike] khi không có tool nào, model in markup gọi `bash` ra dạng text, dsh không phát `tool_call` | Cần xác nhận: dsh có phát `tool_call` cho tool ngoài danh sách không. Nếu không, `RUNTIME_TOOL_VIOLATION` sẽ hiếm gặp và tín hiệu nằm ở `text`/`final` |
| `tool_result.status` khi tool trả lỗi | Chỉ thấy `completed` | Cần xác nhận giá trị khi harness trả `isError` (reject của gate, lời gọi bị chặn) |
| Token và thời gian | `status.usage` có trong NDJSON nhưng runner không cộng vào `result.json` | Sau lần chạy đầu, cộng `usage` từ `<stage>.ndjson` bằng tay (mục 4) |
| Secret nằm trong `tool_result.result` | `result` là chuỗi chứa JSON. Secret có ký tự `"` hoặc `\` sẽ bị escape hai lớp và không khớp. Canary hiện tại chỉ có chữ, số, `-` nên không bị ảnh hưởng | Giới hạn đã biết; chỉ liên quan nếu corpus sinh secret có `"` hoặc `\` |
| Thinking | [log spike] mọi lượt có event `thinking`; text suy luận của model được lưu vào run-dir | Leak scan đã quét trường này; không cần làm gì |

### 1.3. Khác biệt giữa dsh thật và `fake_dsh`

| | dsh thật | `scripts/fake_dsh.py` |
|---|---|---|
| Thứ tự cờ | `--patch` phải trước tham số của app | nhận thứ tự bất kỳ |
| Event | `session`, `status`, `thinking`, `tool_call`, `tool_result`, `text`, `final`, `error` | `session`, `tool_call`, `tool_result`, `final` (dòng 70–90) |
| `session` | có `cwd` | có `offered_tools` (dsh thật không có) |
| JSON | nén, không khoảng trắng | `json.dumps` mặc định, có khoảng trắng |
| Sau khi tool trả lỗi hoặc bị chặn | model tự quyết: có thể thử lại, gọi tool khác, hoặc dừng | dừng ngay khi thấy `"terminal": true` hoặc `Session terminated` (dòng 88) |
| Số lời gọi | model có thể gọi thừa, thiếu, hoặc sai thứ tự | đúng theo kịch bản |
| `DSH_HOME` | ghi session log, cache (mục 2) | không ghi gì |
| Harness chết | exit 0, `final` có nội dung (F11) | giả lập đúng như vậy |

## 2. dsh ghi gì vào `DSH_HOME`

`[dsh] home` trong config là `runs/.dsh_home` (theo `config/local.example.toml`); thư mục này chưa tồn tại. Bảng dưới
lấy từ `spikes/dsh_mcp/.dsh_home` [log spike] và từ `DSH_HOME` scratch của [thử không key].

| Đường dẫn dưới `DSH_HOME` | Nội dung | Ghi chú |
|---|---|---|
| `profiles/headless/package.json`, `cordis.yml`, `cordis.patch.yml`, `pnpm-workspace.yaml` | Profile headless, tự tạo ở lần chạy đầu từ template đi kèm dsh [tài liệu] | Là CODE (F7): `cordis.patch.yml` được nạp mỗi lần khởi động. Cần xác nhận: lần tạo đầu không tải gì từ mạng |
| `sessions/--<cwd dạng slug>--/session-<uuid>/session.v4.jsonl.zstd` | **Toàn bộ phiên**, nén zstd: system prompt, prompt của user, `request/header` (cấu hình model, danh sách tool), `assistant/message` (kèm `usage`), `tool/call` (`name`, `arguments`), `tool/result` | Mỗi process dsh một session, tức 2 session mỗi case (cba, tsa). Tạo cả khi thiếu key. Với `cwd` = gốc repo, slug là `--D-FPT-KLTN-Cisco-Harness-Agent-Cisco-Harness-Agent--` |
| `storages/session_projcache/sessions/session-<uuid>.json` | JSON thường (không nén): tiêu đề phiên, `titleInput`, `tokenUsage`, `inbox`… | Chứa text lấy từ prompt |
| `.anonymous-user-id` | 37 byte | Định danh ẩn danh của dsh |
| `logs/` | Báo cáo lỗi khởi động | [tài liệu] mã nguồn `lib/bin.js`: "Raw diagnostics may contain configuration or credential values". Chưa thấy trong spike |
| `.credentials.yaml` | Nơi dsh tìm API key nếu không có biến môi trường (spike 1.1, Q4) | Không được tạo file này |
| `cordis.patch.yml` (ngay dưới `DSH_HOME`) | Lớp patch cấp home, áp sau lớp profile [tài liệu] | Không dùng; nếu có thì là CODE |

Hệ quả cho D-018:

- **Session log chứa nguyên tham số của mọi tool call.** [log spike] trong session của lượt `10_a_get_device`, tham số
  `SW-ACC-01` có trong `tool/call`, `tool/result`, `assistant/message`, `user/message`, `request/header`. Vậy nếu model
  đưa secret vào một lời gọi tool, secret sẽ nằm trong `DSH_HOME` dù harness đã chặn và runner đã che run-dir.
- **Quét byte thô sẽ sót**, vì session log nén zstd. Phải giải nén rồi mới tìm (script ở phụ lục; `zstandard 0.25.0`
  đang có trong `.venv` nhưng không khai trong `pyproject.toml`).
- Row `session-log-deepseek` đã tắt trong base patch chỉ dừng việc **upload** session log lên DeepSeek, không dừng
  việc ghi xuống đĩa.
- Cần bạn quyết sau lần chạy đầu: giữ `DSH_HOME` như hiện tại (dữ liệu toàn canary), xoá `sessions/` sau mỗi case,
  hay để runner quét và đưa vào `leak_sources`. Chưa làm gì.

## 3. Model đang cấu hình

`config/dsh.base.patch.yml` giống hệt `spikes/dsh_mcp/dsh.patch.cisco.yml` và **không khai model, không khai
thinking**. Nó chỉ tắt 20 row và có một khối MCP client. Model thực tế là mặc định của dsh:

| Thông số | Giá trị | Nguồn |
|---|---|---|
| provider | `deepseek-official` | [log spike] row `agent-default-model` trong `logs/05_config_cisco.txt:64`; `request/header` của 9 session |
| model | `deepseek-flash` | như trên; model trong phản hồi cũng là `deepseek-flash` (NOTES mục 7) |
| reasoningEffort | `high` (mặc định của adapter) | [log spike] `request/header.config`, `adapterDefaults.reasoningEffort = true` |
| thinking | **bật** | [log spike] mọi lượt có event `thinking` |
| maxTokens | 256000 (mặc định của adapter) | [log spike] |
| temperature | không gửi | [log spike] NOTES mục 7 |

Lưu ý:

- Model không được ghim trong patch của nhóm. Bản dsh đã ghim (`0.2.0-rc.1`) nên mặc định không tự đổi, nhưng báo cáo
  nên ghi model lấy từ session log của chính lượt chạy, không lấy từ file cấu hình.
- Tên model, và việc B0 chạy qua dsh, vẫn chờ GVHD duyệt (CLAUDE.md mục 12).

## 4. Việc phải soát sau lần chạy thật đầu tiên

Chạy đúng một case trước: `clean-vlan30`, sau khi đã xử lý mục 0.

```powershell
.\.venv\Scripts\python.exe scripts\run_cases.py --config config\local.toml clean-vlan30
```

| # | Soát gì | Xem ở đâu | Kỳ vọng |
|---|---|---|---|
| 1 | Kết cục | `runs\clean-vlan30\result.json` | `status=ok`, `verdict=accept`, `error_code=null`, `leak_sources=[]`. Model thật có thể cho kết cục khác; một lần chạy chưa kết luận được gì |
| 2 | Chuỗi tool call stage cba | `runs\clean-vlan30\cba.ndjson`, các dòng `tool_call` | Chạy khô: `list_devices`, `get_device`, `get_interface`, `submit_change_request`. Ghi lại chuỗi thật và số lần nộp |
| 3 | Chuỗi tool call stage tsa | `tsa.ndjson` | Chạy khô: `create_vlan`, `set_access_vlan`, `finalize`. Ghi lại: có gọi thừa, gọi lại, hay bỏ `finalize` không |
| 4 | Tên tool | `tool_call.tool` | Dạng `mcp__harness__<tool>` |
| 5 | Loại event và trường | `*.ndjson` | Đúng bảng 1.2; ghi lại loại hoặc trường mới |
| 6 | Dòng ngoài JSON, `final` | `result.json` (`MALFORMED_OUTPUT` nếu có) và `*.stderr.txt` | Không có dòng ngoài JSON; mỗi stage một `final` |
| 7 | Harness có chạy | `trace.jsonl` | Có `server_start` của `harness:cba` và `harness:tsa`; `trace_chain_ok = true` ở `case_end` |
| 8 | Gate | `trace.jsonl`, event `gate` | Mỗi lời gọi tsa có đủ 6 bước |
| 9 | Model thật | session log trong `runs\.dsh_home` (`request/context.model`) | `deepseek-flash`, `reasoningEffort=high` |
| 10 | Tool được cấp cho model | session log, `request/header.tools` | Stage cba: đúng 4 tool; stage tsa: đúng 12 tool; không có tool built-in nào của dsh |
| 11 | **Quét 7 secret trên `DSH_HOME`** | script ở phụ lục; chỉ báo CÓ/không và đường dẫn | `không` với `clean-vlan30`. Nếu CÓ: ghi file nào, dừng, quyết định xử lý trước khi chạy tiếp |
| 12 | Quét secret trên run-dir | `leak_sources` trong `result.json` | `[]` |
| 13 | `DSH_HOME` có gì | liệt kê `runs\.dsh_home` | Đúng bảng mục 2; ghi lại file hoặc thư mục mới |
| 14 | Token | cộng `usage` của các dòng `status` có `phase=step_end` trong hai file `.ndjson` | So với ước tính ở mục 5 |
| 15 | Thời gian | chênh `ts` giữa `stage_start` và `stage_end` trong `trace.jsonl` | So với ước tính ở mục 5 |
| 16 | Khác biệt so với `fake_dsh` | so `cba.ndjson` thật với lượt chạy khô cùng case | Ghi vào bảng 1.3 |

Sau đó mới chạy các case còn lại. Bỏ `fault-*` và `adv-credential-leak` khi chạy thật: các case này thiết kế cho chạy
khô (SETUP_WINDOWS mục 9.3).

## 5. Ước tính token, thời gian, chi phí

Đây là ước tính thô, sai số có thể tới hai lần. Số đo thật lấy ở việc 14 và 15.

Cơ sở:

- [log spike] một lượt dsh hai bước (một tool call) với prompt rất ngắn và một tool: 1 100–2 300 token tổng, 1,9–5,5
  giây. Phần cố định của dsh mỗi request khoảng 500 token.
- Kích thước phía nhóm: `prompts/cba.md` 2 196 ký tự, `prompts/tsa.md` 878 ký tự; khai báo tool stage cba 2 106 ký tự
  (4 tool), stage tsa 7 833 ký tự (12 tool); `get_device` trả 800–1 500 ký tự. Quy đổi khoảng 3,5 ký tự một token.
- Mỗi bước gửi lại toàn bộ hội thoại, nên input tăng theo số bước.

| | Bước (request) | Input (gồm phần cache) | Output |
|---|---|---|---|
| Stage cba, case accept | 5 | khoảng 12 000 | 1 000–2 000 |
| Stage tsa, 2 thao tác | 4 | khoảng 12 500 | khoảng 1 000 |
| **Một case accept** | 9 | **khoảng 25 000** | **2 000–3 000** |
| Một case reject hoặc clarify | 3–6 | 8 000–18 000 | 1 000–2 000 |

| | 15 case | 50 case |
|---|---|---|
| Token (input + output) | 0,3–0,45 triệu | 1–1,5 triệu |
| Thời gian chạy tuần tự (20–40 giây mỗi case) | 5–10 phút | 17–35 phút |
| Chi phí, cận trên (toàn bộ input tính giá cache miss, giờ cao điểm) | khoảng 0,17 USD | khoảng 0,55 USD |

Giá dùng để tính: `deepseek-flash`, mỗi triệu token input cache miss 0,15–0,30 USD, cache hit 0,003–0,006 USD, output
0,60–1,20 USD (thấp điểm–cao điểm). Nguồn: <https://api-docs.deepseek.com/quick_start/pricing>, đọc ngày 01/10/2026
qua công cụ tóm tắt trang; **kiểm tra lại trang giá trước khi dùng con số này**. Thực nghiệm chính thức chạy lặp k lần
mỗi case, nên nhân các số trên với k.

## 6. Đề xuất cho task 1.2 (record/replay) — ĐỀ XUẤT, chưa hiện thực

Bối cảnh: nhánh `origin/tv1/1.2-llm-record-replay` (1 commit WIP, `966901e`) ghi và phát lại ở tầng HTTP giữa dsh và
DeepSeek API. Mỗi entry là cặp (request body, response nguyên byte), khoá bằng sha256 của salt + header + body. Bản ghi
đó là **bản thô**: request chứa cả hội thoại, response chứa tool call của model. D-018 thì quy định output thô do runner
lưu phải được che. Hai việc này cần một quy tắc chung.

### Phương án A: chỉ replay từ bản đã che

Bản ghi dùng cho replay cũng bị che như output trong run-dir.

- Được: không nơi nào trên đĩa có secret.
- Mất: **case có leak không replay được.** Response đã che khiến dsh gọi tool với `[REDACTED]`; harness không chặn; tool
  result khác lượt gốc; request kế tiếp có body khác nên trượt khoá cache. Với thiết kế của nhánh 1.2, trượt khoá là
  HTTP 400 và case thành ERROR. Tức đúng các case adversarial về rò rỉ, là nhóm cần đo lại nhất khi sửa harness, lại
  không phát lại được.

### Phương án B (đề xuất): giữ bản thô, chỉ cho dữ liệu tổng hợp

- Bản ghi thô lưu vào `recordings/`, có trong `.gitignore`.
- **Chỉ ghi khi context có `"synthetic": true`.** Context không có cờ này thì không ghi bản thô; lượt chạy vẫn tiến
  hành, chỉ là không replay được.
- Replay dùng bản thô, nên tái tạo đúng lượt gốc, kể cả lời gọi chứa secret và việc harness chặn.
- Báo cáo, `result.json` và mọi thứ trong run-dir vẫn dùng bản đã che (D-018 không đổi).

Hai ràng buộc đi kèm:

1. **Test rằng `recordings/` bị ignore**: một test gọi `git check-ignore recordings/x` và đỏ nếu ai đó sửa `.gitignore`.
2. **Corpus chỉ sinh canary ngẫu nhiên từ 24 ký tự trở lên.** Lý do: `sha256` của dòng gốc trong `leak_sources` không
   có salt (mục 8.5 của file thiết kế). Người có `result.json` và dòng đã che có thể thử từng giá trị; secret ngắn
   hoặc đoán được thì dò ra. Canary ngẫu nhiên đủ dài thì không. Nên có test trên corpus cho ràng buộc này.

### So sánh

| | A: replay bản đã che | B: bản thô cho dữ liệu tổng hợp |
|---|---|---|
| Secret trên đĩa | không ở đâu cả | có trong `recordings/` (và vốn đã có trong `DSH_HOME`, mục 2) |
| Replay case có leak | không được (trượt khoá → ERROR) | được, giống lượt gốc |
| Replay case không leak | được | được |
| Rủi ro | mất khả năng đo lại nhóm case rò rỉ | bản thô lọt ra ngoài nếu `.gitignore` bị sửa, hoặc nếu chạy với dữ liệu thật. Hai ràng buộc trên và cờ `synthetic` chặn hai đường này |
| Phù hợp với phạm vi đồ án | — | dữ liệu của đồ án đều là tổng hợp; secret là canary |

Việc phải làm nếu chọn B (chưa làm việc nào):

- Thêm `"synthetic": true` vào context. `data/contexts/campus-small-01.json` hiện **chưa có** khoá này (các khoá
  top-level: `context_id`, `devices`, `domain_name`, `links`, `policy`, `schema_version`). Đây là thay đổi contract của
  `NetworkContext`, phải hỏi nhóm (CLAUDE.md mục 8) và ghi DECISIONS.
- Nhánh 1.2 đang lưu vào `cache/` (đã có trong `.gitignore`), không phải `recordings/`: cần thống nhất tên.
- Nhánh 1.2 đánh số quyết định của nó là D-009, trùng với D-009 đã có trên `main` (contract `ToolCall`). Khi rebase
  nhánh 1.2 phải đánh số lại.
- Canary hiện tại dài 31 ký tự, đạt ngưỡng 24. Cần xác nhận phần đuôi là ngẫu nhiên chứ không sinh từ tên context.
- Sửa câu "replay (task 1.2) chỉ dùng bản đã che" trong D-018 và mục 8.3 của file thiết kế nếu nhóm chọn B.

## Phụ lục: script quét `DSH_HOME`

Đã thử trên `DSH_HOME` scratch và trên `spikes/dsh_mcp/.dsh_home` (tìm đúng chuỗi có trong prompt, ở cả session log
nén lẫn projcache). Script chưa nằm trong repo; lưu thành file rồi chạy:

```powershell
.\.venv\Scripts\python.exe scan_dsh_home.py runs\.dsh_home data\contexts\campus-small-01.json
```

```python
"""Quét DSH_HOME tìm secret của context. Chỉ in CÓ/không và đường dẫn, không in giá trị."""

from __future__ import annotations

import sys
from pathlib import Path

import zstandard

from harness_agent.harness.context_store import ContextStore


def content(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix == ".zstd":  # session log của dsh nén zstd: quét byte thô sẽ sót
        with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
            return reader.read()
    return raw


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    home, context = Path(sys.argv[1]), sys.argv[2]
    needles = {s.encode("utf-8") for s in ContextStore.load(context).secret_values()}
    label = f"{len(needles)} secret của context"
    if len(sys.argv) > 3:  # chuỗi thử để tự kiểm script, vd một từ có trong prompt
        needles, label = {sys.argv[3].encode("utf-8")}, "chuỗi thử"
    # Tiền tố \\?\ để Windows không bỏ sót đường dẫn dài hơn 260 ký tự.
    root = Path("\\\\?\\" + str(home.resolve())) if sys.platform == "win32" else home.resolve()
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and "node_modules" not in p.parts]
    sessions = [p for p in files if p.suffix == ".zstd"]
    hits = [p for p in files if any(n in content(p) for n in needles)]
    print(f"DSH_HOME: {home} | {len(files)} file đã quét, trong đó {len(sessions)} session log | tìm {label}")
    print("KẾT QUẢ:", "CÓ" if hits else "không")
    for p in hits:
        print("  ", p.relative_to(root).as_posix())
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Tự kiểm trước khi tin kết quả "không": chạy thêm với tham số thứ ba là một từ có trong intent (vd `FINANCE`). Kết quả
phải là CÓ, kèm đường dẫn session log.
