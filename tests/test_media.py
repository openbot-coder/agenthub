"""Media send/receive support (images, video, audio, files).

Inbound: wecom file/video frames download+decrypt into InboundMessage.files;
session layer stores them under media_in/ and tells the agent the path.

Outbound: after each turn, the session layer diffs the agent's workspace for
new files ("media sentinel") and emits "media" events; channels upload and
send them (wecom uses the 3-step aibot upload protocol).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid

import pytest

from agenthub.channels.base import InboundMessage
from agenthub.channels.wecom import WecomAibotChannel


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _channel(ws=None) -> WecomAibotChannel:
    ch = WecomAibotChannel(
        {"bot_id": "b", "secret": "s"},
        ws_factory=lambda cfg: ws,
    )
    return ch


class FakeWS:
    """Records sent frames; feed acks via pop_sent()."""

    def __init__(self):
        self.sent: list[dict] = []
        self.lock = threading.Lock()

    def connect(self):
        pass

    def send(self, raw: str):
        with self.lock:
            self.sent.append(json.loads(raw))

    def recv(self, timeout=1.0):
        time.sleep(0.01)
        return None

    def close(self):
        pass


def _make_media_file(tmp_path, name="out.png", size=600):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG" + os.urandom(size))
    return p


# ---------------------------------------------------------------------------
# inbound: file / video frames
# ---------------------------------------------------------------------------

def test_inbound_file_frame_downloaded_and_attached(tmp_path, monkeypatch):
    ch = _channel()
    payload = b"PDFDATA" * 10
    monkeypatch.setattr(ch, "_download_aibot_image", lambda d: payload)
    got: list[InboundMessage] = []
    ch.on_message(lambda m: got.append(m))
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "chatid": "c1",
            "chattype": 0,
            "msgid": "m1",
            "from": {"userid": "u1"},
            "msgtype": "file",
            "file": {"url": "https://x/a", "aeskey": "k", "filename": "doc.pdf"},
        },
    }
    ch._dispatch(frame, None)
    assert len(got) == 1
    m = got[0]
    assert len(m.files) == 1
    assert m.files[0].data == payload
    assert m.files[0].filename == "doc.pdf"
    assert m.files[0].kind == "file"


def test_inbound_video_frame_attached(tmp_path, monkeypatch):
    ch = _channel()
    monkeypatch.setattr(ch, "_download_aibot_image", lambda d: b"MP4")
    got: list[InboundMessage] = []
    ch.on_message(got.append)
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r2"},
        "body": {
            "chatid": "c1", "chattype": 0, "msgid": "m2",
            "from": {"userid": "u1"},
            "msgtype": "video",
            "video": {"url": "https://x/v", "aeskey": "k"},
        },
    }
    ch._dispatch(frame, None)
    assert got[0].files[0].kind == "video"


def test_inbound_voice_still_transcribed_text():
    ch = _channel()
    got: list[InboundMessage] = []
    ch.on_message(got.append)
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r3"},
        "body": {
            "chatid": "c1", "chattype": 0, "msgid": "m3",
            "from": {"userid": "u1"},
            "msgtype": "voice",
            "voice": {"content": "帮我查天气"},
        },
    }
    ch._dispatch(frame, None)
    assert got[0].text == "帮我查天气"
    assert got[0].files == []


# ---------------------------------------------------------------------------
# outbound: wecom 3-step upload + send
# ---------------------------------------------------------------------------

def test_wecom_send_media_upload_protocol(tmp_path):
    ws = FakeWS()
    ch = _channel(ws)
    ch._ws = ws  # pretend connected
    f = _make_media_file(tmp_path)

    # ack pump: respond to each upload frame with proper ack
    def pump():
        deadline = time.time() + 5
        while time.time() < deadline:
            for fr in list(ws.sent):
                cmd = fr.get("cmd", "")
                if cmd.startswith("aibot_upload_media"):
                    rid = fr["headers"]["req_id"]
                    if cmd.endswith("init"):
                        body = {"errcode": 0, "upload_id": "UP1"}
                    elif cmd.endswith("finish"):
                        body = {"errcode": 0, "media_id": "MEDIA1"}
                    else:
                        body = {"errcode": 0}
                    ch._deliver_ack({"cmd": "", "headers": {"req_id": rid},
                                     "errcode": 0, "body": body, **body})
                    with ws.lock:
                        ws.sent = [x for x in ws.sent if x is not fr]
            time.sleep(0.02)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    ok = ch.send_media("c1", str(f))
    t.join(timeout=2)

    # pump consumed upload frames; keep a global record via channel outbox trick:
    # re-run protocol with an ack-pump that records commands before consuming
    class RecordingWS(FakeWS):
        def __init__(self):
            super().__init__()
            self.all_cmds: list[str] = []
            self.lock2 = threading.Lock()

        def send(self, raw: str):
            fr = json.loads(raw)
            with self.lock2:
                self.all_cmds.append(fr.get("cmd", ""))
            super().send(raw)

    ws2 = RecordingWS()
    ch2 = _channel(ws2)
    ch2._ws = ws2

    def pump2():
        deadline = time.time() + 5
        while time.time() < deadline:
            for fr in list(ws2.sent):
                cmd = fr.get("cmd", "")
                if cmd.startswith("aibot_upload_media"):
                    rid = fr["headers"]["req_id"]
                    if cmd.endswith("init"):
                        body = {"errcode": 0, "upload_id": "UP1"}
                    elif cmd.endswith("finish"):
                        body = {"errcode": 0, "media_id": "MEDIA1"}
                    else:
                        body = {"errcode": 0}
                    ch2._deliver_ack({"cmd": "", "headers": {"req_id": rid},
                                      "errcode": 0, "body": body, **body})
                    with ws2.lock:
                        ws2.sent = [x for x in ws2.sent if x is not fr]
            time.sleep(0.02)

    t2 = threading.Thread(target=pump2, daemon=True)
    t2.start()
    assert ch2.send_media("c1", str(f))
    t2.join(timeout=2)

    cmds = list(ws2.all_cmds)
    assert "aibot_upload_media_init" in cmds
    assert cmds.count("aibot_upload_media_chunk") == 1
    assert "aibot_upload_media_finish" in cmds
    send_frames = [fr for fr in _all_sent(ws2) if fr.get("cmd") == "aibot_send_msg"]
    assert any(
        fr["body"].get("msgtype") == "image" and fr["body"]["image"]["media_id"] == "MEDIA1"
        for fr in send_frames
    )
    assert ok


def _all_sent(ws):
    # frames already consumed by pump are tracked in ch history; simplest: record
    return ws.sent


def test_wecom_send_media_too_large_downgrades_to_text(tmp_path):
    ws = FakeWS()
    ch = _channel(ws)
    ch._ws = ws
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * (21 * 1024 * 1024))  # > 20MB
    ok = ch.send_media("c1", str(f))
    assert not ok  # rejected, channel should reply text instead (checked in session layer)


# ---------------------------------------------------------------------------
# session layer: sentinel scan
# ---------------------------------------------------------------------------

def test_sentinel_diff_detects_new_files(tmp_path):
    from agenthub.media import scan_new_files

    base = {}
    events = scan_new_files(str(tmp_path), base)
    assert events == []

    f1 = _make_media_file(tmp_path, "a.png")
    events = scan_new_files(str(tmp_path), base)
    assert len(events) == 1
    assert events[0]["filename"] == "a.png"
    assert events[0]["kind"] == "image"
    # second scan: no new files
    assert scan_new_files(str(tmp_path), base) == []


def test_sentinel_ignores_hidden_and_oversize(tmp_path):
    from agenthub.media import scan_new_files, MEDIA_MAX_BYTES

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.png").write_bytes(b"p")
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (MEDIA_MAX_BYTES + 1))
    base = {}
    events = scan_new_files(str(tmp_path), base)
    assert events == []  # hidden dir ignored; oversize ignored (logged)


# ---------------------------------------------------------------------------
# session integration: prompt with files -> blocks + media_in storage
# ---------------------------------------------------------------------------

def test_prompt_with_file_stores_and_references(tmp_path, monkeypatch):
    """prompt() with files stores them under media_in/ and adds a path note."""
    from agenthub.session import SessionManager
    import agenthub.session as sess_mod

    captured = {}

    class StubClient:
        def __init__(self):
            self._notif_handlers = []
            self._req_handlers = []

        def is_alive(self):
            return True

        def register_notification_handler(self, h):
            self._notif_handlers.append(h)

        def register_request_handler(self, h):
            self._req_handlers.append(h)

        def session_prompt(self, sid, blocks, timeout=None):
            captured["blocks"] = blocks
            return {"stopReason": "end_turn"}

        def session_new(self, cwd=None, mcp_servers=None):
            return "s9"

        def set_config_option(self, sid, config_id, value):
            return {"currentValue": value}

        def list_config_options(self, sid):
            return {"configOptions": []}

    mgr = SessionManager({"opencode": {"cmd": "opencode acp"}}, db_path=str(tmp_path / "s.db"))
    stub = StubClient()
    monkeypatch.setattr(mgr, "_client_for", lambda name: stub)
    monkeypatch.setattr(mgr, "_ensure_session", lambda k, a, cwd=None: (stub, "s9"))

    media_dir = tmp_path / "agent_home"
    media_dir.mkdir()
    monkeypatch.setattr(mgr, "_media_dir", lambda agent: str(media_dir))

    f = tmp_path / "in.pdf"
    f.write_bytes(b"hello pdf")

    # stub workspace scan targets: keep sentinel happy (no whitelist -> cwd fallback)
    monkeypatch.setattr(mgr, "_agent_cwd", lambda agent: str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(exist_ok=True)

    mgr.prompt("wecom", "c1", "看看这个", files=[{"data": f.read_bytes(), "filename": "in.pdf", "mime": "application/pdf"}])

    text = captured["blocks"][0]["text"]
    assert "用户发送了文件" in text
    assert "agent_home" in text
    stored = list(media_dir.glob("*_in.pdf")) or list(media_dir.glob("media_in/*in.pdf"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == b"hello pdf"
    # file also delivered as an ACP content block path note (text only agent-facing)
