"""WeCom aibot channel: official WebSocket long-connection via @wecom aibot API.

Python port of dsh-im src/channels/wecom (runtime/bridge patterns), speaking
the aibot WebSocket protocol directly:
- wss long connection, auth handshake with bot token/secret
- client.replyStream(frame, reqId, chunk, finished) for streaming replies
- client.sendMessage(chatId, {msgtype}) for active sends
- reconnect with backoff; auth-failure is terminal

Config (dict):
    bot_id: str
    secret: str
    domain: str = "https://open.weixin.qq.com"   # aibot gateway
    connect_timeout_s: float = 20
    max_reconnect_attempts: int = 10
    reply_timeout_s: float = 600
    allowed_users: list[str] = []                # empty = allow all

Note: requires `websockets` (or aiohttp) at runtime; M1.5 ships with a
mockable transport so tests run without the real gateway.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable

from .base import IMChannel, InboundMessage, clean_control_chars, split_message_text

# aibot protocol message types (subset used here)
MT_REPLY_STREAM = "reply_stream"
MT_SEND_MESSAGE = "send_message"


class WecomAibotChannel:
    name = "wecom"

    def __init__(
        self,
        config: dict[str, Any],
        ws_factory: Callable[[dict[str, Any]], Any] | None = None,
        logger: Any = None,
    ):
        self.bot_id = config["bot_id"]
        self.secret = config["secret"]
        self.domain = config.get("domain", "https://open.weixin.qq.com")
        self.connect_timeout_s = float(config.get("connect_timeout_s", 20))
        self.max_reconnect = int(config.get("max_reconnect_attempts", 10))
        self.reply_timeout_s = float(config.get("reply_timeout_s", 600))
        self.allowed_users = set(config.get("allowed_users") or [])
        self._ws_factory = ws_factory  # returns object with send/recv/close
        self._logger = logger or _default_logger()
        self._msg_cb: Callable[[InboundMessage], None] | None = None
        self._sink: Callable[[str, str, dict], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None  # current socket (set by _connect_and_serve)
        # frame -> stream state for streaming replies
        self._streams: dict[str, dict] = {}
        self._stream_lock = threading.Lock()

    # -- IMChannel ---------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wecom-aibot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def on_message(self, cb: Callable[[InboundMessage], None]) -> None:
        self._msg_cb = cb

    def set_event_sink(self, sink: Callable[[str, str, dict], None]) -> None:
        self._sink = sink

    # -- connection loop ---------------------------------------------------
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connect_and_serve()
                attempt = 0
            except _AuthFailure as e:
                self._logger.error(f"[wecom] auth failed, giving up: {e}")
                return
            except Exception as e:  # noqa: BLE001
                attempt += 1
                if attempt > self.max_reconnect:
                    self._logger.error(f"[wecom] reconnect exhausted ({attempt}): {e}")
                    return
                delay = min(2**attempt, 30)
                self._logger.warning(f"[wecom] disconnected ({e}); retry {attempt}/{self.max_reconnect} in {delay}s")
                self._stop.wait(delay)

    def _connect_and_serve(self) -> None:
        if self._ws_factory is None:
            raise RuntimeError("wecom channel requires a ws_factory (real or mock transport)")
        ws = self._ws_factory(
            {
                "bot_id": self.bot_id,
                "secret": self.secret,
                "domain": self.domain,
                "timeout_s": self.connect_timeout_s,
            }
        )
        ws.connect()
        self._ws = ws
        self._logger.info("[wecom] WebSocket authenticated")
        try:
            while not self._stop.is_set():
                raw = ws.recv(timeout=1.0)
                if raw is None:
                    continue
                msg = json.loads(raw)
                self._dispatch(msg, ws)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- inbound -----------------------------------------------------------
    def _dispatch(self, msg: dict[str, Any], ws: Any) -> None:
        # aibot frames carry a callback payload; adapt per real protocol fields
        frame_id = msg.get("frame_id") or msg.get("msgid") or uuid.uuid4().hex
        body = msg.get("body") or msg
        sender = (body.get("from") or {}).get("userid") or body.get("sender") or ""
        chat_id = body.get("chatid") or body.get("chat_id") or sender
        if self.allowed_users and sender and sender not in self.allowed_users:
            self._logger.warning(f"[wecom] user {sender} not allowed, dropped")
            return
        text = ""
        images: list[bytes] = []
        msgtype = body.get("msgtype", "text")
        if msgtype == "text":
            text = (body.get("text") or {}).get("content", "")
        elif msgtype == "image":
            b64 = (body.get("image") or {}).get("base64", "")
            if b64:
                import base64

                images.append(base64.b64decode(b64))
        elif msgtype == "markdown":
            text = (body.get("markdown") or {}).get("content", "")
        text = clean_control_chars(text, max_len=100000).strip() if text else ""
        if not text and not images:
            return
        if self._msg_cb:
            self._msg_cb(
                InboundMessage(
                    channel=self.name,
                    chat_id=str(chat_id),
                    user_id=str(sender),
                    text=text,
                    images=images,
                    raw={"frame_id": frame_id, "msg": msg},
                )
            )

    # -- outbound ----------------------------------------------------------
    def reply(self, chat_id: str, text: str) -> None:
        for chunk in split_message_text(text, 4000):
            self._send({"type": MT_SEND_MESSAGE, "chatid": chat_id, "msgtype": "markdown", "markdown": {"content": chunk}})

    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None:
        if kind == "delta":
            self._stream_append(chat_id, str(payload.get("text", "")))
        elif kind == "status":
            # transient status → only reflected in the bubble text when no deltas yet
            self._stream_ensure(chat_id, str(payload.get("text", "")))
        elif kind == "final":
            self._stream_close(chat_id, str(payload.get("text", "")))
        elif kind == "permission":
            self.reply(chat_id, _permission_text(payload))
        # "replace" treated as delta reset for now

    # -- streaming state ---------------------------------------------------
    def _stream_key_states(self, chat_id: str) -> dict:
        with self._stream_lock:
            return self._streams.setdefault(chat_id, {"req_id": uuid.uuid4().hex, "buf": "", "sent": False})

    def _stream_ensure(self, chat_id: str, text: str) -> None:
        st = self._stream_key_states(chat_id)
        if not st["sent"]:
            self._reply_stream(chat_id, st, text or "正在处理…", finished=False)

    def _stream_append(self, chat_id: str, text: str) -> None:
        st = self._stream_key_states(chat_id)
        st["buf"] += text
        # rate: push edits at most every 0.8s (like dsh-im update_interval)
        now = time.monotonic()
        if now - st.get("last", 0) >= 0.8:
            st["last"] = now
            self._reply_stream(chat_id, st, st["buf"][-4000:], finished=False)

    def _stream_close(self, chat_id: str, final_text: str) -> None:
        st = self._stream_key_states(chat_id)
        text = final_text or st["buf"]
        self._reply_stream(chat_id, st, text, finished=True)
        with self._stream_lock:
            self._streams.pop(chat_id, None)

    def _reply_stream(self, chat_id: str, st: dict, text: str, finished: bool) -> None:
        self._send(
            {
                "type": MT_REPLY_STREAM,
                "chatid": chat_id,
                "req_id": st["req_id"],
                "content": text,
                "finished": finished,
            }
        )
        if finished:
            st["sent"] = True

    def _send(self, obj: dict) -> None:
        ws = getattr(self, "_ws", None)
        if ws is None:
            self._logger.warning("[wecom] send skipped, not connected")
            return
        ws.send(json.dumps(obj, ensure_ascii=False))


class _AuthFailure(Exception):
    pass


def _permission_text(payload: dict[str, Any]) -> str:
    title = payload.get("title") or payload.get("tool", "operation")
    return (
        f"⚠️ 权限请求：{title}\n"
        f"回复「允许」执行；其他回复视为拒绝。\n"
        f"请求 ID：{payload.get('request_id', '')}"
    )


def _default_logger():
    import logging

    return logging.getLogger("agenthub.wecom")
