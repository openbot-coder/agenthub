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

from .base import IMChannel, InboundMessage, MediaFile, clean_control_chars, split_message_text, _interaction_text, _command_result_text

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
        # media upload acks: req_id -> {event, msg}
        self._pending_acks: dict[str, dict] = {}
        self._ack_lock = threading.Lock()
        # approval card answers: set by runtime, called as
        # cb(request_id, option_id, answered_by) -> None
        self.card_answer_cb: Callable[[str, str, str], None] | None = None

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
                if "aibot_msg_callback" in raw or "chatid" in raw or "aibot_event_callback" in raw:
                    # include event callbacks: card clicks (template_card_event)
                    # carry no chatid and were previously invisible in logs
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
            # ack/pong frame — route to a waiting media-upload _request() if any
            self._deliver_ack(msg)
            return
        req_id = (msg.get("headers") or {}).get("req_id", "")
        body = msg.get("body") or msg
        msgtype = body.get("msgtype", "text")

        if cmd == "aibot_event_callback" or msgtype == "event":
            event = (body.get("event") or {}).get("eventtype", "")
            if event == "enter_chat":
                self._respond_welcome(ws, req_id)
            elif event == "template_card_event":
                self._on_card_click(body, ws, req_id)
            return

        sender = (body.get("from") or {}).get("userid") or body.get("sender") or ""
        chat_id = str(body.get("chatid") or body.get("chat_id") or sender)
        if self.allowed_users and sender and sender not in self.allowed_users:
            self._logger.warning(f"[wecom] user {sender} not allowed, dropped")
            return
        text = ""
        images: list[bytes] = []
        files: list[MediaFile] = []
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
        elif msgtype == "file":
            finfo = body.get("file") or {}
            data = self._download_aibot_image(finfo)
            if data:
                files.append(MediaFile(kind="file", data=data, filename=str(finfo.get("filename") or "file.bin")))
        elif msgtype == "video":
            vinfo = body.get("video") or {}
            data = self._download_aibot_image(vinfo)
            if data:
                files.append(MediaFile(kind="video", data=data, filename=str(vinfo.get("filename") or "video.mp4")))
        # remember req_id for streaming replies keyed by chat
        if req_id:
            with self._stream_lock:
                self._req_ids[chat_id] = req_id
        text = clean_control_chars(text, max_len=100000).strip() if text else ""
        # group chat: only respond when @mentioned is handled by WeCom itself
        if not text and not images and not files:
            return
        if self._msg_cb:
            self._msg_cb(
                InboundMessage(
                    channel=self.name,
                    chat_id=chat_id,
                    user_id=str(sender),
                    text=text,
                    images=images,
                    files=files,
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
            if payload.get("close_stream"):
                # queued follow-up ack: the "正在处理…" bubble from this
                # message's own ack would otherwise hang forever (a queued
                # message never produces a final) — close it out instead.
                self._stream_close(chat_id, str(payload.get("text", "")))
            else:
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
            # approval / choice question: render as an interactive template
            # card (button_interaction); falls back to text if the card send
            # is not possible. The card click callback answers via
            # card_answer_cb; numbered text replies still work.
            try:
                self._send_approval_card(chat_id, payload)
                self._stream_close(chat_id, "🔒 审批请求已发卡片，请点击按钮或回复编号。")
            except Exception as e:  # noqa: BLE001
                self._logger.warning(f"[wecom] approval card send failed: {e}")
                self._stream_close(chat_id, _interaction_text(payload))
        elif kind == "command_result":
            self.reply(chat_id, _command_result_text(payload))
        elif kind == "final":
            self._stream_close(chat_id, str(payload.get("text", "")))
        elif kind == "media":
            path = str(payload.get("path", ""))
            size = int(payload.get("size") or 0)
            try:
                sent = self.send_media(chat_id, path) if path else False
            except Exception as e:  # noqa: BLE001
                self._logger.warning(f"[wecom] media send failed: {e}")
                sent = False
            if not sent:
                kb = size // 1024
                self.reply(chat_id, f"📎 已生成文件（{kb} KB）: {path}")
        elif kind == "permission":
            self.reply(chat_id, _interaction_text(payload))
        # "replace" treated as delta reset for now

    # -- approval cards ------------------------------------------------------
    _BTN_STYLE = {"allow_once": 1, "always_once": 1, "reject_once": 4}  # 1 primary, 4 danger

    def _send_approval_card(self, chat_id: str, payload: dict[str, Any]) -> None:
        """Send a button_interaction template card via aibot_send_msg.

        Button key = '<request_id>|<option_id>' so the click callback can
        route the answer back to the exact pending permission. task_id is
        the request_id (unique per approval, reused by the update call).
        """
        req_id = str(payload.get("request_id") or "")
        opts = payload.get("options") or []
        buttons = [
            {
                "text": str(o.get("name") or o.get("option_id"))[:12],
                "style": self._BTN_STYLE.get(o.get("kind"), 0),
                "key": f"{req_id}|{o.get('option_id')}",
            }
            for o in opts
        ][:6]  # WeCom card cap: 6 buttons
        card = {
            "card_type": "button_interaction",
            "source": {"desc": "agenthub"},
            "main_title": {"title": f"🔐 {str(payload.get('tool') or '审批请求')[:26]}"},
            "sub_title_text": f"agent: {payload.get('agent', '')} · 请求 {req_id[:8]}",
            "button_list": buttons,
            "task_id": req_id.replace("-", "_").replace("@", "_")[:128],
        }
        self._send(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": uuid.uuid4().hex},
                "body": {
                    "chatid": chat_id,
                    "chat_type": 0,
                    "msgtype": "template_card",
                    "template_card": card,
                },
            }
        )

    def _on_card_click(self, body: dict[str, Any], ws: Any, cb_req_id: str) -> None:
        """aibot_event_callback eventtype=template_card_event: route the
        button answer to the pending permission, then update the card."""
        evt = (body.get("event") or {}).get("template_card_event") or {}
        event_key = str(evt.get("event_key") or "")
        sender = str(((body.get("from") or {}).get("userid")) or "")
        self._logger.info(f"[wecom] card click: key={event_key[:60]} by={sender}")
        req_id, _, option_id = event_key.partition("|")
        answered = False
        reason = ""
        if req_id and option_id and self.card_answer_cb:
            try:
                self.card_answer_cb(req_id, option_id, sender)
                answered = True
            except Exception as e:  # noqa: BLE001
                reason = str(e)
                self._logger.error(f"[wecom] card answer failed: {e}")
        # update the card so the user sees the decision was taken
        task_id = str(evt.get("task_id") or "").replace("-", "_").replace("@", "_")[:128]
        if answered:
            title = "✅ 已批准" if "reject" not in option_id else "❌ 已拒绝"
        elif "no pending permission" in reason:
            title = "⚠️ 该审批已失效（已处理、超时或服务重启）"
        else:
            title = "⚠️ 处理失败"
        try:
            self._send(
                {
                    "cmd": "aibot_respond_update_msg",
                    "headers": {"req_id": cb_req_id},
                    "body": {
                        "response_type": "update_template_card",
                        "template_card": {
                            "card_type": "button_interaction",
                            "main_title": {"title": f"{title} · {req_id[:8]}"},
                            "task_id": task_id,
                        },
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            self._logger.warning(f"[wecom] card update failed: {e}")

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

    # -- media: 3-step upload protocol (aibot_upload_media_init/chunk/finish) ----
    UPLOAD_CHUNK_SIZE = 512 * 1024
    MEDIA_MAX_BYTES = 20 * 1024 * 1024

    def _deliver_ack(self, msg: dict) -> None:
        """Route an ack frame to a waiting _request() caller (called from recv loop)."""
        rid = (msg.get("headers") or {}).get("req_id", "")
        with self._ack_lock:
            ev = self._pending_acks.pop(rid, None)
        if ev is not None:
            ev["msg"] = msg
            ev["event"].set()

    def _request(self, cmd: str, body: dict, wait_s: float = 30.0) -> dict | None:
        """Send a frame and wait for its errcode ack (matched by req_id)."""
        rid = uuid.uuid4().hex
        ev = {"event": threading.Event(), "msg": None}
        with self._ack_lock:
            self._pending_acks[rid] = ev
        self._send({"cmd": cmd, "headers": {"req_id": rid}, "body": body})
        if not ev["event"].wait(wait_s):
            with self._ack_lock:
                self._pending_acks.pop(rid, None)
            return None
        return ev["msg"]

    def send_media(self, chat_id: str, path: str) -> bool:
        """Upload a local file and send it as an image/video/voice/file message.

        Returns True when sent. Oversize (>20MB) files are refused (the caller
        should tell the user the server-side path instead).
        """
        import hashlib
        import os as _os

        try:
            size = _os.path.getsize(path)
        except OSError:
            return False
        if size > self.MEDIA_MAX_BYTES:
            self._logger.warning(f"[wecom] media too large, skip upload: {path} ({size}B)")
            return False
        name = _os.path.basename(path)
        ext = _os.path.splitext(name)[1].lower()
        media_type = ("image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                      else "video" if ext in (".mp4", ".mov", ".avi")
                      else "voice" if ext in (".amr", ".mp3", ".wav")
                      else "file")
        with open(path, "rb") as fh:
            data = fh.read()
        md5 = hashlib.md5(data).hexdigest()
        chunks = [data[i:i + self.UPLOAD_CHUNK_SIZE] for i in range(0, len(data), self.UPLOAD_CHUNK_SIZE)] or [b""]

        ack = self._request("aibot_upload_media_init", {
            "filename": name, "type": media_type, "total_size": size,
            "total_chunks": len(chunks), "md5": md5,
        })
        if not ack or ack.get("errcode") not in (0, None):
            self._logger.warning(f"[wecom] upload init failed: {ack}")
            return False
        upload_id = (ack.get("body") or {}).get("upload_id", "")
        if not upload_id:
            self._logger.warning("[wecom] upload init: no upload_id")
            return False
        import base64 as _b64
        for idx, chunk in enumerate(chunks):
            ack = self._request("aibot_upload_media_chunk", {
                "upload_id": upload_id, "chunk_index": idx,
                "total_chunks": len(chunks),
                "base64_data": _b64.b64encode(chunk).decode("ascii"),
            })
            if not ack or ack.get("errcode") not in (0, None):
                self._logger.warning(f"[wecom] upload chunk {idx} failed: {ack}")
                return False
        ack = self._request("aibot_upload_media_finish", {
            "upload_id": upload_id, "filename": name, "media_type": media_type,
        })
        if not ack or ack.get("errcode") not in (0, None):
            self._logger.warning(f"[wecom] upload finish failed: {ack}")
            return False
        media_id = (ack.get("body") or {}).get("media_id", "")
        if not media_id:
            self._logger.warning("[wecom] upload finish: no media_id")
            return False
        self._send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": uuid.uuid4().hex},
            "body": {"chatid": chat_id, "chat_type": 0, "msgtype": media_type,
                     media_type: {"media_id": media_id}},
        })
        return True


class _AuthFailure(Exception):
    pass




def _default_logger():
    import logging

    return logging.getLogger("agenthub.wecom")
