"""Tests for the three IM channels (mock transports, no network)."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.channels.base import clean_control_chars, split_message_text  # noqa: E402
from agenthub.channels.wecom import WecomAibotChannel  # noqa: E402
from agenthub.channels.wecom_app import WxMsgCrypt  # noqa: E402
from agenthub.channels.weixin import WeixinChannel  # noqa: E402


# ---------------------------------------------------------------------------
# base helpers
# ---------------------------------------------------------------------------

def test_split_prefers_newline():
    assert split_message_text("ab\ncd", 4) == ["ab", "cd"]


def test_split_hard_cut_utf8_safe():
    parts = split_message_text("你好世界", 4)  # 4 bytes = 1 chinese char + part
    assert all(p.encode("utf-8") for p in parts)
    assert "".join(parts) == "你好世界"


def test_clean_control_chars():
    assert clean_control_chars("a\u200bb\u0000c", 100) == "abc"


# ---------------------------------------------------------------------------
# wecom aibot
# ---------------------------------------------------------------------------

class MockWS:
    def __init__(self):
        self.sent = []
        self.incoming = []

    def connect(self):
        pass

    def recv(self, timeout=1.0):
        if self.incoming:
            return self.incoming.pop(0)
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        pass


def _make_wecom():
    ws = MockWS()
    ch = WecomAibotChannel(
        {"bot_id": "b", "secret": "s", "allowed_users": ["u1"]},
        ws_factory=lambda cfg: ws,
    )
    return ch, ws


def test_wecom_dispatch_text_and_permission():
    ch, ws = _make_wecom()
    got = []
    ch.on_message(got.append)
    frame = {"cmd": "aibot_msg_callback", "headers": {"req_id": "r1"},
             "body": {"msgid": "m1", "chatid": "c1", "chattype": "single", "msgtype": "text",
                      "from": {"userid": "u1"}, "text": {"content": "hello\u200b"}}}
    ch._dispatch(frame, ws)
    assert len(got) == 1 and got[0].text == "hello" and got[0].chat_id == "c1"
    assert ch._req_ids["c1"][0] == "r1"  # captured for the streaming reply


def test_wecom_allowed_users_drops_others():
    ch, ws = _make_wecom()
    got = []
    ch.on_message(got.append)
    ch._dispatch({"cmd": "aibot_msg_callback", "headers": {"req_id": "r2"}, "body": {"msgtype": "text", "from": {"userid": "u2"}, "chatid": "c", "text": {"content": "x"}}}, ws)
    assert got == []


def test_wecom_streaming_delta_coalesce_and_final():
    ch, ws = _make_wecom()
    ch._ws = ws  # pretend connected (stream sends go through the socket)
    ch._req_ids["c1"] = ("req-9", __import__("time").time())  # fresh inbound callback
    ch._req_ids["stale"] = ("req-old", __import__("time").time() - 600)  # expired callback
    ch.push_event("c1", "delta", {"text": "hello "})
    ch.push_event("c1", "delta", {"text": "world"})
    ch.push_event("c1", "final", {"text": "hello world"})
    replies = [m for m in ws.sent if m["cmd"] == "aibot_respond_msg"]
    assert replies and replies[-1]["body"]["stream"]["finish"] is True
    assert replies[-1]["body"]["stream"]["content"] == "hello world"
    assert replies[-1]["headers"]["req_id"] == "req-9"  # must reuse callback req_id
    assert len({m["body"]["stream"]["id"] for m in replies}) == 1  # same bubble


# ---------------------------------------------------------------------------
# wecom-app crypto round-trip
# ---------------------------------------------------------------------------

def test_wxbiz_crypt_roundtrip():
    import os

    key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    crypt = WxMsgCrypt("tok", key, "corp123")
    nonce, ts = "n1", "1700000000"
    enc = crypt.encrypt_msg("<xml>hi</xml>", nonce, ts)
    payload = json.loads(enc)
    assert crypt._sign(ts, nonce, payload["encrypt"]) == payload["msgsignature"]
    plain = crypt._decrypt(payload["encrypt"])
    assert plain == "<xml>hi</xml>"


# ---------------------------------------------------------------------------
# weixin
# ---------------------------------------------------------------------------

class MockHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.posts = []

    def post(self, endpoint, body, token, timeout_ms):
        self.posts.append((endpoint, body, token))
        return self.responses.get(endpoint, {})

    def get(self, endpoint, timeout_ms):
        return self.responses.get(endpoint, {})


def test_weixin_login_flow():
    http = MockHTTP(
        {
            "ilink/bot/get_bot_qrcode": {"qrcode": "qr1"},
            "ilink/bot/get_qrcode_status?qrcode=qr1": {"status": "confirmed", "token": "tk"},
        }
    )
    ch = WeixinChannel({}, http_post=http.post, http_get=http.get)
    qr = ch.start_login()
    assert qr["qrcode"] == "qr1"
    token = ch.poll_login("qr1", timeout_s=3)
    assert token == "tk" and ch._token == "tk"


def test_weixin_receive_and_reply():
    http = MockHTTP({"ilink/bot/getupdates": {"msgs": [], "get_updates_buf": ""}})
    ch = WeixinChannel({"token": "tk"}, http_post=http.post, http_get=http.get)
    got = []
    ch.on_message(got.append)
    ch._handle({"from_user_id": "wxid_1", "to_user_id": "bot", "msg_type": "text", "text": {"content": "  hi\u200b "}})
    assert got[0].text == "hi" and got[0].chat_id == "wxid_1"

    ch.reply("wxid_1", "answer")
    ep, body, token = http.posts[-1]
    assert ep == "ilink/bot/sendmessage" and token == "tk"
    assert body["text"]["content"] == "answer"


def test_weixin_no_edit_deltas_dropped_final_sent():
    http = MockHTTP({})
    ch = WeixinChannel({"token": "tk"}, http_post=http.post, http_get=http.get)
    ch.push_event("u", "delta", {"text": "partial"})
    assert not http.posts
    ch.push_event("u", "final", {"text": "full text"})
    assert http.posts[-1][1]["text"]["content"] == "full text"


def test_wecom_stream_falls_back_when_callback_req_stale():
    ch, ws = _make_wecom()
    ch._ws = ws
    ch._req_ids["c1"] = ("req-old", __import__("time").time() - 600)  # beyond TTL
    ch.push_event("c1", "final", {"text": "late answer"})
    respond = [m for m in ws.sent if m["cmd"] == "aibot_respond_msg"]
    send = [m for m in ws.sent if m["cmd"] == "aibot_send_msg"]
    assert respond == []  # stale req_id must NOT be reused
    assert send and "late answer" in json.dumps(send[-1])
