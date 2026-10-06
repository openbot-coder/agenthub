"""Tests for the real aibot WebSocket transport (wecom_ws.AibotWSAdapter).

Spins a local websockets server that mimics the WeCom aibot gateway:
  - expects aibot_subscribe, replies errcode 0
  - pushes an aibot_msg_callback
  - records outbound frames (aibot_respond_msg stream)
  - answers ping with errcode 0
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

websockets = pytest.importorskip("websockets")

from agenthub.channels.wecom_ws import AibotWSAdapter  # noqa: E402


class FakeAibotServer:
    def __init__(self):
        self.received: list[dict] = []
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "fake server did not start"

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        async def handler(ws):
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self.received.append(msg)
                    cmd = msg.get("cmd")
                    if cmd == "aibot_subscribe":
                        await ws.send(json.dumps({"headers": msg["headers"], "errcode": 0, "errmsg": "ok"}))
                        # push a user message right after subscribing
                        await asyncio.sleep(0.1)
                        await ws.send(json.dumps({
                            "cmd": "aibot_msg_callback",
                            "headers": {"req_id": "seed-req"},
                            "body": {"msgid": "m1", "chatid": "chatA", "chattype": "single",
                                     "msgtype": "text", "from": {"userid": "userA"},
                                     "text": {"content": "hello"}},
                        }))
                    elif cmd == "ping":
                        await ws.send(json.dumps({"headers": msg["headers"], "errcode": 0, "errmsg": "ok"}))
            except Exception:
                pass

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await asyncio.get_running_loop().create_future()  # run forever

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


def test_adapter_subscribe_receive_and_reply():
    srv = FakeAibotServer()
    srv.start()
    adapter = AibotWSAdapter({"bot_id": "bot1", "secret": "sec1",
                              "ws_url": f"ws://127.0.0.1:{srv.port}"})
    adapter.connect()

    # first outbound frame must be the subscribe with credentials
    assert srv.received[0]["cmd"] == "aibot_subscribe"
    assert srv.received[0]["body"] == {"bot_id": "bot1", "secret": "sec1"}

    # inbound callback should come through recv()
    raw = adapter.recv(timeout=5)
    assert raw is not None
    frame = json.loads(raw)
    assert frame["cmd"] == "aibot_msg_callback"
    assert frame["body"]["text"]["content"] == "hello"

    # send a streaming reply; server must see it with the callback's req_id
    adapter.send(json.dumps({
        "cmd": "aibot_respond_msg",
        "headers": {"req_id": "seed-req"},
        "body": {"msgtype": "stream", "stream": {"id": "s1", "finish": False, "content": "partial"}},
    }))
    deadline = time.time() + 5
    while time.time() < deadline:
        if any(m.get("cmd") == "aibot_respond_msg" for m in srv.received):
            break
        time.sleep(0.05)
    replies = [m for m in srv.received if m.get("cmd") == "aibot_respond_msg"]
    assert replies and replies[0]["headers"]["req_id"] == "seed-req"
    assert replies[0]["body"]["stream"]["content"] == "partial"

    adapter.close()
    srv.stop()


def test_adapter_auth_failure_raises():
    class BadServer(FakeAibotServer):
        async def _serve(self):
            async def handler(ws):
                async for raw in ws:
                    msg = json.loads(raw)
                    await ws.send(json.dumps({"headers": msg.get("headers", {}), "errcode": 40001,
                                              "errmsg": "invalid secret"}))

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await asyncio.get_running_loop().create_future()

    srv = BadServer()
    srv.start()
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "bad", "ws_url": f"ws://127.0.0.1:{srv.port}"})
    try:
        adapter.connect()
        assert False, "connect() should raise on subscribe failure"
    except PermissionError as e:
        assert "40001" in str(e) or "subscribe failed" in str(e)
    finally:
        srv.stop()
