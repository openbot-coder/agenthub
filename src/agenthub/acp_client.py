"""ACP client: JSON-RPC 2.0 over stdio to an agent subprocess.

M0 scope:
- launch agent subprocess, talk ACP frames over stdin/stdout
- initialize / session/new / session/prompt
- dispatch server->client notifications (session/update) and requests
  (fs/*, terminal/*, session/request_permission) to registered handlers
- thread-safe request/response correlation by id
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .framing import FrameError, decode_frames, decode_ndjson_lines, encode_frame, encode_ndjson

# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ACPError(Exception):
    """An error returned by the agent for a client request."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ACPProcessDead(ACPError):
    def __init__(self, detail: str = ""):
        super().__init__(INTERNAL_ERROR, f"agent process is not running: {detail}")


# ---------------------------------------------------------------------------
# handler signatures
# ---------------------------------------------------------------------------
NotificationHandler = Callable[[str, dict[str, Any]], None]
RequestHandler = Callable[[str, dict[str, Any]], Any]  # returns result, raises ACPError


@dataclass
class ACPClient:
    """A single ACP connection to one agent subprocess."""

    cmd: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    transport: str = "ndjson"  # "ndjson" (current ACP SDK agents) | "lsp-frame"

    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)
    _pending: dict[int, tuple[threading.Event, list]] = field(
        default_factory=dict, init=False, repr=False
    )
    _notif_handlers: list[NotificationHandler] = field(default_factory=list, init=False, repr=False)
    _req_handlers: list[RequestHandler] = field(default_factory=list, init=False, repr=False)
    _buf: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _stderr_lines: list[str] = field(default_factory=list, init=False, repr=False)
    _last_error: str = field(default="", init=False)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        flat: list[str] = []
        for c in self.cmd:
            flat.extend(shlex.split(c) if isinstance(c, str) else list(c))
        self._proc = subprocess.Popen(
            flat,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_loop, name="acp-reader", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name="acp-stderr", daemon=True).start()

    def close(self, timeout: float = 5.0) -> None:
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_lines[-50:])

    # -- low level io ------------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> None:
        if not self.is_alive():
            raise ACPProcessDead(self._last_error or "process exited")
        assert self._proc and self._proc.stdin
        data = encode_ndjson(obj) if self.transport == "ndjson" else encode_frame(obj)
        with self._write_lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise ACPProcessDead(str(e)) from e

    def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_lines.append(line)

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while not self._closed:
                chunk = self._proc.stdout.read(65536)
                if not chunk:
                    break
                self._buf.extend(chunk)
                if self.transport == "ndjson":
                    self._drain_ndjson()
                else:
                    self._drain()
        except (OSError, ValueError):
            pass
        finally:
            detail = self._last_error
            if not detail and self._proc:
                detail = f"exit={self._proc.poll()}"
            self._fail_all(f"reader stopped ({detail})")

    def _drain(self) -> None:
        """Parse complete frames out of the buffer, keeping partial tails.

        decode_frames raises FrameError for both 'need more bytes' and
        'corrupt stream'; we distinguish by scanning incrementally:
        try to parse with a shrinking window until only an incomplete
        tail remains.
        """
        while True:
            if not self._buf:
                return
            try:
                msgs = list(decode_frames(self._buf))
            except FrameError as e:
                msg = str(e)
                if "incomplete frame" in msg or "no header terminator" in msg:
                    return  # partial tail: wait for more bytes
                # corrupt stream: record and drop
                self._last_error = f"framing error: {e}"
                self._buf.clear()
                return
            if not msgs:
                return
            # decode_frames consumed the whole buffer by definition
            # (it only yields complete frames and the buffer holds only
            #  whole frames here, else it would have raised 'incomplete')
            self._buf.clear()
            for m in msgs:
                self._dispatch(m)
            return

    def _drain_ndjson(self) -> None:
        try:
            msgs, consumed = decode_ndjson_lines(self._buf)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._last_error = f"ndjson parse error: {e}"
            self._buf.clear()
            return
        if consumed:
            del self._buf[:consumed]
        for m in msgs:
            self._dispatch(m)

    def _fail_all(self, detail: str) -> None:
        self._last_error = detail
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _rid, (ev, slot) in pending:
            slot.append(ACPProcessDead(detail))
            ev.set()

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        has_id = "id" in msg
        method = msg.get("method")
        if method is not None and not has_id:
            for h in self._notif_handlers:
                try:
                    h(method, msg.get("params") or {})
                except Exception:  # noqa: BLE001 - handler errors must not kill the reader
                    pass
            return
        if method is not None and has_id:
            self._handle_server_request(msg)
            return
        if has_id:
            self._resolve_response(msg)

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        # Handlers may BLOCK (e.g. session/request_permission waits minutes for
        # the user's IM reply). Run them on a worker thread: blocking here would
        # stall the reader loop, and every pending request would time out.
        threading.Thread(
            target=self._run_server_request, args=(msg,), daemon=True,
            name=f"acp-srvreq-{msg.get('method')}-{msg.get('id')}",
        ).start()

    def _run_server_request(self, msg: dict[str, Any]) -> None:
        rid = msg["id"]
        method = msg["method"]
        params = msg.get("params") or {}
        result: Any = None
        error: dict[str, Any] | None = None
        handled = False
        for h in self._req_handlers:
            try:
                result = h(method, params)
                handled = True
                break
            except ACPError as e:
                error = {"code": e.code, "message": e.message, "data": e.data}
                handled = True
                break
            except NotImplementedError:
                continue
            except Exception as e:  # noqa: BLE001
                error = {"code": INTERNAL_ERROR, "message": str(e)}
                handled = True
                break
        if not handled:
            error = {"code": METHOD_NOT_FOUND, "message": f"unsupported: {method}"}

        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error:
            reply["error"] = error
        else:
            reply["result"] = result
        try:
            self._send(reply)
        except ACPProcessDead:
            pass

    def _resolve_response(self, msg: dict[str, Any]) -> None:
        rid = msg["id"]
        with self._lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return
        ev, out = slot
        if "error" in msg:
            err = msg["error"] or {}
            out.append(
                ACPError(err.get("code", INTERNAL_ERROR), err.get("message", "unknown"), err.get("data"))
            )
        else:
            out.append(msg.get("result"))
        ev.set()

    # -- public API --------------------------------------------------------
    def register_notification_handler(self, h: NotificationHandler) -> None:
        self._notif_handlers.append(h)

    def register_request_handler(self, h: RequestHandler) -> None:
        self._req_handlers.append(h)

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = 60.0
    ) -> Any:
        if not self.is_alive():
            raise ACPProcessDead(self._last_error or "not started")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            ev = threading.Event()
            slot: list = []
            self._pending[rid] = (ev, slot)
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except ACPProcessDead:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise ACPError(INTERNAL_ERROR, f"timeout waiting for {method} ({timeout}s)")
        out = slot[0]
        if isinstance(out, Exception):
            raise out
        return out

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- ACP convenience ---------------------------------------------------
    def initialize(
        self, client_capabilities: dict[str, Any] | None = None, protocol_version: int = 1
    ) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "clientCapabilities": client_capabilities
                or {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        )

    def session_new(self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None) -> str:
        res = self.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers or []})
        sid = (res or {}).get("sessionId")
        if not sid:
            raise ACPError(INTERNAL_ERROR, f"session/new returned no sessionId: {res!r}")
        return sid

    def session_prompt(
        self, session_id: str, blocks: list[dict[str, Any]], timeout: float | None = None
    ) -> dict[str, Any]:
        return self.request(
            "session/prompt", {"sessionId": session_id, "prompt": blocks}, timeout=timeout
        )

    def session_cancel(self, session_id: str) -> None:
        self.notify("session/cancel", {"sessionId": session_id})
