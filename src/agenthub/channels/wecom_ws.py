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
        # keepalive bookkeeping: reader task + last inbound pong/frame time.
        # The 2026-10-07 outage: reader/heartbeat died silently while the
        # channel's recv() loop only ever saw None, so nothing reconnected for
        # 19h. recv() must detect a dead transport and raise.
        self._reader_task: asyncio.Task | None = None
        self._last_inbound = time.monotonic()

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
        if self._loop is None or self._loop.is_closed():
            raise ConnectionError("ws loop not running")
        # Cheap liveness first: a dead reader means an empty queue forever, so
        # detect it before paying the wait (this is the 2026-10-07 outage).
        if not self._reader_alive():
            raise ConnectionError("ws reader is dead; connection zombie")
        fut = asyncio.run_coroutine_threadsafe(self._queue_get(timeout), self._loop)
        try:
            item = fut.result(timeout=timeout + 2)
        except TimeoutError:
            item = None
        except Exception as e:  # noqa: BLE001
            raise ConnectionError(f"ws loop failed: {e}") from e
        if item is _EOF:
            raise ConnectionError("websocket closed by peer")
        if item is None:
            # idle: if nothing (not even a pong) has arrived for a long time the
            # TCP connection is half-open -> reconnect instead of idling forever
            self._check_stale_inbound()
        return item

    def _reader_alive(self) -> bool:
        task = self._reader_task
        if task is None:
            return True  # mock transports without a reader task
        return not task.done()

    def _check_stale_inbound(self) -> None:
        """No inbound frames (including pong replies) for too long = half-open
        TCP. Heartbeat sends ping but the pong never arrives; raise so the
        outer loop reconnects with a fresh socket."""
        stale_after = max(HEARTBEAT_S * 10, 300)
        if time.monotonic() - self._last_inbound > stale_after:
            raise ConnectionError("no inbound frames (incl. pong) for too long")

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
        self._reader_task = asyncio.create_task(self._reader())
        self._last_inbound = time.monotonic()

    async def _heartbeat(self) -> None:
        """Ping every HEARTBEAT_S; a pong (or any inbound frame) refreshes
        _last_inbound. Two consecutive failed pings -> close the socket so the
        reader ends and the channel reconnects, instead of silently pinging a
        dead TCP connection."""
        misses = 0
        try:
            while not self._stop_ev.is_set() and self._ws is not None:
                await asyncio.sleep(HEARTBEAT_S)
                if self._ws is None:
                    return
                try:
                    await self._ws.send(
                        json.dumps({"cmd": "ping", "headers": {"req_id": uuid.uuid4().hex}})
                    )
                    if time.monotonic() - self._last_inbound < HEARTBEAT_S * 2:
                        misses = 0
                    else:
                        misses += 1
                except Exception:  # noqa: BLE001
                    misses += 1
                if misses >= 2:
                    log.warning("[aibot-ws] heartbeat missed %d pongs; closing socket", misses)
                    try:
                        await self._ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("[aibot-ws] heartbeat stopped: %s", e)

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                self._last_inbound = time.monotonic()
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
