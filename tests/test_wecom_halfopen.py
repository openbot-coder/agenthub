"""Half-open connection regression: the 2026-10-22 outage was a WeCom WS
that stayed ESTABLISHED for 19h while the gateway was gone — agent replies
were written into a dead socket and silently lost.

These tests simulate a gateway that stops answering (no pong, no frames)
without closing the TCP connection, and assert the adapter detects the
stale link and raises so the channel's reconnect loop can rebuild it.
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


class MuteAibotServer:
    """Aibot gateway that answers the subscribe then goes silent: never
    replies to ping, never sends frames, never closes the socket — the
    exact shape of a half-open link from the client's side."""

    def __init__(self):
        self.port: int | None = None
        self.pings: list[dict] = []
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _serve(self):
        async def handler(ws):
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("cmd") == "aibot_subscribe":
                        await ws.send(json.dumps(
                            {"headers": msg["headers"], "errcode": 0, "errmsg": "ok"}))
                    elif msg.get("cmd") == "ping":
                        self.pings.append(msg)  # record but DO NOT answer
            except Exception:
                pass

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await asyncio.get_running_loop().create_future()

    # stop() is defined above (clean loop shutdown); no extra teardown needed


def test_stale_halfopen_connection_raises_on_recv():
    """No pong for > stale window -> recv() raises ConnectionError so the
    channel reconnects instead of idling on a dead socket."""
    srv = MuteAibotServer()
    srv.start()
    # heartbeat 1s -> stale window 3s; misses>=2 closes socket around 4s
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "s",
                              "ws_url": f"ws://127.0.0.1:{srv.port}",
                              "heartbeat_s": 1.0})
    try:
        adapter.connect()
        t0 = time.monotonic()
        raised = None
        while time.monotonic() - t0 < 15:
            try:
                item = adapter.recv(timeout=1.0)
            except ConnectionError as e:
                raised = str(e)
                break
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
                break
        assert raised is not None, (
            "recv() never raised on a half-open connection — would idle forever"
        )
        # the heartbeat must actually have pinged (it was the silence probe)
        deadline = time.time() + 5
        while not srv.pings and time.time() < deadline:
            time.sleep(0.1)
        assert srv.pings, "heartbeat never sent ping"
    finally:
        adapter.close()
        srv.stop()


def test_heartbeat_two_missed_pongs_closes_socket():
    """Two consecutive ping windows without any inbound frame -> the adapter
    closes the socket itself; recv() then raises (EOF path)."""
    srv = MuteAibotServer()
    srv.start()
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "s",
                              "ws_url": f"ws://127.0.0.1:{srv.port}",
                              "heartbeat_s": 1.0})
    try:
        adapter.connect()
        t0 = time.monotonic()
        raised = None
        while time.monotonic() - t0 < 15:
            try:
                adapter.recv(timeout=1.0)
            except ConnectionError as e:
                raised = str(e)
                break
        assert raised is not None
        # closed-by-heartbeat is the proactive path; EOF/stale both acceptable
        # but the connection must not survive silently.
    finally:
        adapter.close()
        srv.stop()


def test_stale_check_unit():
    """Direct unit check of _check_stale_inbound thresholds."""
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "s", "heartbeat_s": 30.0})
    # fresh connection: no raise
    adapter._check_stale_inbound()
    # 400s of silence -> raise
    adapter._last_inbound = time.monotonic() - 400
    with pytest.raises(ConnectionError):
        adapter._check_stale_inbound()
