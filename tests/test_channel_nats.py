"""Tests for the NATS channel (mocked transport, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agenthub.channels.nats import NatsChannel
from agenthub.channels.base import InboundMessage


class FakeMsg:
    def __init__(self, subject: str, data: bytes, reply: str = ""):
        self.subject = subject
        self.data = data
        self.reply = reply

    def decode(self) -> str:
        return self.data.decode()


class FakeNATS:
    """Minimal SyncNATS stand-in: records subs/pubs, replays on demand."""

    def __init__(self, *a, **kw):
        self.subs: dict[str, Any] = {}
        self.pubs: list[tuple[str, bytes]] = []
        self.closed = False

    def subscribe(self, subject, cb):
        self.subs[subject] = cb
        return f"sid-{len(self.subs)}"

    def unsubscribe(self, sid):
        pass

    def publish(self, subject, data):
        self.pubs.append((subject, data))

    def close(self):
        self.closed = True


@pytest.fixture()
def ch_and_nc():
    nc = FakeNATS()
    ch = NatsChannel({"servers": "nats://x:4222"}, nats_factory=lambda *a, **kw: nc)
    inbound: list[InboundMessage] = []
    ch.on_message(inbound.append)
    ch.start()
    yield ch, nc, inbound
    ch.stop()


def _feed(nc: FakeNATS, subject: str, payload: dict, reply: str = ""):
    cb = next(iter(nc.subs.values()))
    cb(FakeMsg(subject, json.dumps(payload).encode(), reply))


def test_start_subscribes_and_stop_closes(ch_and_nc):
    ch, nc, _ = ch_and_nc
    assert "agenthub.in.>" in nc.subs
    ch.stop()
    assert nc.closed


def test_inbound_message_dispatch(ch_and_nc):
    ch, nc, inbound = ch_and_nc
    _feed(nc, "agenthub.in.chat1", {"chat_id": "chat1", "user_id": "u1", "text": "hello"})
    assert len(inbound) == 1
    m = inbound[0]
    assert m.channel == "nats"
    assert m.chat_id == "chat1"
    assert m.user_id == "u1"
    assert m.text == "hello"
    assert m.raw["subject"] == "agenthub.in.chat1"


def test_inbound_chat_id_falls_back_to_subject(ch_and_nc):
    ch, nc, inbound = ch_and_nc
    _feed(nc, "agenthub.in.room9", {"text": "hi"})
    assert inbound[0].chat_id == "room9"
    assert inbound[0].user_id == "room9"


def test_inbound_allowed_users_gate(ch_and_nc):
    ch, nc, inbound = ch_and_nc
    ch.allowed_users = {"alice"}
    _feed(nc, "agenthub.in.c1", {"chat_id": "c1", "user_id": "bob", "text": "x"})
    assert inbound == []
    _feed(nc, "agenthub.in.c1", {"chat_id": "c1", "user_id": "alice", "text": "x"})
    assert len(inbound) == 1


def test_inbound_bad_json_does_not_raise(ch_and_nc):
    ch, nc, inbound = ch_and_nc
    cb = next(iter(nc.subs.values()))
    cb(FakeMsg("agenthub.in.c1", b"not json"))
    cb(FakeMsg("agenthub.in.c1", json.dumps({"text": "  "}).encode()))  # empty text
    assert inbound == []


def test_final_event_publishes_to_out_subject(ch_and_nc):
    ch, nc, _ = ch_and_nc
    ch.push_event("chat1", "final", {"text": "answer"})
    assert nc.pubs == [("agenthub.out.chat1", json.dumps({"chat_id": "chat1", "text": "answer"}, ensure_ascii=False).encode())]


def test_delta_dropped_status_published(ch_and_nc):
    ch, nc, _ = ch_and_nc
    ch.push_event("c", "delta", {"text": "partial"})
    assert nc.pubs == []
    ch.push_event("c", "status", {"text": "running..."})
    assert nc.pubs[0][0] == "agenthub.out.c"
    assert b"running..." in nc.pubs[0][1]


def test_interaction_renders_numbered_options(ch_and_nc):
    ch, nc, _ = ch_and_nc
    ch.push_event("c", "interaction", {
        "interaction_type": "permission", "tool": "execute",
        "options": [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}],
        "request_id": "req-1",
    })
    subject, body = nc.pubs[0]
    assert subject == "agenthub.out.c"
    text = json.loads(body)["text"]
    assert "execute" in text and "Allow once" in text and "req-1" in text


def test_reply_splits_long_text(ch_and_nc):
    ch, nc, _ = ch_and_nc
    ch.reply("c", "x" * 1_200_000)  # > 900k budget -> 2 chunks
    assert len(nc.pubs) == 2
    total = sum(len(json.loads(b)["text"]) for _, b in nc.pubs)
    assert total >= 1_199_000
