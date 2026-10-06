"""A minimal ACP *server* (agent side) used to test the client deterministically.

Implements just enough of the protocol:
- initialize -> protocolVersion + agentCapabilities
- session/new -> sessionId
- session/prompt -> streams a few session/update notifications, then returns
  {stopReason: end_turn}
- session/cancel -> aborts the in-flight prompt
- asks the client for permission once (session/request_permission) if
  --with-permission is passed

Run standalone:  python -m tests.fixtures.mock_acp_agent [--with-permission]
"""

from __future__ import annotations

import json
import sys
import threading
import uuid

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/src")

from agenthub.framing import decode_ndjson_lines, encode_ndjson  # noqa: E402


class MockAgent:
    def __init__(self, with_permission: bool = False, chunk_count: int = 3):
        self.with_permission = with_permission
        self.chunk_count = chunk_count
        self.sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending_permission: dict[int, threading.Event] = {}
        self._cancelled: set[str] = set()

    # -- io ---------------------------------------------------------------
    def send(self, obj: dict) -> None:
        sys.stdout.buffer.write(encode_ndjson(obj))
        sys.stdout.buffer.flush()

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        ev = threading.Event()
        self._pending_permission[rid] = ev
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            raise TimeoutError(method)
        return {}

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        buf = bytearray()
        while True:
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                break
            buf.extend(chunk)
            msgs, consumed = decode_ndjson_lines(buf)
            if consumed:
                del buf[:consumed]
            for m in msgs:
                self.handle(m)

    def handle(self, msg: dict) -> None:
        if "method" not in msg:  # response to our request
            rid = msg.get("id")
            if rid is None:
                return
            ev = self._pending_permission.pop(rid, None)
            if ev:
                ev.set()
            return
        method = msg["method"]
        rid = msg.get("id")
        if method == "initialize":
            self.reply(rid, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
        elif method == "session/new":
            sid = "s-" + uuid.uuid4().hex[:8]
            self.sessions[sid] = {"cwd": msg["params"].get("cwd")}
            self.reply(rid, {"sessionId": sid})
        elif method == "session/prompt":
            sid = msg["params"]["sessionId"]
            threading.Thread(target=self._run_prompt, args=(sid, rid), daemon=True).start()
        elif method == "session/cancel":
            self._cancelled.add(msg["params"]["sessionId"])
        else:
            self.reply_error(rid, -32601, f"unknown method {method}")

    def reply(self, rid, result) -> None:
        if rid is not None:
            self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def reply_error(self, rid, code, message) -> None:
        if rid is not None:
            self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def _run_prompt(self, sid: str, rid) -> None:
        import time as _time

        for i in range(self.chunk_count):
            # give the main loop a chance to receive session/cancel
            _time.sleep(0.01)
            if sid in self._cancelled:
                self._cancelled.discard(sid)
                self.reply(rid, {"stopReason": "cancelled"})
                return
            self.notify(
                "session/update",
                {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"chunk{i} "}},
                },
            )
            if i == 1:
                self.notify(
                    "session/update",
                    {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "t1",
                            "title": "read file",
                            "kind": "read",
                            "status": "completed",
                        },
                    },
                )
        if self.with_permission:
            self.request(
                "session/request_permission",
                {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "t2", "title": "rm -rf /tmp/x", "kind": "execute"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            )
        self.reply(rid, {"stopReason": "end_turn"})


def main() -> None:
    with_permission = "--with-permission" in sys.argv
    chunk_count = 3
    if "--chunks" in sys.argv:
        chunk_count = int(sys.argv[sys.argv.index("--chunks") + 1])
    MockAgent(with_permission=with_permission, chunk_count=chunk_count).run()


if __name__ == "__main__":
    main()
