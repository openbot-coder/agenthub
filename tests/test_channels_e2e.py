"""End-to-end wiring test: IM channel -> SessionManager -> mock ACP agent -> channel reply.

Runs each channel in-process with mock transports, verifies the full loop:
inbound text -> prompt -> delta/final events -> outbound reply/permission text.
This proves the wiring; real-gateway verification (M1.5 真机) needs credentials.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.session import AgentSpec, SessionManager  # noqa: E402
from agenthub.channels.wecom import WecomAibotChannel  # noqa: E402
from agenthub.channels.wecom_app import WecomAppChannel, WxMsgCrypt  # noqa: E402
from agenthub.channels.weixin import WeixinChannel  # noqa: E402

MOCK = os.path.join(os.path.dirname(__file__), "fixtures", "mock_acp_agent.py")


def make_hub(agents=None) -> SessionManager:
    agents = agents or {"mock": AgentSpec(name="mock", cmd=[sys.executable, MOCK, "--chunks", "3"])}
    return SessionManager(agents=agents, transport="ndjson")


# ---------------------------------------------------------------------------
# wecom aibot e2e (mock WS transport)
# ---------------------------------------------------------------------------

class MockWS:
    def __init__(self):
        self.sent = []
        self.incoming: list[str] = []
        self._ev = threading.Event()

    def connect(self):
        pass

    def recv(self, timeout=1.0):
        if self.incoming:
            return self.incoming.pop(0)
        return None

    def send(self, raw):
        self.sent.append(raw)

    def close(self):
        pass

    def push_inbound(self, obj: dict):
        self.incoming.append(_json(obj))


def _json(o):
    import json
    return json.dumps(o, ensure_ascii=False)


def test_e2e_wecom_aibot_text_loop():
    hub = make_hub()
    ws = MockWS()
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s"}, ws_factory=lambda cfg: ws)
    ch.on_message(lambda m: hub.prompt(m.channel, m.chat_id, m.text))
    hub.set_event_sink(lambda key, kind, payload: ch.push_event(key.split(":", 1)[1], kind, payload))
    # inbound frame from user u1 in chat c1
    ch._dispatch(
        {"frame_id": "f1", "body": {"msgtype": "text", "from": {"userid": "u1"}, "chatid": "c1", "text": {"content": "帮我看看登录 bug"}}},
        ws,
    )
    # wait for the turn to finish and events to flow
    frames = ws.sent + getattr(ch, "_outbox", [])
    deadline = time.time() + 10
    while time.time() < deadline:
        frames = ws.sent + getattr(ch, "_outbox", [])
        if any('"finished": true' in s for s in frames):
            break
        time.sleep(0.05)
    assert any('"finished": true' in s for s in frames), f"no final frame; frames={frames}"
    assert any("chunk" in s for s in frames), f"no delta frames; frames={frames}"
    b = hub.get_binding("wecom", "c1")
    assert b is not None and b.agent == "mock"
    hub.stop()


def test_e2e_wecom_aibot_image_inbound():
    import base64
    hub = make_hub()
    ws = MockWS()
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s"}, ws_factory=lambda cfg: ws)
    got = queue.Queue()
    ch.on_message(got.put)
    png = base64.b64encode(b"\x89PNG fake").decode()
    ch._dispatch(
        {"frame_id": "f2", "body": {"msgtype": "image", "from": {"userid": "u1"}, "chatid": "c1", "image": {"base64": png}}},
        ws,
    )
    m = got.get(timeout=5)
    assert m.images and m.images[0].startswith(b"\x89PNG")
    hub.stop()


# ---------------------------------------------------------------------------
# wecom-app e2e (real local HTTP server + real crypt, mock qyapi)
# ---------------------------------------------------------------------------

def test_e2e_wecom_app_http_callback_loop(tmp_dir="/tmp/agenthub_wecom_app_test"):
    os.makedirs(tmp_dir, exist_ok=True)
    # crypt material
    import base64 as b64mod

    key = b64mod.b64encode(os.urandom(32)).decode().rstrip("=")
    corp, token = "corp_e2e", "tok_e2e"
    crypt = WxMsgCrypt(token, key, corp)

    hub = make_hub()
    ch = WecomAppChannel(
        {
            "corp_id": corp,
            "agent_id": "1000002",
            "secret": "s",
            "token": token,
            "encoding_aes_key": key,
            "callback_listen_host": "127.0.0.1",
            "callback_listen_port": 30987,
            "allowed_users": ["wxuser_1"],
        }
    )
    ch.on_message(lambda m: hub.prompt(m.channel, m.chat_id, m.text))
    replies: list[tuple[str, str]] = []
    ch.reply = lambda chat_id, text: replies.append((chat_id, text))  # stub qyapi send
    hub.set_event_sink(lambda key, kind, payload: ch.push_event(key.split(":", 1)[1], kind, payload))
    ch.start()
    time.sleep(0.2)

    import urllib.parse
    import urllib.request

    # 1) URL verification (GET echostr)
    nonce, ts, echo = "n1", "1700000001", "echo-verify-me"
    enc_echo = crypt.encrypt_msg(echo, nonce, ts)  # encrypt_msg returns JSON with encrypt+sig
    payload = _json_load(enc_echo)
    qs = urllib.parse.urlencode(
        {"msg_signature": payload["msgsignature"], "timestamp": payload["timestamp"], "nonce": nonce, "echostr": payload["encrypt"]}
    )
    with urllib.request.urlopen(f"http://127.0.0.1:30987/?{qs}", timeout=5) as resp:
        assert resp.read().decode() == echo

    # 2) encrypted text message POST -> full loop -> (stubbed) reply
    xml_plain = (
        "<xml><ToUserName><![CDATA[corp_e2e]]></ToUserName>"
        "<FromUserName><![CDATA[wxuser_1]]></FromUserName>"
        "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[跑一遍e2e]]></Content>"
        "<MsgId><![CDATA[10001]]></MsgId></xml>"
    )
    enc = _json_load(crypt.encrypt_msg(xml_plain, nonce, str(int(ts) + 1)))
    body = f"<xml><Encrypt><![CDATA[{enc['encrypt']}]]></Encrypt></xml>"
    qs2 = urllib.parse.urlencode(
        {"msg_signature": enc["msgsignature"], "timestamp": enc["timestamp"], "nonce": nonce}
    )
    req = urllib.request.Request(
        f"http://127.0.0.1:30987/?{qs2}", data=body.encode(), headers={"Content-Type": "text/xml"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.read() == b"success"

    # wait for turn + final event -> stubbed reply
    deadline = time.time() + 10
    while time.time() < deadline and not replies:
        time.sleep(0.05)
    assert replies, "no reply produced"
    chat_id, text = replies[0]
    assert chat_id == "wxuser_1" and "chunk" in text
    b = hub.get_binding("wecom-app", "wxuser_1")
    assert b is not None
    ch.stop()
    hub.stop()


def _json_load(s):
    import json
    return json.loads(s)


# ---------------------------------------------------------------------------
# weixin e2e (mock HTTP: QR login -> long-poll inbound -> sendmessage outbound)
# ---------------------------------------------------------------------------

class FakeWeixinServer:
    def __init__(self):
        self.updates_buf = ""
        self.inbox: list[dict] = []
        self.sent: list[dict] = []
        self.polls = 0

    def post(self, endpoint, body, token, timeout_ms):
        if endpoint == "ilink/bot/get_bot_qrcode":
            return {"qrcode": "qr_e2e"}
        if endpoint == "ilink/bot/getupdates":
            self.polls += 1
            msgs, self.inbox = self.inbox, []
            return {"msgs": msgs, "get_updates_buf": "cursor-%d" % self.polls}
        if endpoint == "ilink/bot/sendmessage":
            self.sent.append(body)
            return {"errcode": 0}
        return {}

    def get(self, endpoint, timeout_ms):
        return {"status": "confirmed", "token": "tok_e2e"}


def test_e2e_weixin_login_poll_reply():
    hub = make_hub()
    fake = FakeWeixinServer()
    ch = WeixinChannel({"poll_timeout_ms": 500}, http_post=fake.post, http_get=fake.get)
    # login
    qr = ch.start_login()
    assert qr["qrcode"] == "qr_e2e"
    assert ch.poll_login("qr_e2e", timeout_s=3) == "tok_e2e"
    # wire hub
    ch.on_message(lambda m: hub.prompt(m.channel, m.chat_id, m.text))
    hub.set_event_sink(lambda key, kind, payload: ch.push_event(key.split(":", 1)[1], kind, payload))
    ch.start()
    # inject inbound message into the poll loop
    fake.inbox.append({"from_user_id": "wx_friend", "msg_type": "text", "text": {"content": "微信侧问一下"}})
    deadline = time.time() + 10
    while time.time() < deadline and not fake.sent:
        time.sleep(0.05)
    assert fake.sent, "no sendmessage observed"
    assert fake.sent[0]["to_user_id"] == "wx_friend"
    assert "chunk" in fake.sent[0]["text"]["content"]
    b = hub.get_binding("weixin", "wx_friend")
    assert b is not None and b.agent == "mock"
    ch.stop()
    hub.stop()
