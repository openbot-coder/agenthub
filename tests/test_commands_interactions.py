"""Tests for IM commands, inbound ack, and structured permission interactions."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from agenthub.channels.base import _command_result_text, _interaction_text
from agenthub.session import AgentSpec, SessionManager


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeAcp:
    """Minimal ACP client stub: records requests, replays canned replies."""

    def __init__(self, agent: str):
        self.agent = agent
        self.requests: list[tuple[str, dict]] = []
        self.notif_handlers = []
        self.req_handlers = []
        self._canned: dict[str, dict] = {}
        self._lock = threading.Lock()

    def set_canned(self, method: str, reply: dict) -> None:
        self._canned[method] = reply

    def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        with self._lock:
            self.requests.append((method, params))
        if method in self._canned:
            return self._canned[method]
        if method == "session/new":
            self._n = getattr(self, "_n", 0) + 1
            sid = f"sess-{self._n}"
            self._sid_current = sid
            return {"sessionId": sid, "configOptions": [
                {"id": "model", "currentValue": "gpt-x", "options": [
                    {"value": "gpt-x", "name": "GPT X"},
                    {"value": "claude-y", "name": "Claude Y"},
                ]},
            ]}
        if method == "session/set_config_option":
            return {"configOptions": [
                {"id": "model", "currentValue": params["value"], "options": []},
            ]}
        if method == "session/prompt":
            # fire turn events synchronously
            sid = params["sessionId"]
            for h in list(self.notif_handlers):
                h("session/update", {"sessionId": sid, "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hello"}}})
                h("session/update", {"sessionId": sid, "update": {
                    "sessionUpdate": "turn_end"}})
            return {"stopReason": "end_turn"}
        return {}

    def register_notification_handler(self, h) -> None:
        self.notif_handlers.append(h)

    def register_request_handler(self, h) -> None:
        self.req_handlers.append(h)

    def close(self) -> None: ...


@pytest.fixture()
def mgr(monkeypatch, tmp_path):
    m = SessionManager(
        agents={"opencode": AgentSpec(name="opencode", cmd=[sys.executable, "-c", "import time; time.sleep(600)"])},
        transport="ndjson",
    )
    clients = {}

    def fake_client_for(agent, spec=None):
        if agent not in clients:
            clients[agent] = FakeAcp(agent)
        return clients[agent]

    monkeypatch.setattr(m, "_client_for", fake_client_for)
    monkeypatch.setattr(m, "list_agents", lambda: [{"name": "opencode", "running": True}])
    monkeypatch.setattr(m, "_default_agent", lambda: "opencode")
    m._clients = clients  # type: ignore[assignment]
    yield m
    m.stop()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def test_non_command_returns_none(mgr):
    assert mgr.handle_command("wecom", "c1", "hello world") is None


def test_new_command_creates_fresh_session(mgr):
    mgr.switch("wecom", "c1", "opencode")
    b1 = mgr.get_binding("wecom", "c1")
    assert b1 is not None
    sid1 = b1.acp_session_id
    res = mgr.handle_command("wecom", "c1", "/new")
    assert res["type"] == "new"
    b2 = mgr.get_binding("wecom", "c1")
    assert b2 is not None
    assert b2.acp_session_id != sid1


def test_model_list_and_set(mgr):
    mgr.switch("wecom", "c1", "opencode")
    res = mgr.handle_command("wecom", "c1", "/model")
    assert res["type"] == "model_list"
    values = [mm["value"] for mm in res["models"]]
    assert "gpt-x" in values and "claude-y" in values
    cur = [mm for mm in res["models"] if mm["current"]]
    assert cur and cur[0]["value"] == "gpt-x"

    res = mgr.handle_command("wecom", "c1", "/model claude-y")
    assert res["type"] == "model_set"
    client = mgr._clients["opencode"]
    assert ("session/set_config_option",
            {"sessionId": "sess-1", "configId": "model", "value": "claude-y"}) in client.requests


def test_status_help_agents(mgr):
    assert mgr.handle_command("wecom", "c1", "/help")["type"] == "help"
    assert mgr.handle_command("wecom", "c1", "/agents")["type"] == "agents"
    st = mgr.handle_command("wecom", "c1", "/status")
    assert st["type"] == "status"


def test_unknown_command_is_error(mgr):
    res = mgr.handle_command("wecom", "c1", "/frobnicate")
    assert res["type"] == "error"


def test_command_without_session_model_errors(mgr):
    res = mgr.handle_command("wecom", "c1", "/model")
    assert res["type"] == "error"


# ---------------------------------------------------------------------------
# ack + interaction rendering
# ---------------------------------------------------------------------------

def test_ack_event_emitted_on_inbound(mgr):
    seen = []
    mgr.set_event_sink(lambda key, kind, payload: seen.append((kind, payload)))
    mgr.prompt = lambda *a, **k: seen.append(("final", {"text": "done"}))  # skip agent
    from agenthub.runtime import _safe_inbound

    class Msg:
        chat_id = "c9"
        user_id = "u9"
        text = "hi there"
        images = None

    class Ch:
        name = "wecom"

    _safe_inbound(mgr, Ch(), Msg())
    kinds = [k for k, _ in seen]
    assert "ack" in kinds
    ack = next(p for k, p in seen if k == "ack")
    assert ack["channel"] == "wecom" and ack["chat_id"] == "c9"


def test_interaction_text_renders_options():
    txt = _interaction_text({
        "interaction_type": "permission",
        "tool": "rm -rf /tmp/x",
        "kind": "execute",
        "options": [
            {"option_id": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
        "request_id": "abc123",
    })
    assert "rm -rf /tmp/x" in txt
    assert "1. Allow once" in txt
    assert "2. Reject" in txt
    assert "abc123" in txt


def test_command_result_rendering():
    assert "可用模型" in _command_result_text(
        {"type": "model_list", "models": [{"value": "m", "name": "M", "current": True}]})
    assert "新会话" in _command_result_text({"type": "new", "agent": "opencode"})
    assert "opencode" in _command_result_text(
        {"type": "agents", "agents": [{"name": "opencode", "running": True}]})


# ---------------------------------------------------------------------------
# permission request -> interaction event -> permission_reply round-trip
# ---------------------------------------------------------------------------

def test_permission_round_trip(mgr):
    events = []
    mgr.set_event_sink(lambda key, kind, payload: events.append((kind, payload)))
    mgr.switch("wecom", "c1", "opencode")
    b = mgr.get_binding("wecom", "c1")
    assert b is not None
    mgr._attach_update_relay(b)
    client = mgr._clients["opencode"]
    assert client.req_handlers, "request handler not registered"

    result_holder = {}

    def agent_side():
        result_holder["resp"] = client.req_handlers[0]("session/request_permission", {
            "sessionId": b.acp_session_id,
            "toolCall": {"title": "rm -rf /tmp/x", "kind": "execute"},
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        })

    th = threading.Thread(target=agent_side, daemon=True)
    th.start()
    time.sleep(0.2)

    interactions = [p for k, p in events if k == "interaction"]
    assert interactions, "no interaction event emitted"
    inter = interactions[0]
    assert inter["interaction_type"] == "permission"
    assert inter["tool"] == "rm -rf /tmp/x"
    assert [o["option_id"] for o in inter["options"]] == ["allow_once", "reject_once"]
    rid = inter["request_id"]

    mgr.permission_reply("wecom", "c1", rid, "1")  # numbered reply == allow_once? see aliasing below
    th.join(timeout=3)
    resp = result_holder["resp"]
    assert resp["outcome"]["outcome"] == "selected"


def test_permission_wrong_chat_rejected(mgr):
    mgr.switch("wecom", "c1", "opencode")
    b = mgr.get_binding("wecom", "c1")
    assert b is not None
    mgr._attach_update_relay(b)
    client = mgr._clients["opencode"]
    holder = {}
    th = threading.Thread(target=lambda: holder.update(resp=client.req_handlers[0](
        "session/request_permission",
        {"sessionId": b.acp_session_id, "toolCall": {"title": "t"}, "options": []})), daemon=True)
    th.start()
    time.sleep(0.2)
    rid = next(p["request_id"] for k, p in
               [(k, p) for k, p in []] ) if False else None
    # grab pending id via internal state
    with mgr._lock:
        rid = next(iter(mgr._pending_permissions))
    with pytest.raises(Exception):
        mgr.permission_reply("other", "chat", rid, "allow")
    with mgr._lock:
        pr = mgr._pending_permissions[rid]
    pr.choice = "reject_once"
    pr.answered.set()
    th.join(timeout=3)


def test_interaction_answer_consumed_by_inbound(mgr, monkeypatch):
    """User's numbered reply during a pending permission must reach
    permission_reply, not the agent."""
    import agenthub.runtime as rt

    events = []
    mgr.switch("wecom", "c1", "opencode")
    b = mgr.get_binding("wecom", "c1")
    assert b is not None
    mgr._attach_update_relay(b)
    mgr.set_event_sink(lambda k, kind, p: events.append((kind, p)))
    client = mgr._clients["opencode"]

    # simulate agent asking permission (non-blocking thread like real client)
    holder = {}
    th = threading.Thread(target=lambda: holder.update(resp=client.req_handlers[0](
        "session/request_permission",
        {"sessionId": b.acp_session_id, "toolCall": {"title": "read /mnt/quant/README.md"},
         "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                     {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                     {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]})), daemon=True)
    th.start()
    time.sleep(0.2)

    # inbound "2" via _safe_inbound
    forwarded = []

    def fake_prompt(*a, **k):
        forwarded.append((a, k))

    monkeypatch.setattr(mgr, "prompt", fake_prompt)

    class Msg:
        chat_id = "c1"
        user_id = "u1"
        text = "2"
        images = None

    class Ch:
        name = "wecom"

    rt._safe_inbound(mgr, Ch(), Msg())
    time.sleep(0.3)
    assert not forwarded, "answer leaked to the agent"
    th.join(timeout=3)
    resp = holder["resp"]
    assert resp["outcome"]["optionId"] == "allow_always", resp
    kinds = [k for k, _ in events]
    assert "ack" in kinds and "status" in kinds


def test_interaction_answer_not_matching_prompts_hint(mgr, monkeypatch):
    import agenthub.runtime as rt

    events = []
    mgr.switch("wecom", "c1", "opencode")
    b = mgr.get_binding("wecom", "c1")
    assert b is not None
    mgr._attach_update_relay(b)
    mgr.set_event_sink(lambda k, kind, p: events.append((kind, p)))
    client = mgr._clients["opencode"]

    th = threading.Thread(target=lambda: client.req_handlers[0](
        "session/request_permission",
        {"sessionId": b.acp_session_id, "toolCall": {"title": "t"},
         "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"}]}), daemon=True)
    th.start()
    time.sleep(0.2)

    class Msg:
        chat_id = "c1"
        user_id = "u1"
        text = "好了吗"
        images = None

    class Ch:
        name = "wecom"

    rt._safe_inbound(mgr, Ch(), Msg())
    time.sleep(0.3)
    finals = [p for k, p in events if k == "final"]
    assert finals and "待选择" in finals[0]["text"]
    # cleanup: deny the pending one
    mgr.take_interaction_answer("wecom", "c1", "r")
    th.join(timeout=3)


def test_no_pending_answer_goes_to_agent(mgr, monkeypatch):
    import agenthub.runtime as rt

    forwarded = []
    monkeypatch.setattr(mgr, "prompt", lambda *a, **k: forwarded.append(1))

    class Msg:
        chat_id = "cz"
        user_id = "u"
        text = "2"
        images = None

    class Ch:
        name = "wecom"

    rt._safe_inbound(mgr, Ch(), Msg())
    time.sleep(0.3)
    assert forwarded, "normal prompt should reach agent when nothing pending"


def test_permission_timeout_denies(mgr, monkeypatch):
    mgr.permission_timeout_s = 0.3
    mgr.switch("wecom", "c1", "opencode")
    b = mgr.get_binding("wecom", "c1")
    assert b is not None
    mgr._attach_update_relay(b)
    client = mgr._clients["opencode"]
    holder = {}
    th = threading.Thread(target=lambda: holder.update(resp=client.req_handlers[0](
        "session/request_permission",
        {"sessionId": b.acp_session_id, "toolCall": {"title": "t"},
         "options": [{"optionId": "allow_once", "kind": "allow_once"},
                     {"optionId": "reject_once", "kind": "reject_once"}]})), daemon=True)
    th.start()
    th.join(timeout=5)
    assert holder["resp"]["outcome"]["optionId"] == "reject_once"
