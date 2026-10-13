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

from .base import IMChannel, InboundMessage, clean_control_chars, split_message_text, _interaction_text, _command_result_text

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
        # 0/negative = unlimited (default): network loss must never permanently
        # kill the bot. Backoff caps at 60s, so a retry storm is bounded anyway.
        self.max_reconnect = int(config.get("max_reconnect_attempts", 0))
        self.reply_timeout_s = float(config.get("reply_timeout_s", 600))
        self.allowed_users = set(config.get("allowed_users") or [])
        self._ws_factory = ws_factory  # returns object with send/recv/close
        self._logger = logger or _default_logger()
        self._msg_cb: Callable[[InboundMessage], None] | None = None
        self._sink: Callable[[str, str, dict], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None  # current socket (set by _connect_and_serve)
        self.connection_state = "idle"
        self.connection_error: str = ""
        # chat -> streaming state; chat -> last callback req_id (respond must reuse it)
        self._streams: dict[str, dict] = {}
        self._req_ids: dict[str, str] = {}
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
                if self.max_reconnect > 0 and attempt > self.max_reconnect:
                    self._logger.error(f"[wecom] reconnect exhausted ({attempt}): {e}")
                    self._set_channel_state("failed", str(e))
                    return
                delay = min(2**attempt, 60)
                self._logger.warning(
                    f"[wecom] disconnected ({e}); retry {attempt} in {delay}s"
                )
                self._set_channel_state("connecting", f"reconnecting in {delay}s: {e}")
                self._stop.wait(delay)

    def _set_channel_state(self, state: str, detail: str = "") -> None:
        """Surface connection state so ops can see a dead connection from the
        status endpoint instead of discovering it by missing replies."""
        self.connection_state = state
        self.connection_error = detail
        try:
            if self._sink:
                self._sink(f"{self.name}:__channel__", "channel_state", {"state": state, "error": detail})
        except Exception:  # noqa: BLE001
            pass

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
        self._set_channel_state("connected")
        self._logger.info("[wecom] WebSocket authenticated")
        try:
            while not self._stop.is_set():
                raw = ws.recv(timeout=1.0)
                if raw is None:
                    continue
                msg = json.loads(raw)
                cmd = msg.get("cmd", "")
                keys = sorted(msg.keys())
                if "aibot_msg_callback" in raw or "chatid" in raw:
                    self._logger.info(f"[wecom] frame: {raw[:220]}")
                try:
                    self._dispatch(msg, ws)
                except Exception as e:  # noqa: BLE001
                    self._logger.error(f"[wecom] dispatch failed: {e}; msg={raw[:200]}")
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- inbound -----------------------------------------------------------
    def _dispatch(self, msg: dict[str, Any], ws: Any) -> None:
        """Official aibot long-connection frames:
        {cmd: aibot_msg_callback, headers:{req_id}, body:{msgid, aibotid, chatid,
         chattype, from:{userid}, msgtype, text:{content}, image:{url,aeskey}, ...}}
        {cmd: aibot_event_callback, body:{event:{eventtype}, from, chatid, ...}}
        {headers:{req_id}, errcode, errmsg}  (subscribe/heartbeat responses)
        """
        cmd = msg.get("cmd", "")
        if msg.get("errcode") is not None and not cmd:
            return  # ack/pong frame
        req_id = (msg.get("headers") or {}).get("req_id", "")
        body = msg.get("body") or msg
        msgtype = body.get("msgtype", "text")

        if cmd == "aibot_event_callback" or msgtype == "event":
            event = (body.get("event") or {}).get("eventtype", "")
            if event == "enter_chat":
                self._respond_welcome(ws, req_id)
            return

        sender = (body.get("from") or {}).get("userid") or body.get("sender") or ""
        chat_id = str(body.get("chatid") or body.get("chat_id") or sender)
        if self.allowed_users and sender and sender not in self.allowed_users:
            self._logger.warning(f"[wecom] user {sender} not allowed, dropped")
            return
        text = ""
        images: list[bytes] = []
        if msgtype == "text":
            text = (body.get("text") or {}).get("content", "")
        elif msgtype == "image":
            data = self._download_aibot_image(body.get("image") or {})
            if data:
                images.append(data)
        elif msgtype == "mixed":
            for item in body.get("mixed", {}).get("content", []):
                if item.get("msgtype") == "text":
                    text += (item.get("text") or {}).get("content", "")
                elif item.get("msgtype") == "image":
                    data = self._download_aibot_image(item.get("image") or {})
                    if data:
                        images.append(data)
        elif msgtype == "voice":
            text = (body.get("voice") or {}).get("content", "")  # already transcribed
        # remember req_id for streaming replies keyed by chat
        if req_id:
            with self._stream_lock:
                self._req_ids[chat_id] = req_id
        text = clean_control_chars(text, max_len=100000).strip() if text else ""
        # group chat: only respond when @mentioned is handled by WeCom itself
        if not text and not images:
            return
        if self._msg_cb:
            self._msg_cb(
                InboundMessage(
                    channel=self.name,
                    chat_id=chat_id,
                    user_id=str(sender),
                    text=text,
                    images=images,
                    raw={"req_id": req_id, "msgid": body.get("msgid"), "chattype": body.get("chattype"), "msg": msg},
                )
            )

    def _download_aibot_image(self, img: dict[str, Any]) -> bytes | None:
        """image {url, aeskey}: url valid 5min; AES-256-CBC, IV=aeskey[:16], PKCS7."""
        url, aeskey = img.get("url"), img.get("aeskey")
        if not url:
            return None
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            if aeskey:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                key = aeskey.encode() if isinstance(aeskey, str) and len(aeskey) == 64 else bytes.fromhex(aeskey) if isinstance(aeskey, str) else aeskey
                if len(key) == 64:  # hex string
                    key = bytes.fromhex(aeskey)
                dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
                raw = dec.update(data) + dec.finalize()
                pad = raw[-1]
                if 1 <= pad <= 32:
                    raw = raw[:-pad]
                data = raw
            return data
        except Exception as e:  # noqa: BLE001
            self._logger.warning(f"[wecom] image download failed: {e}")
            return None

    def _respond_welcome(self, ws: Any, req_id: str) -> None:
        self._send(
            {
                "cmd": "aibot_respond_welcome_msg",
                "headers": {"req_id": req_id},
                "body": {"msgtype": "markdown", "markdown": {"content": "hi\n\u6211\u662f agenthub\uff0c\u76f4\u63a5\u53d1\u6d88\u606f\u5373\u53ef\u4e0e opencode \u5bf9\u8bdd\u3002"}},
            }
        )

    # -- outbound ----------------------------------------------------------
    def reply(self, chat_id: str, text: str) -> None:
        for chunk in split_message_text(text, 20000):  # markdown limit 20480 bytes
            self._send(
                {
                    "cmd": "aibot_send_msg",
                    "headers": {"req_id": uuid.uuid4().hex},
                    "body": {"chatid": chat_id, "chat_type": 0, "msgtype": "markdown", "markdown": {"content": chunk}},
                }
            )

    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None:
        if kind == "ack":
            # upstream received the message: open the stream bubble immediately
            # (satisfies low-latency feedback; WeCom has no separate receipt)
            self._stream_ensure(chat_id, "正在处理…")
        elif kind == "delta":
            self._stream_append(chat_id, str(payload.get("text", "")))
        elif kind == "status":
            self._stream_ensure(chat_id, str(payload.get("text", "")))
        elif kind == "tool_start":
            # show agent progress inside the stream bubble so long tool turns
            # don't look stuck
            tool = str(payload.get("tool") or "")
            cur = self._stream_current(chat_id)
            marker = f"\n🔧 {tool}" if tool and (not cur or f"🔧 {tool}" not in cur) else ""
            if marker:
                self._stream_append(chat_id, marker)
        elif kind == "interaction":
            # approval / choice question: close current bubble with a
            # structured render; user replies in chat (M1: text-based choice)
            self._stream_close(chat_id, _interaction_text(payload))
        elif kind == "command_result":
            self.reply(chat_id, _command_result_text(payload))
        elif kind == "final":
            self._stream_close(chat_id, str(payload.get("text", "")))
        elif kind == "permission":
            self.reply(chat_id, _interaction_text(payload))
        # "replace" treated as delta reset for now

    # -- streaming state ---------------------------------------------------
    def _stream_key_states(self, chat_id: str) -> dict:
        with self._stream_lock:
            return self._streams.setdefault(chat_id, {"req_id": uuid.uuid4().hex, "buf": "", "sent": False})

    def _stream_current(self, chat_id: str) -> str:
        with self._stream_lock:
            st = self._streams.get(chat_id)
            return st["buf"] if st else ""

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

    # aibot_respond_msg single-frame limit is 20480 bytes; leave headroom for
    # the JSON envelope and stream metadata (matches the wecom-app markdown cap)
    _STREAM_FINISH_BYTES = 19000

    def _stream_close(self, chat_id: str, final_text: str) -> None:
        st = self._stream_key_states(chat_id)
        text = final_text or st["buf"]
        # Overflow guard: the finish frame carries the FULL text and WeCom
        # rejects frames over ~20KB. dsh-im-verified approach: first chunk
        # closes the bubble, the remainder goes out as separate markdown
        # messages instead of being silently dropped by the gateway.
        chunks = split_message_text(text, self._STREAM_FINISH_BYTES)
        if chunks:
            self._reply_stream(chat_id, st, chunks[0], finished=True)
            overflow = chunks[1:]
        else:
            self._reply_stream(chat_id, st, "", finished=True)
            overflow = []
        with self._stream_lock:
            self._streams.pop(chat_id, None)
        if overflow:
            self._logger.info(
                f"[wecom] finish frame overflow: {len(overflow)} extra chunk(s) sent as markdown"
            )
            for chunk in overflow:
                self.reply(chat_id, chunk)

    def _reply_stream(self, chat_id: str, st: dict, text: str, finished: bool) -> None:
        # official: reuse the CALLBACK's req_id in headers; stream.id identifies the bubble
        req_id = self._req_ids.get(chat_id, "")
        self._send(
            {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {"id": st["req_id"], "finish": finished, "content": text},
                },
            }
        )
        if finished:
            st["sent"] = True
            # proactive follow-ups after the stream need aibot_send_msg
            self._req_ids.pop(chat_id, None)

    def _send(self, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False)
        ws = getattr(self, "_ws", None)
        if ws is None:
            # not connected: buffer so callers/tests can observe pending frames
            self._outbox = getattr(self, "_outbox", [])
            self._outbox.append(raw)
            self._logger.warning("[wecom] send buffered, not connected")
            return
        ws.send(raw)


class _AuthFailure(Exception):
    pass




def _default_logger():
    import logging

    return logging.getLogger("agenthub.wecom")
