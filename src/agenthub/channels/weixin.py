"""WeChat 智能机器人 channel: QR-scan login + getupdates long-polling.

Port of dsh-im src/channels/weixin (API surface only, no protocol hacks):
- login: POST /ilink/bot/get_bot_qrcode?bot_type=...  -> {qrcode,...}
         GET  /ilink/bot/get_qrcode_status?qrcode=...  (long poll, optional verify_code)
         -> token on success
- receive: POST /ilink/bot/getupdates {get_updates_buf}  (long poll; timeout
           returns empty msgs, NOT an error)
- send:    POST /ilink/bot/sendmessage  (whole-text only, no edit API)
- base:    https://ilinkai.weixin.qq.com/

Config (dict):
    token: str = ""            # existing login token (skip QR if set)
    bot_type: str = ""         # passed to get_bot_qrcode
    base_url: str = default
    poll_timeout_ms: int = 35000
    allowed_users: list[str] = []

Login flow: call start_login() to get a QR for the user to scan with
WeChat; poll_login() yields status; on success the token is stored via
token_provider (persist it — scanning is manual).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .base import IMChannel, InboundMessage, clean_control_chars, split_message_text, _interaction_text, _command_result_text

DEFAULT_BASE = "https://ilinkai.weixin.qq.com/"
DEFAULT_POLL_TIMEOUT_MS = 35_000


class WeixinApiError(Exception):
    def __init__(self, code: str, message: str, provider: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.provider = provider


class WeixinChannel:
    name = "weixin"

    def __init__(
        self,
        config: dict[str, Any],
        http_post: Callable[[str, dict, str | None, int], dict] | None = None,
        http_get: Callable[[str, int], dict] | None = None,
        token_persist: Callable[[str], None] | None = None,
        logger: Any = None,
    ):
        self.base_url = (config.get("base_url") or DEFAULT_BASE).rstrip("/") + "/"
        self.bot_type = config.get("bot_type", "")
        self.poll_timeout_ms = int(config.get("poll_timeout_ms", DEFAULT_POLL_TIMEOUT_MS))
        self.allowed_users = set(config.get("allowed_users") or [])
        self._token: str = config.get("token", "")
        self._get_updates_buf = ""
        self._http_post = http_post or self._default_post
        self._http_get = http_get or self._default_get
        self._token_persist = token_persist
        self._logger = logger or _log()
        self._msg_cb: Callable[[InboundMessage], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

    # -- IMChannel ---------------------------------------------------------
    def start(self) -> None:
        if not self._token:
            raise WeixinApiError("no-token", "call start_login()/finish QR scan first, or set config token")
        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="weixin-poll", daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def on_message(self, cb: Callable[[InboundMessage], None]) -> None:
        self._msg_cb = cb

    def set_event_sink(self, sink: Callable[[str, str, dict], None]) -> None:
        self._sink = sink

    # -- login -------------------------------------------------------------
    def start_login(self, local_tokens: list[str] | None = None) -> dict[str, str]:
        """Request a login QR; returns {qrcode, qrcode_url}. User must scan."""
        body: dict[str, Any] = {"local_token_list": local_tokens or []}
        if self.bot_type:
            resp = self._http_post(f"ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(self.bot_type)}", body, None, 10_000)
        else:
            resp = self._http_post("ilink/bot/get_bot_qrcode", body, None, 10_000)
        qrcode = resp.get("qrcode")
        if not qrcode:
            raise WeixinApiError("invalid-qr", "微信服务没有返回二维码令牌", resp)
        return {"qrcode": qrcode, "qrcode_url": resp.get("qrcode_img_content", "")}

    def poll_login(self, qrcode: str, verify_code: str = "", timeout_s: float = 120) -> str:
        """Long-poll QR status until confirmed; returns the bot token."""
        deadline = time.monotonic() + timeout_s
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}"
        if verify_code:
            endpoint += f"&verify_code={urllib.parse.quote(verify_code)}"
        while time.monotonic() < deadline and not self._stop.is_set():
            resp = self._http_get(endpoint, 40_000)
            status = resp.get("status")
            if status == "confirmed" or resp.get("token"):
                self._token = resp["token"]
                if self._token_persist:
                    self._token_persist(self._token)
                self._logger.info("[weixin] login confirmed")
                return self._token
            if status in ("expired", "rejected"):
                raise WeixinApiError(f"login-{status}", f"扫码登录{ '过期' if status=='expired' else '被拒绝' }")
            time.sleep(1.0)
        raise WeixinApiError("login-timeout", "扫码登录超时")

    # -- receive -----------------------------------------------------------
    def _poll_loop(self) -> None:
        self._logger.info("[weixin] long-poll started")
        while not self._stop.is_set():
            try:
                resp = self._http_post(
                    "ilink/bot/getupdates",
                    {"get_updates_buf": self._get_updates_buf, "base_info": {}},
                    self._token,
                    self.poll_timeout_ms,
                )
                msgs = resp.get("msgs") or []
                self._get_updates_buf = resp.get("get_updates_buf", self._get_updates_buf)
                for m in msgs:
                    self._handle(m)
            except Exception as e:  # noqa: BLE001 - keep polling on transient errors
                self._logger.warning(f"[weixin] poll error: {e}; backoff 3s")
                self._stop.wait(3)

    def _handle(self, msg: dict[str, Any]) -> None:
        sender = str(msg.get("from_user_id") or msg.get("sender") or "")
        chat_id = sender  # 1:1 DM: peer = sender
        if self.allowed_users and sender not in self.allowed_users:
            return
        text = ""
        msgtype = msg.get("msg_type", "")
        if msgtype in ("text", 1, "1"):
            text = msg.get("text", {}).get("content", "") if isinstance(msg.get("text"), dict) else str(msg.get("content", ""))
        text = clean_control_chars(text, max_len=100000).strip()
        if not text:
            return
        if self._msg_cb:
            self._msg_cb(InboundMessage(channel=self.name, chat_id=chat_id, user_id=sender, text=text, raw={"msg": msg}))

    # -- outbound ----------------------------------------------------------
    def reply(self, chat_id: str, text: str) -> None:
        for chunk in split_message_text(text, 2000):
            self._http_post(
                "ilink/bot/sendmessage",
                {"to_user_id": chat_id, "msg_type": "text", "text": {"content": chunk}},
                self._token,
                15_000,
            )

    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None:
        # No edit API: deltas coalesce away, only final text is delivered.
        if kind == "final":
            self.reply(chat_id, str(payload.get("text", "")))
        elif kind in ("interaction", "permission"):
            self.reply(chat_id, _interaction_text(payload))
        elif kind == "command_result":
            self.reply(chat_id, _command_result_text(payload))

    # -- transport defaults ------------------------------------------------
    def _default_post(self, endpoint: str, body: dict, token: str | None, timeout_ms: int) -> dict:
        url = self.base_url + endpoint
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000 + 5) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise WeixinApiError("http-error", f"{e.code} on {endpoint}", e.read()[:200]) from e
        except urllib.error.URLError as e:
            if "timed out" in str(e.reason):
                return {}  # long-poll timeout: empty tick
            raise WeixinApiError("network", str(e)) from e

    def _default_get(self, endpoint: str, timeout_ms: int) -> dict:
        url = self.base_url + endpoint
        try:
            with urllib.request.urlopen(url, timeout=timeout_ms / 1000 + 5) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise WeixinApiError("http-error", f"{e.code} on {endpoint}") from e


def _permission_text(payload: dict[str, Any]) -> str:
    title = payload.get("title") or payload.get("tool", "operation")
    return f"⚠️ 权限请求：{title}\n回复「允许」执行；其他回复视为拒绝。"


def _log():
    import logging

    return logging.getLogger("agenthub.weixin")
