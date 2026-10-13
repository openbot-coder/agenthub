"""HTTP surface for nginx reverse proxy (/agents).

Endpoints (JSON, programmatic — open, nginx-level gating applies):
  GET  /agents/status     -> {status, agents, channels, uptime}
  GET  /agents/sessions   -> live bindings (legacy; superseded by /api/sessions)
  GET  /agents/healthz    -> liveness (200)
  POST /agents/prompt     -> {channel, chat_id, text, agent?, cwd?}  (ops tool)

Admin UI (auth-gated via HTTP Basic from config http.admin; fail-closed 503
when http.admin is absent):
  GET  /agents/ , /ui             -> dashboard HTML
  GET  /agents/api/conversations  -> hub-side conversation logs
  GET  /agents/api/approvals      -> pending permission requests
  GET  /agents/api/sessions       -> bindings + pinned model
  GET  /agents/api/config         -> sanitized effective config

Runs a ThreadingHTTPServer in-thread; bound to loopback by default —
nginx on the same host proxies https://api.vxquant.com/agents/ to it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from agenthub.admin_data import (
    approvals_payload,
    check_auth,
    conversations_payload,
    sanitize_config,
    sessions_payload,
)
from agenthub.admin_ui import render_index


class StatusHTTP:
    def __init__(
        self,
        mgr: Any,
        host: str = "127.0.0.1",
        port: int = 30990,
        admin_cfg: dict[str, Any] | None = None,
    ):
        self.mgr = mgr
        self.host = host
        self.port = port
        self.admin_cfg = admin_cfg  # {"username","password"} or None (fail-closed)
        self.effective_cfg: dict[str, Any] = {}  # injected by runtime.main()
        self.started = time.time()
        self.channels: list = []  # injected by runtime.main() after build_channels
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass  # nginx access log covers it

            def _json(self, code: int, obj: dict[str, Any]) -> None:
                data = json.dumps(obj, ensure_ascii=False, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _html(self, data: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _require_auth(self) -> bool:
                if check_auth(self.headers.get("Authorization"), outer.admin_cfg):
                    return True
                if outer.admin_cfg:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="agenthub"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._json(
                        503,
                        {"error": "admin auth not configured; set http.admin in config.json"},
                    )
                return False

            def _route_get(self, path: str, qs: dict) -> None:
                m = outer.mgr
                if path in ("/", "/status"):
                    channels = []
                    with m._lock:
                        # channels registered on the runtime are injected here
                        # by main() (see set_channel_registry) so /status can
                        # report per-channel connection state incl. wecom WS
                        for ch in outer.channels:
                            channels.append({
                                "name": getattr(ch, "name", str(ch)),
                                "state": getattr(ch, "connection_state", "unknown"),
                                "error": getattr(ch, "connection_error", "") or None,
                            })
                    degraded = any(c["state"] in ("failed",) for c in channels)
                    self._json(
                        200,
                        {
                            "service": "agenthub",
                            "status": "degraded" if degraded else "running",
                            "uptime_s": round(time.time() - outer.started, 1),
                            "agents": m.list_agents(),
                            "channels": channels,
                        },
                    )
                elif path == "/healthz":
                    self._json(200, {"ok": True})
                elif path == "/sessions":
                    bindings = []
                    with m._lock:
                        for (channel, chat_id), b in m._bindings.items():
                            bindings.append(
                                {
                                    "channel": channel,
                                    "chat_id": chat_id,
                                    "agent": b.agent,
                                    "acp_session_id": b.acp_session_id,
                                    "last_active": b.last_active,
                                }
                            )
                    self._json(200, {"bindings": bindings})
                elif path in ("/ui", "/index.html"):
                    if not self._require_auth():
                        return
                    self._html(render_index().encode())
                elif path == "/api/conversations":
                    if not self._require_auth():
                        return
                    self._json(200, conversations_payload(m))
                elif path == "/api/approvals":
                    if not self._require_auth():
                        return
                    self._json(200, approvals_payload(m))
                elif path == "/api/sessions":
                    if not self._require_auth():
                        return
                    self._json(200, sessions_payload(m))
                elif path == "/api/config":
                    if not self._require_auth():
                        return
                    self._json(200, {"config": sanitize_config(outer.effective_cfg)})
                else:
                    self._json(404, {"error": "not found"})

            def _route_post(self, path: str, body: bytes) -> None:
                if path != "/prompt":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    req = json.loads(body or b"{}")
                    res = outer.mgr.prompt(
                        req["channel"], str(req["chat_id"]), str(req["text"]),
                        agent=req.get("agent"), cwd=req.get("cwd"),
                        timeout_s=float(req.get("timeout_s", 120)),
                    )
                    self._json(200, res)
                except KeyError as e:
                    self._json(400, {"error": f"missing field {e}"})
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": str(e)})

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path == "/":
                    # With auth configured, / serves the dashboard (browser entry).
                    # Without auth config, keep the legacy open JSON status so
                    # existing scripts don't break.
                    if outer.admin_cfg is not None:
                        if not self._require_auth():
                            return
                        self._html(render_index().encode())
                        return
                    self._route_get(path, parse_qs(parsed.query))
                    return
                self._route_get(path, parse_qs(parsed.query))

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self._route_post(urlparse(self.path).path.rstrip("/"), self.rfile.read(length))

        self._handler = Handler

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="agenthub-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
