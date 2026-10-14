"""Tests: approval routing to the wecom approval chat.

covers SessionManager.set_route / route_key matching in
pending_interaction + permission_reply, and runtime._route_approval
multi-channel dispatch (dispatch-all sink + approval multicast).
"""

from agenthub.session import SessionManager, PermissionRequest
from agenthub.runtime import _route_approval


def _mgr() -> SessionManager:
    return SessionManager(agents={}, client_factory=lambda spec: None)


def _pending(mgr: SessionManager, req_id="req1", key="nats:chat-a") -> None:
    mgr._pending_permissions[req_id] = PermissionRequest(
        request_id=req_id, agent="opencode", acp_request_id=None,
        session_id="sid", key=key, tool_title="run cmd",
        tool_kind="execute", raw_request_id=None,
        options=[
            {"option_id": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
    )
    mgr._pending_permissions[req_id].presented = True


def test_route_key_matches_pending_interaction():
    mgr = _mgr()
    _pending(mgr)
    mgr.set_route("req1", "wecom:vex1023")
    inter = mgr.pending_interaction("wecom", "vex1023")
    assert inter is not None and inter["request_id"] == "req1"
    # originating chat still matches too
    assert mgr.pending_interaction("nats", "chat-a")["request_id"] == "req1"
    # unrelated chat does not
    assert mgr.pending_interaction("wecom", "other") is None


def test_permission_reply_from_routed_chat():
    mgr = _mgr()
    _pending(mgr)
    mgr.set_route("req1", "wecom:vex1023")
    # reply from the approval route chat: accepted
    mgr.permission_reply("wecom", "vex1023", "req1", "allow_once")
    assert mgr._pending_permissions["req1"].choice == "allow_once"


def test_permission_reply_wrong_chat_rejected():
    import pytest
    from agenthub.session import SessionError
    mgr = _mgr()
    _pending(mgr)
    mgr.set_route("req1", "wecom:vex1023")
    with pytest.raises(SessionError):
        mgr.permission_reply("wecom", "someone-else", "req1", "allow_once")


def test_take_interaction_answer_from_routed_chat():
    mgr = _mgr()
    _pending(mgr)
    mgr.set_route("req1", "wecom:vex1023")
    assert mgr.take_interaction_answer("wecom", "vex1023", "1") == "consumed"
    assert mgr._pending_permissions["req1"].choice == "allow_once"


class _FakeWecom:
    name = "wecom"

    def __init__(self):
        self.events = []

    def push_event(self, chat_id, kind, payload):
        self.events.append((chat_id, kind, payload))


def test_route_approval_multicast():
    wecom = _FakeWecom()
    mgr = _mgr()
    _pending(mgr)
    ccfg = {"wecom": {"approval_chat": "vex1023"}}
    payload = {"request_id": "req1", "interaction_type": "permission",
               "tool": "run cmd", "options": []}
    _route_approval([wecom], ccfg, mgr, "nats:chat-a", payload)
    assert wecom.events and wecom.events[0][0] == "vex1023"
    assert wecom.events[0][1] == "interaction"
    assert wecom.events[0][2]["routed_from"] == "nats:chat-a"
    assert mgr._pending_permissions["req1"].route_key == "wecom:vex1023"


def test_route_approval_no_config_noop():
    wecom = _FakeWecom()
    mgr = _mgr()
    _pending(mgr)
    _route_approval([wecom], {}, mgr, "nats:chat-a",
                    {"request_id": "req1", "options": []})
    assert wecom.events == []
    assert mgr._pending_permissions["req1"].route_key is None


def test_route_approval_origin_is_approval_chat_noop():
    wecom = _FakeWecom()
    mgr = _mgr()
    _pending(mgr, key="wecom:vex1023")
    ccfg = {"wecom": {"approval_chat": "vex1023"}}
    _route_approval([wecom], ccfg, mgr, "wecom:vex1023",
                    {"request_id": "req1", "options": []})
    assert wecom.events == []  # not duplicated
