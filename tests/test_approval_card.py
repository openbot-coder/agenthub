"""Tests: wecom approval template cards.

card send frame shape, click event -> card_answer_cb, card update frame,
and SessionManager.answer_card.
"""

import json

from agenthub.channels.wecom import WecomAibotChannel
from agenthub.session import SessionManager, PermissionRequest


def _ch() -> WecomAibotChannel:
    return WecomAibotChannel(
        {"bot_id": "b", "secret": "s", "mock_transport": True}, ws_factory=None
    )


def _outbox(ch: WecomAibotChannel) -> list[dict]:
    return [json.loads(x) for x in ch._outbox]


def test_interaction_sends_template_card():
    ch = _ch()
    ch.push_event("vex1023", "interaction", {
        "interaction_type": "permission", "request_id": "req-1234",
        "tool": "run cmd", "kind": "execute", "agent": "opencode",
        "options": [
            {"option_id": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
    })
    frames = _outbox(ch)
    card = [f for f in frames if f.get("cmd") == "aibot_send_msg"
            and f["body"].get("msgtype") == "template_card"]
    assert card, frames
    tc = card[0]["body"]["template_card"]
    assert tc["card_type"] == "button_interaction"
    assert tc["task_id"] == "req_1234"
    keys = [b["key"] for b in tc["button_list"]]
    assert keys == ["req-1234|allow_once", "req-1234|reject_once"]
    assert tc["button_list"][1]["style"] == 4  # reject = danger style


def test_card_click_answers_permission():
    ch = _ch()
    answered = []

    def cb(req, opt, by):
        answered.append((req, opt, by))

    ch.card_answer_cb = cb
    ch._on_card_click(
        {
            "from": {"userid": "vex1023"},
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "card_type": "button_interaction",
                    "event_key": "req-1234|allow_once",
                    "task_id": "req_1234",
                },
            },
        },
        ws=None,
        cb_req_id="cb-1",
    )
    assert answered == [("req-1234", "allow_once", "vex1023")]
    frames = _outbox(ch)
    upd = [f for f in frames if f.get("cmd") == "aibot_respond_update_msg"]
    assert upd and upd[0]["headers"]["req_id"] == "cb-1"
    assert "已批准" in upd[0]["body"]["template_card"]["main_title"]["title"]
    assert upd[0]["body"]["template_card"]["task_id"] == "req_1234"


def test_card_click_reject_title():
    ch = _ch()
    ch.card_answer_cb = lambda *a: None
    ch._on_card_click(
        {
            "event": {"eventtype": "template_card_event",
                      "template_card_event": {"event_key": "r1|reject_once", "task_id": "r1"}},
        }, ws=None, cb_req_id="cb2",
    )
    upd = [f for f in _outbox(ch) if f.get("cmd") == "aibot_respond_update_msg"]
    assert "已拒绝" in upd[0]["body"]["template_card"]["main_title"]["title"]


def _mgr() -> SessionManager:
    return SessionManager(agents={}, client_factory=lambda spec: None)


def _pending(mgr: SessionManager, req_id="req1") -> None:
    mgr._pending_permissions[req_id] = PermissionRequest(
        request_id=req_id, agent="a", acp_request_id=None, session_id="s",
        key="nats:c", tool_title="t", tool_kind="execute", raw_request_id=None,
        options=[{"option_id": "allow_once", "name": "A", "kind": "allow_once"}],
    )
    mgr._pending_permissions[req_id].presented = True


def test_answer_card_sets_choice():
    import pytest
    from agenthub.session import SessionError
    mgr = _mgr()
    _pending(mgr)
    mgr.answer_card("req1", "allow_once", "vex1023")
    assert mgr._pending_permissions["req1"].choice == "allow_once"
    # unknown request id -> error
    with pytest.raises(SessionError):
        mgr.answer_card("nope", "allow_once")
