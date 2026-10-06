"""WeCom 自建应用 channel: HTTP callback + qyapi.weixin.qq.com APIs.

Port of dsh-im src/channels/wecom-app. Works in BOTH WeCom client and
WeChat (via 企业微信后台的「微信插件」) — same conversation, both ends.

Protocol facts (from dsh-im docs/企业微信自建应用接入.md):
- callback: GET verify (echostr decrypt) + POST encrypted XML/JSON messages
- APIs: access_token (2h TTL, corp_id+secret), send message, upload media
- errors: 60020 = trusted-IP missing, 41004 = bad secret
- streaming: WeChat side has NO edit API -> always whole-message delivery;
  WeCom client side also whole-message for self-built apps (aibot has
  replyStream, self-built does not)
- http server runs in-thread (stdlib), no external web framework needed

Config (dict):
    corp_id, agent_id, secret, token, encoding_aes_key
    callback_listen_host: str = "127.0.0.1"
    callback_listen_port: int = 30987
    callback_base_url: str = ""        # public base (IP:port or domain)
    api_proxy: str = ""                # optional reverse proxy for qyapi
    allowed_users: list[str] = []
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .base import IMChannel, InboundMessage, clean_control_chars, split_message_text, _interaction_text, _command_result_text

QYAPI = "https://qyapi.weixin.qq.com"


# ---------------------------------------------------------------------------
# WXBizMsgCrypt equivalent (stdlib) — SHA1 sig + AES-CBC msg encryption
# ---------------------------------------------------------------------------

class WxBizError(Exception):
    pass


class WxMsgCrypt:
    """企业微信消息加解密（sha1 签名 + AES-256-CBC，PKCS7，base64）."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        key = base64.b64decode(encoding_aes_key + "=")
        if len(key) != 32:
            raise WxBizError("EncodingAESKey invalid")
        self.key = key

    def _sign(self, timestamp: str, nonce: str, encrypt: str) -> str:
        raw = "".join(sorted([self.token, timestamp, nonce, encrypt]))
        return hashlib.sha1(raw.encode()).hexdigest()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """Decrypt echostr for URL verification; raises WxBizError on mismatch."""
        if self._sign(timestamp, nonce, echostr) != msg_signature:
            raise WxBizError("signature mismatch (verify URL)")
        plain = self._decrypt(echostr)
        return plain

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> str:
        if self._sign(timestamp, nonce, encrypt) != msg_signature:
            raise WxBizError("signature mismatch (msg)")
        return self._decrypt(encrypt)

    def encrypt_msg(self, plain: str, nonce: str, timestamp: str | None = None) -> str:
        import os
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        ts = timestamp or str(int(time.time()))
        rnd = os.urandom(16)
        msg_bytes = plain.encode()
        # 16B rnd + 4B network-order len + msg + corp_id + PKCS7 to 32-block
        raw = (
            rnd
            + len(msg_bytes).to_bytes(4, "big")
            + msg_bytes
            + self.corp_id.encode()
        )
        pad = 32 - (len(raw) % 32)
        raw += bytes([pad]) * pad
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]))
        enc = cipher.encryptor()
        encrypted = enc.update(raw) + enc.finalize()
        b64 = base64.b64encode(encrypted).decode()
        sig = self._sign(ts, nonce, b64)
        return json.dumps({"encrypt": b64, "msgsignature": sig, "timestamp": ts, "nonce": nonce})

    def _decrypt(self, b64: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        data = base64.b64decode(b64)
        if len(data) % 16 or len(data) < 32:
            raise WxBizError("bad encrypted payload length")
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]))
        dec = cipher.decryptor()
        raw = dec.update(data) + dec.finalize()
        pad = raw[-1]
        if not 1 <= pad <= 32:
            raise WxBizError("bad PKCS7 padding")
        raw = raw[:-pad]
        msg_len = int.from_bytes(raw[16:20], "big")
        corp = raw[20 + msg_len :].decode()
        if corp != self.corp_id:
            raise WxBizError(f"corp_id mismatch: {corp}")
        return raw[20 : 20 + msg_len].decode()


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------

class WecomAppChannel:
    name = "wecom-app"

    def __init__(self, config: dict[str, Any], logger: Any = None):
        self.corp_id = config["corp_id"]
        self.agent_id = str(config["agent_id"])
        self.secret = config["secret"]
        self.token = config["token"]
        self.aes_key = config["encoding_aes_key"]
        self.host = config.get("callback_listen_host", "127.0.0.1")
        self.port = int(config.get("callback_listen_port", 30987))
        self.api_base = (config.get("api_proxy") or QYAPI).rstrip("/")
        self.allowed_users = set(config.get("allowed_users") or [])

        self.crypt = WxMsgCrypt(self.token, self.aes_key, self.corp_id)
        self._msg_cb: Callable[[InboundMessage], None] | None = None
        self._logger = logger or _log()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._access_token: str = ""
        self._token_exp: float = 0
        self._token_lock = threading.Lock()
        self._external = self  # for handler closure

    # -- IMChannel ---------------------------------------------------------
    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ms, ts, nc = q.get("msg_signature", [""])[0], q.get("timestamp", [""])[0], q.get("nonce", [""])[0]
                echo = q.get("echostr", [""])[0]
                try:
                    plain = outer.crypt.verify_url(ms, ts, nc, echo)
                    self._respond(200, plain.encode())
                except WxBizError as e:
                    outer._logger.warning(f"[wecom-app] URL verify failed: {e}")
                    self._respond(403, b"verify failed")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ms = q.get("msg_signature", [""])[0]
                ts = q.get("timestamp", [""])[0]
                nc = q.get("nonce", [""])[0]
                self._respond(200, b"success")  # ack fast, process async
                try:
                    encrypt = _extract_encrypt(body)
                    plain_xml = outer.crypt.decrypt_msg(ms, ts, nc, encrypt)
                    outer._on_plain_xml(plain_xml)
                except Exception as e:  # noqa: BLE001
                    outer._logger.error(f"[wecom-app] msg processing failed: {e}")

            def _respond(self, code: int, data: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="wecom-app-callback", daemon=True)
        self._thread.start()
        self._logger.info(f"[wecom-app] callback server on {self.host}:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.shutdown()

    def on_message(self, cb: Callable[[InboundMessage], None]) -> None:
        self._msg_cb = cb

    def set_event_sink(self, sink: Callable[[str, str, dict], None]) -> None:
        self._sink = sink

    # -- inbound -----------------------------------------------------------
    def _on_plain_xml(self, xml_text: str) -> None:
        root = ET.fromstring(xml_text)
        get = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
        sender = get("FromUserName")
        if self.allowed_users and sender not in self.allowed_users:
            return
        msgtype = get("MsgType")
        text, images = "", []
        if msgtype == "text":
            text = get("Content")
        elif msgtype == "image":
            data = self._download_media(get("PicUrl"))
            if data:
                images.append(data)
        elif msgtype in ("voice", "video", "location"):
            text = f"[{msgtype} message omitted]"  # M1.5 scope: text/image only
        text = clean_control_chars(text, max_len=100000).strip()
        if not text and not images:
            return
        if self._msg_cb:
            self._msg_cb(
                InboundMessage(
                    channel=self.name,
                    chat_id=sender,  # self-built app: 1:1 by user
                    user_id=sender,
                    text=text,
                    images=images,
                    raw={"msg_id": get("MsgID"), "msg_type": msgtype},
                )
            )

    # -- outbound ----------------------------------------------------------
    def get_access_token(self) -> str:
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_exp:
                return self._access_token
            url = (
                f"{self.api_base}/cgi-bin/gettoken"
                f"?corpid={urllib.parse.quote(self.corp_id)}&corpsecret={urllib.parse.quote(self.secret)}"
            )
            with urllib.request.urlopen(url, timeout=10) as resp:
                d = json.loads(resp.read())
            if d.get("errcode"):
                raise RuntimeError(f"gettoken failed: {d}")
            self._access_token = d["access_token"]
            self._token_exp = time.monotonic() + int(d.get("expires_in", 7200)) - 120
            return self._access_token

    def reply(self, chat_id: str, text: str) -> None:
        for chunk in split_message_text(text, 2000):  # self-built markdown limit
            self._api(
                "/cgi-bin/message/send",
                {
                    "touser": chat_id,
                    "msgtype": "markdown",
                    "agentid": self.agent_id,
                    "markdown": {"content": chunk},
                },
            )

    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None:
        # Self-built apps have no edit API: coalesce deltas, deliver on final.
        if kind == "delta":
            return  # accumulate in session layer; final carries full text
        if kind == "final":
            self.reply(chat_id, str(payload.get("text", "")))
        elif kind == "status":
            pass  # no transient surface on this channel
        elif kind in ("interaction", "permission"):
            self.reply(chat_id, _interaction_text(payload))
        elif kind == "command_result":
            self.reply(chat_id, _command_result_text(payload))

    # -- helpers -----------------------------------------------------------
    def _api(self, path: str, payload: dict) -> dict:
        url = f"{self.api_base}{path}?access_token={self.get_access_token()}"
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        if d.get("errcode") not in (0, None):
            raise RuntimeError(f"{path} -> {d}")
        return d

    def _download_media(self, url: str) -> bytes | None:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            self._logger.warning(f"[wecom-app] media download failed: {e}")
            return None


def _extract_encrypt(body: bytes) -> str:
    """Callback body may be XML (<Encrypt>) or JSON ({"encrypt": ...})."""
    text = body.decode("utf-8", "replace").lstrip()
    if text.startswith("<"):
        root = ET.fromstring(text)
        return (root.findtext("Encrypt") or "").strip()
    return (json.loads(text)).get("encrypt", "")


def _permission_text(payload: dict[str, Any]) -> str:
    title = payload.get("title") or payload.get("tool", "operation")
    return f"⚠️ 权限请求：{title}\n回复「允许」执行；其他回复视为拒绝。"


def _log():
    import logging

    return logging.getLogger("agenthub.wecom-app")
