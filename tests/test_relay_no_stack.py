"""Regression test: repeated prompts must NOT stack duplicate update-relay
handlers (the "repeated/interleaved reply" bug: every prompt attached another
notification handler, so the Nth turn's deltas were appended N times)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from agenthub.session import SessionManager, AgentSpec


class StubClient:
    """Minimal ACP client stub recording registered handlers.

    Attribute names mirror the real ACPClient (_notif_handlers/_req_handlers)
    because the dedup guard in session.py reads them."""

    def __init__(self):
        self._notif_handlers: list = []
        self._req_handlers: list = []
        self.prompts: list = []

    def register_notification_handler(self, h):
        self._notif_handlers.append(h)

    def register_request_handler(self, h):
        self._req_handlers.append(h)

    def is_alive(self):
        return True

    def session_prompt(self, sid, blocks, timeout=None):
        self.prompts.append((sid, blocks))
        return {"stopReason": "end_turn"}

    def request(self, method, params, timeout=None):
        return {}


@pytest.fixture()
def mgr_with_stub():
    spec = AgentSpec(name="opencode", cmd=["stub"])
    stub = StubClient()
    mgr = SessionManager({"opencode": spec}, client_factory=lambda s: stub)
    # pre-bind a session so prompt() doesn't spawn a real process
    from agenthub.session import Binding
    b = Binding(channel="nats", chat_id="c1", agent="opencode", acp_session_id="s1")
    mgr._bindings[mgr.key("nats", "c1")] = b
    return mgr, stub, b


def test_no_handler_stacking_across_turns(mgr_with_stub):
    mgr, stub, b = mgr_with_stub
    events = []
    mgr.set_event_sink(lambda key, kind, payload: events.append((kind, payload)))

    for i in range(4):
        mgr.prompt("nats", "c1", f"msg {i}")

    # one notification handler per client, not one per prompt
    assert len(stub._notif_handlers) == 1, (
        f"handler stacking: {len(stub._notif_handlers)} handlers after 4 prompts"
    )
    # final events delivered per turn (stub sends no deltas; empty text ok)
    finals = [p for k, p in events if k == "final"]
    assert len(finals) == 4, f"expected 4 finals, got {len(finals)}"

def test_delta_appended_once_with_single_handler(mgr_with_stub):
    """Simulate the agent sending one agent_message_chunk: with the dedup
    guard, a 3-prompt history still appends it exactly once."""
    mgr, stub, b = mgr_with_stub
    events = []
    mgr.set_event_sink(lambda key, kind, payload: events.append((kind, payload)))

    for i in range(3):
        mgr.prompt("nats", "c1", f"msg {i}")

    assert len(stub._notif_handlers) == 1
    h = stub._notif_handlers[0]
    h("session/update", {"sessionId": "s1", "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}}})
    with mgr._lock:
        stored = list(mgr._deltas.get("nats:c1", []))
    assert stored == ["hello"], f"delta duplicated: {stored}"
