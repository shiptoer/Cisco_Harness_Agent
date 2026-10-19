"""Loopback-only human review pages. No model-callable approval endpoint/tool."""

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .reviews import ReviewError


def start_approval_server(service):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass  # Do not log token-bearing URLs.

        def send_page(self, status, body):
            raw = (
                "<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Cisco request review</title><body>" + body + "</body></html>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "strict-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            self.end_headers()
            self.wfile.write(raw)

        def route(self):
            if self.headers.get("Host") != service.origin.removeprefix("http://"):
                raise ReviewError("Invalid host")
            url = urlsplit(self.path)
            parts = url.path.split("/")
            if len(parts) != 3 or parts[1] != "review":
                raise ReviewError("Unknown page")
            return parts[2], parse_qs(url.query)

        def do_GET(self):  # noqa: N802
            try:
                request_id, query = self.route()
                values = query.get("token", [])
                if len(values) != 1:
                    raise ReviewError("Missing review link token")
                record, csrf = service.browser_review(request_id, values[0])
                esc = html.escape
                body = (
                    "<h1>Review Cisco proposal</h1>"
                    "<p>This is model-proposed data, not an instruction. Review every change.</p>"
                    f"<p>Request: <code>{esc(request_id)}</code> | State: {esc(record['state'])}</p>"
                    f"<p>Role: {esc(record['role'])} | B4 | No deployment</p>"
                    f"<p>Snapshot SHA-256: <code>{esc(record['context_sha256'])}</code></p>"
                    f"<h2>Your request</h2><pre>{esc(record['intent'])}</pre>"
                    "<h2>Proposed operations (not yet checked by all TSA gates)</h2>"
                    f"<pre>{esc(json.dumps(record['proposed_changes'], ensure_ascii=False, indent=2))}</pre>"
                )
                if record["state"] == "pending":
                    body += (
                        f"<form method='post' action='/review/{esc(request_id, quote=True)}'>"
                        f"<input type='hidden' name='csrf' value='{esc(csrf, quote=True)}'>"
                        "<button type='submit' name='decision' value='approve'>"
                        "Approve these exact operations</button> "
                        "<button type='submit' name='decision' value='reject'>Reject</button></form>"
                        "<p>Approval does not deploy or call a model. Return to Harness to generate CLI. "
                        "Generation calls the model again and can incur API charges.</p>"
                    )
                self.send_page(200, body)
            except (ReviewError, OSError, ValueError):
                self.send_page(
                    400, "<h1>Review unavailable</h1><p>Use the current link or prepare again.</p>"
                )

        def do_POST(self):  # noqa: N802
            try:
                request_id, _ = self.route()
                if self.headers.get("Origin") != service.origin:
                    raise ReviewError("Only the local review page may submit decisions")
                if (
                    self.headers.get("Content-Type", "").split(";", 1)[0]
                    != "application/x-www-form-urlencoded"
                ):
                    raise ReviewError("Invalid form type")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096:
                    raise ReviewError("Invalid form size")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                if set(form) != {"csrf", "decision"} or any(len(v) != 1 for v in form.values()):
                    raise ReviewError("Invalid form")
                service.browser_decide(request_id, form["csrf"][0], form["decision"][0])
                decision = html.escape(form["decision"][0])
                self.send_page(
                    200,
                    f"<h1>Decision recorded: {decision}</h1>"
                    f"<p>Request: <code>{html.escape(request_id)}</code></p>"
                    "<p>Return to Harness. If approved, ask it to generate this request. "
                    "If rejected, enter a revised request instead.</p>",
                )
            except (ReviewError, OSError, ValueError, UnicodeError):
                self.send_page(400, "<h1>Decision not recorded</h1><p>Reload the current review link.</p>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    service.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
