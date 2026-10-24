"""Regressions: finish-frame overflow splitting + /status channel state."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.channels.wecom import WecomAibotChannel  # noqa: E402


class MockWS:
    def __init__(self):
        self.sent = []

    def connect(self):
        pass

    def recv(self, timeout=1.0):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        pass


def _channel() -> tuple[WecomAibotChannel, MockWS]:
    ws = MockWS()
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s"}, ws_factory=lambda cfg: ws)
    ch._ws = ws
    return ch, ws


def test_small_final_single_stream_frame():
    ch, ws = _channel()
    ch._req_ids["c1"] = ("r1", __import__("time").time())
    ch._stream_close("c1", "hello")
    streams = [m for m in ws.sent if m.get("cmd") == "aibot_respond_msg"]
    assert len(streams) == 1
    assert streams[0]["body"]["stream"]["finish"] is True
    assert streams[0]["body"]["stream"]["content"] == "hello"


def test_large_final_splits_into_stream_plus_markdown():
    ch, ws = _channel()
    ch._req_ids["c1"] = ("r1", __import__("time").time())
    big = "字" * 12000  # 3 bytes/char -> 36000 bytes > 19000 limit
    ch._stream_close("c1", big)
    streams = [m for m in ws.sent if m.get("cmd") == "aibot_respond_msg"]
    sends = [m for m in ws.sent if m.get("cmd") == "aibot_send_msg"]
    assert len(streams) == 1
    assert streams[0]["body"]["stream"]["finish"] is True
    # first chunk in the finish frame, remainder as markdown messages
    assert len(streams[0]["body"]["stream"]["content"].encode()) <= 19000
    assert len(sends) >= 1
    total = (streams[0]["body"]["stream"]["content"] + "".join(
        m["body"]["markdown"]["content"] for m in sends))
    assert total == big  # nothing lost
    for m in sends:
        assert len(m["body"]["markdown"]["content"].encode("utf-8")) <= 20000
