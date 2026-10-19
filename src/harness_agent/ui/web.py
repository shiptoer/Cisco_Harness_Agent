"""Lớp HTTP của giao diện vận hành (D-057). Chỉ thư viện chuẩn; chỉ bind 127.0.0.1.

Ý của các control lấy từ `web/approval_http.py` của repo Khang (header bảo mật, không log URL, kiểm `Host`,
kiểm `Origin`, form đúng kiểu, đúng kích thước, đúng tập khoá), viết lại cho thiết kế không có LLM agent
đứng ngoài:
- Token ngẫu nhiên lúc khởi động, chỉ in ra console. `/login?token=…` đổi token lấy cookie phiên
  (`HttpOnly; SameSite=Strict`) rồi chuyển hướng bỏ token khỏi URL. Mọi trang khác cần cookie hợp lệ.
- POST cần token CSRF (HMAC theo phiên) VÀ `Origin` đúng của server. Form chỉ nhận đúng tập khoá của route:
  `role`, `level` hay bất kỳ khoá lạ nào → 400 (role và mức cố định lúc khởi động server).
- CSP chặn script và mọi tài nguyên ngoài; trang không có JavaScript.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
import traceback
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import views
from .app import UiApp, UiError
from .style import CSS

COOKIE = "cha_ui"
REFERRER_POLICY = "same-origin"
MAX_FORM = 8192
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
_RUN = re.compile(r"^/run/([^/]+)$")
_REVIEW = re.compile(r"^/run/([^/]+)/review$")
_CFG = re.compile(r"^/run/([^/]+)/cfg/([^/]+)$")


class Auth:
    """Token khởi động, phiên và CSRF. Không lưu gì ra đĩa: khởi động lại server là mọi phiên hết hạn."""

    def __init__(self, token: str | None = None):
        self.token = token or secrets.token_urlsafe(32)
        self._key = secrets.token_bytes(32)
        self._sessions: set[str] = set()

    def login(self, token: str) -> str | None:
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            return None
        session = secrets.token_urlsafe(32)
        self._sessions.add(session)
        return session

    def valid(self, session: str | None) -> bool:
        return bool(session) and any(hmac.compare_digest(session, s) for s in self._sessions)

    def csrf(self, session: str) -> str:
        return hmac.new(self._key, session.encode(), hashlib.sha256).hexdigest()

    def csrf_ok(self, session: str, value: str) -> bool:
        return hmac.compare_digest(self.csrf(session), value)


def make_server(app: UiApp, auth: Auth, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(app, auth))
    server.daemon_threads = True
    return server


def _handler(app: UiApp, auth: Auth) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "cha-ui"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_args) -> None:  # URL /login mang token: không log
            pass

        # ---------------------------------------------------------------- phản hồi
        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # `same-origin`, KHÔNG `no-referrer`: với `no-referrer`, trình duyệt gửi `Origin: null` khi submit
            # form và phép kiểm Origin chặn luôn form hợp lệ của chính trang (thấy khi chạy thử bằng Chrome,
            # 15/10/2026).
            self.send_header("Referrer-Policy", REFERRER_POLICY)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _html(self, status: int, page: str) -> None:
            self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._html(status, views.error_page(status, message))

        def _redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location, **(extra or {})})

        # ---------------------------------------------------------------- kiểm request
        def _hosts(self) -> tuple[str, ...]:
            port = self.server.server_address[1]
            return (f"127.0.0.1:{port}", f"localhost:{port}")

        def _origin(self) -> str:
            # Origin phải khớp đúng Host của CHÍNH request này (đã qua `_host_ok`): mở UI bằng
            # http://localhost:<port> thì form gửi `Origin: http://localhost:<port>`; trước đây chỉ nhận
            # 127.0.0.1 nên đăng nhập được mà mọi form đều 403 (hồi quy 16/10/2026).
            return f"http://{self.headers.get('Host')}"

        def _host_ok(self) -> bool:
            return self.headers.get("Host") in self._hosts()

        def _session(self) -> str | None:
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                return None
            morsel = jar.get(COOKIE)
            return morsel.value if morsel and auth.valid(morsel.value) else None

        def _form(self, session: str, keys: set[str], optional: set[str] = frozenset()) -> dict[str, str]:
            if self.headers.get("Origin") != self._origin():
                raise UiError(403, "chỉ trang của chính server này được gửi form")
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                != "application/x-www-form-urlencoded"
            ):
                raise UiError(400, "kiểu form không hợp lệ")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise UiError(400, "độ dài form không hợp lệ") from None
            if not 1 <= length <= MAX_FORM:
                raise UiError(400, "độ dài form không hợp lệ")
            try:
                raw = self._post_body.decode("utf-8")
                form = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
            except (UnicodeDecodeError, ValueError):
                raise UiError(400, "form không đọc được") from None
            if not keys <= set(form) <= keys | optional or any(len(v) != 1 for v in form.values()):
                raise UiError(400, "form có trường lạ hoặc thiếu trường")
            values = {k: v[0] for k, v in form.items()}
            if not auth.csrf_ok(session, values.get("csrf", "")):
                raise UiError(403, "form hết hạn hoặc không phải của trang này; tải lại trang")
            return values

        # ---------------------------------------------------------------- định tuyến
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            try:
                if not self._host_ok():
                    raise UiError(400, "Host không hợp lệ")
                url = urlsplit(self.path)
                if url.path == "/static/ui.css":
                    self._send(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
                    return
                if url.path == "/login":
                    values = parse_qs(url.query).get("token", [])
                    session = auth.login(values[0]) if len(values) == 1 else None
                    if session is None:
                        raise UiError(403, "token không đúng; dùng đường dẫn in ra lúc khởi động server")
                    cookie = f"{COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"
                    self._redirect("/", {"Set-Cookie": cookie})
                    return
                session = self._session()
                if session is None:
                    raise UiError(401, "chưa đăng nhập; mở đường dẫn có token in ra lúc khởi động server")
                if url.path == "/":
                    picked = parse_qs(url.query).get("scenario", [None])[-1]
                    self._html(200, views.dashboard(app, auth.csrf(session), picked))
                    return
                if m := _RUN.match(url.path):
                    self._html(200, views.run_page(app, app.run_view(m.group(1)), auth.csrf(session)))
                    return
                if m := _CFG.match(url.path):
                    raw = app.export_cfg(m.group(1), m.group(2))
                    name = f"{m.group(2)}.cfg"
                    self._send(
                        200,
                        raw,
                        "text/plain; charset=utf-8",
                        {"Content-Disposition": f'attachment; filename="{name}"'},
                    )
                    return
                raise UiError(404, "không có trang này")
            except UiError as exc:
                self._error(exc.status, exc.message)
            except Exception:  # noqa: BLE001 — không lộ chi tiết lỗi ra trang
                self._internal_error()

        def _internal_error(self) -> None:
            # Trang chỉ nói "lỗi nội bộ"; traceback ra stderr của server để người vận hành chẩn đoán.
            # Không in URL (đường dẫn /login mang token) — traceback của Python không chứa URL.
            print(f"[UI] lỗi nội bộ khi xử lý {self.command}:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            self._error(500, "lỗi nội bộ của giao diện; xem traceback ở terminal đang chạy server")

        def do_POST(self) -> None:  # noqa: N802
            try:
                # Consume a bounded body before any early response. Closing with
                # unread request bytes can reset the connection on Windows.
                if self.headers.get("Transfer-Encoding") is not None:
                    raise UiError(400, "Transfer-Encoding không được hỗ trợ")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) > 1:
                    raise UiError(400, "độ dài form không hợp lệ")
                try:
                    length = int(lengths[0]) if lengths else 0
                except ValueError:
                    raise UiError(400, "độ dài form không hợp lệ") from None
                if not 0 <= length <= MAX_FORM:
                    raise UiError(400, "độ dài form không hợp lệ")
                self._post_body = self.rfile.read(length)
                if len(self._post_body) != length:
                    raise UiError(400, "form chưa được gửi đầy đủ")
                if not self._host_ok():
                    raise UiError(400, "Host không hợp lệ")
                session = self._session()
                if session is None:
                    raise UiError(401, "chưa đăng nhập")
                path = urlsplit(self.path).path
                if path == "/run":
                    keys = {"csrf", "scenario"} if app.fake else {"csrf", "intent"}
                    values = self._form(session, keys)
                    run_id = (
                        app.start_run(scenario=values["scenario"])
                        if app.fake
                        else app.start_run(intent=values["intent"])
                    )
                    self._redirect(f"/run/{run_id}")
                    return
                if m := _REVIEW.match(path):
                    values = self._form(session, {"csrf", "decision", "reason"})
                    app.record_review(m.group(1), values["decision"], values["reason"])
                    self._redirect(f"/run/{m.group(1)}")
                    return
                raise UiError(404, "không có trang này")
            except UiError as exc:
                self._error(exc.status, exc.message)
            except Exception:  # noqa: BLE001
                self._internal_error()

    return Handler
