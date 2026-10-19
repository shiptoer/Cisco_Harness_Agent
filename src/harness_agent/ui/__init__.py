"""Giao diện vận hành (human review UI, D-057): một server HTTP chỉ trên loopback để người vận hành nhập
intent,
chạy một lượt pipeline, đọc kết quả và duyệt change package. KHÔNG deploy, không có LLM agent đứng ngoài.

- `app.py`: lõi (trạng thái, chạy runner bằng process con, đọc run-dir theo allowlist, ghi bản duyệt vào
  trace).
- `web.py`: lớp HTTP (token, cookie, CSRF, Host, Origin, header bảo mật).
- `views.py`, `style.py`, `topology.py`: HTML, CSS và sơ đồ SVG do server sinh; không JavaScript.

Trạng thái: ĐỀ XUẤT (D-057). Không dùng trong lô đo.
"""
