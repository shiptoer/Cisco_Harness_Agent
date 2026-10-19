# Giao diện vận hành (ops UI) — MVP

Trạng thái: ĐỀ XUẤT (D-057). Thiết kế do TV1 duyệt ngày 15/10/2026, trước khi viết code. Nhánh `tv1/ops-ui`
(worktree `cha-ui`). File này ghi lại thiết kế đã duyệt để phiên sau đọc lại; quyết định và trạng thái nằm ở D-057.

## 1. Mục đích và phạm vi

Một console để kỹ sư mạng nhập intent, xem harness đã làm gì với lượt chạy đó, và duyệt change package.

- Một lượt = pipeline đủ hai stage (Context-Building → Tool-Selection) do runner chạy, như `run_cases`.
- Duyệt **sau khi render**, một lượt duyệt. Đã duyệt thì được xuất `.cfg`. **Không deploy.**
- Không có LLM agent đứng ngoài UI. Không dùng UI trong lô đo: số liệu Ch4 chỉ lấy từ `run_cases` / `run_corpus`.
- Ràng buộc khi làm: không sửa `runner/`, `harness/`, `models/`; không thêm dependency (chỉ thư viện chuẩn).

Để giai đoạn 2: duyệt hai giai đoạn (duyệt change request trước khi render, `prepare_only` → `confirmed_intent`
như bản của Khang); thử với dsh thật.

## 2. Quyết định đã duyệt

| # | Quyết định |
| --- | --- |
| 1 | Duyệt change package SAU render, một lượt. |
| 2 | `evaluated_at` = đồng hồ máy lúc bấm chạy, chỉ UI đọc; truyền vào runner qua `--evaluated-at`; nằm trong `case_start` của trace. Runner vẫn không đọc đồng hồ (D-054). |
| 3 | Không JavaScript. CSP chặn script. Tiến trình hiển thị bằng `<meta http-equiv="refresh">`. |
| 4 | Runner chạy bằng process con (`python -m harness_agent.runner`, argv dạng list, không shell). Một worker: một lượt một lúc. `run_id` do server sinh, không dùng lại. |
| 5 | Chế độ fake (`requires_api_key = false`): chọn kịch bản `fake_dsh` theo tên; ô intent tự điền intent của kịch bản và CHỈ ĐỌC; trang ghi "Kết quả do kịch bản quyết định, không do intent." |
| 6 | `--reviewer <tên>` lúc khởi động; ghi vào bản ghi duyệt. |
| 7 | Bản ghi `human_review` nối tiếp chuỗi hash của `trace.jsonl` hiện có (actor `reviewer`). |
| 8 | Topology: SVG do server sinh, không JS. |
| 9 | `leak_in_package = true` thì không hiển thị package và không cho duyệt (fail-closed). |
| 10 | Role, mức (chỉ B3 hoặc B4), context (kèm SHA-256 ghim, D-054), cấu hình, người duyệt: cố định lúc khởi động. Form có trường `role`, `level` hay khoá lạ → 400. |

## 3. Kiến trúc

```text
trình duyệt ──HTTP 127.0.0.1──► web.py (Auth, Handler) ──► app.py (UiApp) ──subprocess──► runner ─► dsh ⇄ harness
                                     │                          │
                                     └── views.py (HTML, esc)   └── đọc run-dir theo allowlist, ghi human_review
```

| File | Vai trò |
| --- | --- |
| `src/harness_agent/ui/app.py` | Logic, không biết HTTP: cài đặt khởi động, freshness, khoá chạy, argv của runner, đọc run-dir theo allowlist và che secret, duyệt, xuất `.cfg`, dữ liệu topology. |
| `src/harness_agent/ui/web.py` | HTTP (`ThreadingHTTPServer`): token → cookie phiên, CSRF, `Origin`, `Host`, header bảo mật, định tuyến. |
| `src/harness_agent/ui/views.py` | HTML. `esc` là điểm escape duy nhất. Pipeline checklist (i)–(iv). |
| `src/harness_agent/ui/style.py` | CSS phục vụ ở `/static/ui.css` (CSP `style-src 'self'`, không có style inline). |
| `src/harness_agent/ui/topology.py` | SVG topology: thiết bị xếp theo tầng theo role, link kèm tên interface rút gọn. |
| `scripts/ui_server.py` | Điểm khởi động; in chế độ và đường dẫn đăng nhập có token. |

Run-dir: `<out>/<run_id>/` (cấu trúc như `run_cases`). Chỉ mục của UI: `<out>/_ui/runs.jsonl` (lượt đã khởi động,
`evaluated_at`) và `<out>/_ui/exits.jsonl` (exit code). `run_id` dạng `ui-YYYYMMDDtHHMMSSz-<6 hex>`; chỉ id có trong
chỉ mục mới mở được (chặn path traversal).

## 4. Bảo mật

- Bind `127.0.0.1`. `Host` phải là `127.0.0.1:<port>` hoặc `localhost:<port>`.
- Token ngẫu nhiên lúc khởi động, chỉ in ra console. `/login?token=…` đổi lấy cookie `HttpOnly; SameSite=Strict`,
  rồi chuyển hướng bỏ token khỏi URL. Không log URL.
- Mọi POST: `Origin` đúng của server, `Content-Type` form, độ dài ≤ 8192, đúng tập khoá của route, CSRF HMAC theo
  phiên.
- `Referrer-Policy: same-origin`, KHÔNG `no-referrer`: với `no-referrer`, Chrome gửi `Origin: null` khi submit form,
  và mọi form hợp lệ bị 403 (phát hiện khi chạy thử bằng trình duyệt, 15/10/2026; có test hồi quy).
- CSP: `default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none';
  base-uri 'none'`. Thêm `X-Frame-Options: DENY`, `nosniff`, `Cache-Control: no-store`.
- Intent: kiểm ký tự điều khiển và độ dài (≤ 2000); từ chối intent chứa secret của context.
- Chỉ hiển thị file và trường theo allowlist; free-text từ context (description, banner) che secret và escape. Không
  hiển thị prompt, output thô của dsh, patch, `.dsh_home`.
- Snapshot quá hạn (`CTX_STALE` theo `provenance`) hoặc file context đổi so với SHA lúc khởi động → khoá nút chạy.
- Xuất `.cfg` chỉ khi đã duyệt VÀ file vẫn đúng SHA-256 đã ghi trong bản duyệt.

## 5. Màn hình

1. **Bảng điều khiển** (`/`): thanh trạng thái (chế độ, role, mức, người duyệt, SHA context, độ tươi của snapshot:
   vàng khi còn ≤ 20% TTL, đỏ khi quá hạn); form chạy (chế độ thật: ô intent; chế độ fake: chọn kịch bản bằng form
   GET, intent chỉ đọc, kèm cảnh báo); topology SVG, bảng thiết bị, interface trong `<details>`; lịch sử lượt chạy,
   mới nhất trước.
2. **Lượt chạy** (`/run/<id>`): đang chạy thì meta refresh và bảng trace tăng dần. Xong thì:
   - Dải pipeline theo thứ tự (i) context validation → (ii) projection → (iii) gate G1–G6 → render → (iv) sensors.
     Bước chặn được tô đỏ và ghi lý do (vd `G4_blast_radius`); bước không bật ở mức này hiện là "tắt"; clarify màu vàng.
   - Verdict (xanh accept / đỏ reject / vàng clarify), change request, kết quả gate, sensors, CLI từng thiết bị
     (monospace) kèm SHA-256.
   - Form duyệt: lý do (bắt buộc khi từ chối, ≤ 500 ký tự), nút "Duyệt change package" / "Từ chối".
   - Sau khi duyệt: bản duyệt, link xuất `.cfg` từng thiết bị kèm hash.
   - Bảng trace và trạng thái chuỗi hash ("chuỗi hash hợp lệ" hoặc lỗi, khi lỗi thì không cho duyệt).
3. **Trang lỗi**: mã HTTP và thông điệp ngắn, không lộ chi tiết nội bộ.

## 6. Phong cách

Console vận hành cho kỹ sư mạng: dày thông tin, ưu tiên bảng, không thẻ to bo tròn, không gradient, không emoji,
không icon trang trí. Font sans hệ thống; CLI, cấu hình, hash, thời điểm dùng monospace (Cascadia Mono / Consolas).
Màu trung tính; màu chỉ mang nghĩa: đỏ = reject/chặn, xanh = accept, vàng = clarify / snapshot sắp hết hạn.
Sáng/tối theo `prefers-color-scheme`. Dùng được bằng bàn phím: skip link, `:focus-visible`, nút là `<button>` thật,
`prefers-reduced-motion`.

## 7. Dùng lại từ repo Khang

Chép ý, không import: header bảo mật, không log URL, kiểm `Host` và `Origin`, form đúng kiểu / kích thước / tập khoá,
từ chối intent chứa secret, khoá chạy không chờ. Không dùng: MCP bridge (`web_multi_server.py`,
`web_pipeline_server.py`), cấu hình qua biến môi trường, launcher.

## 8. Test

`tests/test_ui.py` (nhanh, launcher giả ghi run-dir thật; server chạy trong thread, gọi bằng `http.client`). Mỗi
control có đối chứng âm:

| Control | Test |
| --- | --- |
| Token, cookie | thiếu/sai token → 403/401; đúng → 303 kèm cookie |
| `Host` | Host lạ → 400 |
| Header, không script | mọi trang có CSP; không có `<script` ngoài payload đã escape |
| CSRF + `Origin` | thiếu hoặc sai → 403 |
| Trường lạ | `role`, `level`, `context`, khoá lạ → 400 |
| XSS | payload trong description, banner, intent đều escape; đối chứng âm: gỡ `esc` thì payload lọt vào trang |
| Path traversal | `run_id` và tên thiết bị ngoài chỉ mục → 404 |
| `leak_in_package` | ẩn package, không cho duyệt; đối chứng: không leak thì hiện và duyệt được |
| Khoá một lượt | lượt thứ hai khi đang chạy → 409 |
| Chuỗi hash | duyệt nối tiếp chuỗi, `verify_chain` hợp lệ; đối chứng: trace bị sửa thì phát hiện và từ chối duyệt |
| Xuất `.cfg` | cần duyệt; `.cfg` đổi sau duyệt → từ chối |
| Freshness | quá hạn → khoá; tươi / sắp hết hạn → chạy được; file context đổi → khoá |
| `Referrer-Policy` | `Origin: null` → 403, header là `same-origin` |

`tests/test_ui_e2e.py` (slow, runner thật bằng process con với `config/fake.toml`): accept → duyệt → xuất qua
`UiApp`; toàn bộ luồng qua HTTP như trình duyệt (login, chọn kịch bản, trang tiến trình có meta refresh, duyệt, tải
`.cfg`); reject ở gate G4 hiện đúng bước.

## 9. Chạy thử

```powershell
python scripts\ui_server.py --config config\fake.toml --context data\contexts\campus-small-01.json `
  --role network_admin --level B4 --reviewer "<tên>" --out runs-ui --port 8765
```

Mở đường dẫn `http://127.0.0.1:8765/login?token=…` in ra console. `runs-ui/` nằm trong `.gitignore`.
