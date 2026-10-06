"""Real WebSocket transport for the WeCom aibot channel.

Implements the official 智能机器人长连接 protocol
(https://developer.work.weixin.qq.com/document/path/101463):
- connect wss://openws.work.weixin.qq.com
- subscribe frame {cmd: aibot_subscribe, body: {bot_id, secret}} -> errcode 0
- inbound: aibot_msg_callback (text/image/...) and aibot_event_callback
- outbound: aibot_respond_msg (stream: {id, finish, content}) with the
  CALLBACK's req_id; aibot_send_msg for proactive pushes;
  aibot_respond_welcome_msg for enter_chat
- heartbeat: {cmd: ping} every 30s (pong comes back as errcode frames)
- rate limits: 30 msg/min, 1000/h per chat; stream window 10min
- single connection per bot: a second subscriber gets kicked

This module provides `make_ws_factory()` used by WecomAibotChannel(ws_factory=...).
The transport is asyncio/websockets based, bridged to the channel's threads
via a queue + loop runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

log = logging.getLogger("agenthub.wecom.ws")

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_S = 30
_EOF = object()  # queue sentinel: connection ended


class AibotWSAdapter:
    """Synchronous adapter matching the channel's ws_factory contract:

    connect() blocks until subscribed (or raises).
    recv(timeout) -> raw JSON str or None.
    send(raw_json_str).
    close().
    """

    def __init__(self, cfg: dict[str, Any]):
        self.bot_id = cfg["bot_id"]
        self.secret = cfg["secret"]
        self.ws_url = cfg.get("ws_url", WS_URL)
        self.connect_timeout = float(cfg.get("timeout_s", 20))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._queue: "asyncio.Queue[str | None]" = asyncio.Queue()
        self._stop_ev = asyncio.Event()
        self._hb_task: asyncio.Task | None = None
        self._subscribed = asyncio.Event()
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    # -- sync API (called from channel thread) ------------------------------
    def connect(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="aibot-ws", daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=self.connect_timeout):
            raise RuntimeError("ws loop did not start in time")
        fut = asyncio.run_coroutine_threadsafe(self._connect_and_subscribe(), self._loop)
        fut.result(timeout=self.connect_timeout + 15)  # raises on auth failure

    def recv(self, timeout: float = 1.0) -> str | None:
        """Return a raw frame, None on idle timeout, raise ConnectionError on EOF.

        Raising (rather than returning None) on EOF is what makes the channel's
        outer loop see an exception and run its reconnect/backoff path.
        """
        if self._loop is None:
            raise ConnectionError("ws loop not running")
        fut = asyncio.run_coroutine_threadsafe(self._queue_get(timeout), self._loop)
        try:
            item = fut.result(timeout=timeout + 2)
        except TimeoutError:
            return None
        if item is _EOF:
            raise ConnectionError("websocket closed by peer")
        return item

    def send(self, raw: str) -> None:
        if self._loop is None or self._ws is None:
            raise ConnectionError("ws not connected")
        asyncio.run_coroutine_threadsafe(self._ws.send(raw), self._loop).result(timeout=10)

    def close(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=8)
        except Exception:  # noqa: BLE001
            pass  # best-effort; the reader thread will die with the process

    # -- asyncio side ---------------------------------------------------------
    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_until_complete(self._stop_ev.wait())

    async def _queue_get(self, timeout: float):
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None  # idle: caller treats as "no message"


    async def _connect_and_subscribe(self) -> None:
        import websockets

        self._ws = await asyncio.wait_for(
            websockets.connect(self.ws_url, ping_interval=None), timeout=self.connect_timeout
        )
        log.info("[aibot-ws] connected %s", self.ws_url)
        req = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": uuid.uuid4().hex},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        }
        await self._ws.send(json.dumps(req))
        # wait for subscribe result
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.connect_timeout + 10)
        resp = json.loads(raw)
        if resp.get("errcode") != 0:
            self._ws = None
            raise PermissionError(f"aibot_subscribe failed: {resp}")
        log.info("[aibot-ws] subscribed ok (bot_id=%s...)", self.bot_id[:6])
        self._subscribed.set()
        self._hb_task = asyncio.create_task(self._heartbeat())
        asyncio.create_task(self._reader())

    async def _heartbeat(self) -> None:
        try:
            while not self._stop_ev.is_set() and self._ws is not None:
                await asyncio.sleep(HEARTBEAT_S)
                if self._ws is None:
                    return
                await self._ws.send(
                    json.dumps({"cmd": "ping", "headers": {"req_id": uuid.uuid4().hex}})
                )
        except Exception as e:  # noqa: BLE001
            log.warning("[aibot-ws] heartbeat stopped: %s", e)

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                await self._queue.put(raw if isinstance(raw, str) else raw.decode())
        except Exception as e:  # noqa: BLE001
            log.warning("[aibot-ws] reader ended: %s", e)
        finally:
            await self._queue.put(_EOF)  # signals recv() to raise -> channel reconnects

    async def _shutdown(self) -> None:
        self._stop_ev.set()
        if self._hb_task:
            self._hb_task.cancel()
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3)
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        try:
            await asyncio.wait_for(self._queue.put(_EOF), timeout=1)
        except Exception:  # noqa: BLE001
            pass


def make_ws_factory(**extra: Any) -> Callable[[dict[str, Any]], AibotWSAdapter]:
    """ws_factory for WecomAibotChannel: passes cfg through to AibotWSAdapter."""

    def factory(cfg: dict[str, Any]) -> AibotWSAdapter:
        cfg = dict(cfg)
        cfg.update(extra)
        return AibotWSAdapter(cfg)

    return factory
