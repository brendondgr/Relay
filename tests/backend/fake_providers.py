"""A provider's API on 127.0.0.1 for tests: a real HTTP server, so the official SDKs (anthropic,
google-genai) and relay's own httpx client talk to it exactly as they would to the real thing.

    srv = FakeProvider()                       # starts on a free port
    srv.route("GET", "/v1/models", lambda req: (200, {...}))
    srv.route("POST", "/v1/messages", lambda req: (200, sse_events([...])))   # an SSE stream
    srv.url  -> "http://127.0.0.1:<port>"
    srv.calls -> [Req(method, path, query, headers, json), ...]

A handler returns (status, body) where body is a dict (JSON), bytes, or a list of bytes chunks (sent
one by one with a flush, as SSE). Unknown routes answer 404.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit


@dataclass
class Req:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    json: Any = None


def sse_events(events: list[tuple[str | None, dict]]) -> list[bytes]:
    """Anthropic-style SSE (`event:` + `data:`) when a name is given, else data-only lines (Gemini, OpenAI)."""
    out = []
    for name, data in events:
        line = (f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}\n\n"
        out.append(line.encode())
    return out


@dataclass
class FakeProvider:
    routes: dict[tuple[str, str], Callable[[Req], tuple[int, Any]]] = field(default_factory=dict)
    calls: list[Req] = field(default_factory=list)

    def __post_init__(self) -> None:
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _handle(self, method):
                u = urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                req = Req(method, u.path, parse_qs(u.query), {k.lower(): v for k, v in self.headers.items()}, body)
                srv.calls.append(req)
                fn = srv.routes.get((method, u.path)) or next(
                    (f for (m, p), f in srv.routes.items() if m == method and p.endswith("*") and u.path.startswith(p[:-1])), None)
                status, out = fn(req) if fn else (404, {"error": {"message": f"no route {method} {u.path}"}})
                if isinstance(out, list):
                    self.send_response(status)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk in out:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def route(self, method: str, path: str, fn: Callable[[Req], tuple[int, Any]]) -> None:
        self.routes[(method, path)] = fn

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
