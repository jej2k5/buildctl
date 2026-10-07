"""The local web console: a standard-library HTTP server on 127.0.0.1.

Every API request carries the per-launch token, reads included, because the model holds the
pending nonces. The Host header must name this server, which defeats DNS rebinding, and a state
change must be a JSON POST from this origin, which a cross-site form cannot produce.
"""
from __future__ import annotations

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .actions import Build, Refused, dumps

WEB = Path(__file__).resolve().parent / "web"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


def handler_for(build: Build, token: str, port_ref: list[int]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "buildctl-ui"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature is fixed
            return

        def _allowed_host(self) -> bool:
            port = port_ref[0]
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._send(status, dumps({"error": message}))

        def _authorized(self) -> bool:
            supplied = self.headers.get("X-Buildctl-Token", "")
            return secrets.compare_digest(supplied.encode(), token.encode())

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if not self._allowed_host():
                return self._error(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                name = url.path.lstrip("/")
                if name == "" or name.endswith("/"):
                    name += "index.html"
                target = (WEB / name).resolve()
                if WEB not in target.parents or not target.is_file():
                    return self._error(HTTPStatus.NOT_FOUND, "not found")
                return self._send(HTTPStatus.OK, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"))
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong console token; reopen the link buildctl-ui printed")
            query = parse_qs(url.query)
            try:
                if url.path == "/api/model":
                    return self._send(HTTPStatus.OK, dumps(build.model()))
                if url.path == "/api/audit":
                    after = int(query.get("after", ["0"])[0]); limit = int(query.get("limit", ["500"])[0])
                    return self._send(HTTPStatus.OK, dumps(build.audit(after, min(limit, 2000))))
                if url.path == "/api/artifact":
                    return self._send(HTTPStatus.OK, dumps({"text": build.read_artifact(query.get("path", [""])[0])}))
                if url.path == "/api/trust-diff":
                    return self._send(HTTPStatus.OK, dumps(build.trust_diff()))
            except Refused as exc:
                return self._error(exc.status, str(exc))
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            if not self._allowed_host():
                return self._error(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port_ref[0]}", f"http://localhost:{port_ref[0]}"}:
                return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong console token")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
            if urlparse(self.path).path != "/api/pull":
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 10_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                job = build.pull(str(body["action"]), generation=int(body["generation"]),
                                 confirm=str(body.get("confirm", "")), operation=body.get("operation"))
            except Refused as exc:
                return self._error(exc.status, str(exc))
            except (KeyError, ValueError, TypeError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, f"bad request: {exc}")
            view = build._job_view(job)
            status = HTTPStatus.ACCEPTED if job.detached else (HTTPStatus.OK if job.returncode == 0 else HTTPStatus.CONFLICT)
            return self._send(status, dumps(view))

    return Handler


def serve(build: Build, *, port: int = 0, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(build, token, port_ref))
    port_ref[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/#token={token}"
    print(f"buildctl console for {build.repo}")
    print(f"  {url}")
    print("  Ctrl-C stops the console. A build it started keeps running.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
