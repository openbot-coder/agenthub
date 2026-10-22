"""Follow-up messages arriving mid-turn are queued and appended as a
supplement in the next prompt, instead of waiting behind the lock as an
independent turn.

Flow (per chat):
  1. prompt() while a turn is in flight -> message is queued, caller gets
     {"queued": True} immediately; a status event "…将合并到本轮结束后追加"
     is emitted so the user sees an ack.
  2. when the in-flight turn finishes, the queue is drained inside the same
     lock: all pending texts are joined and prompted as one supplement turn
     ("[用户补充] …"). The channel's own inbound thread that queued the
     message is released immediately (it does not block waiting).
"""

import threading
import time


def _make_mgr(tmp_path, monkeypatch, stub):
    from agenthub.session import SessionManager

    mgr = SessionManager({"opencode": {"cmd": "opencode acp"}},
                         db_path=str(tmp_path / "s.db"))
    monkeypatch.setattr(mgr, "_client_for", lambda name: stub)
    monkeypatch.setattr(mgr, "_ensure_session", lambda k, a, cwd=None: (stub, "s9"))
    monkeypatch.setattr(mgr, "_agent_cwd", lambda agent: str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(exist_ok=True)
    events: list[tuple] = []
    monkeypatch.setattr(mgr, "_sink", lambda k, kind, payload: events.append((k, kind, payload)))
    mgr._test_events = events
    monkeypatch.setattr("agenthub.session.scan_new_files", lambda *a, **k: [],
                        raising=False)
    return mgr


class StubClient:
    def __init__(self):
        self._notif_handlers = []
        self._req_handlers = []
        self.prompts = []
        self.gate = threading.Event()  # when set, session_prompt returns
        self.lock = threading.Lock()

    def is_alive(self):
        return True

    def register_notification_handler(self, h):
        self._notif_handlers.append(h)

    def register_request_handler(self, h):
        self._req_handlers.append(h)

    def session_prompt(self, sid, blocks, timeout=None):
        with self.lock:
            self.prompts.append([dict(b) for b in blocks])
        self.gate.wait(timeout=10)
        return {"stopReason": "end_turn"}

    def session_new(self, cwd=None, mcp_servers=None):
        return "s9"

    def set_config_option(self, sid, config_id, value):
        return {"currentValue": value}

    def list_config_options(self, sid):
        return {"configOptions": []}

    def close(self, timeout: float = 2.0):
        pass


def test_second_message_queues_and_appends(tmp_path, monkeypatch):
    """While turn 1 is in flight, prompt() returns queued=True at once;
    after turn 1 finishes the queued text is prompted as [用户补充]."""
    stub = StubClient()
    mgr = _make_mgr(tmp_path, monkeypatch, stub)

    t1_done = threading.Event()

    def turn1():
        mgr.prompt("wecom", "c1", "第一问")
        t1_done.set()

    threading.Thread(target=turn1, daemon=True).start()
    deadline = time.time() + 5
    while len(stub.prompts) < 1 and time.time() < deadline:
        time.sleep(0.02)
    assert len(stub.prompts) == 1

    # second message arrives mid-turn: must queue, not block
    res2 = mgr.prompt("wecom", "c1", "补充内容")
    assert res2.get("queued") is True
    time.sleep(0.1)
    assert len(stub.prompts) == 1, "queued message must not hit the agent yet"
    assert any(kind == "status" for _, kind, _ in mgr._test_events)

    # release turn 1 -> the supplement turn must fire automatically
    stub.gate.set()
    assert t1_done.wait(timeout=5)
    deadline = time.time() + 5
    while len(stub.prompts) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(stub.prompts) == 2, "supplement turn should auto-fire"
    text = stub.prompts[1][0]["text"]
    assert "[用户补充]" in text and "补充内容" in text


def test_queue_drains_multiple_messages_in_one_turn(tmp_path, monkeypatch):
    stub = StubClient()
    mgr = _make_mgr(tmp_path, monkeypatch, stub)

    threading.Thread(target=lambda: mgr.prompt("wecom", "c1", "主问"),
                     daemon=True).start()
    deadline = time.time() + 5
    while len(stub.prompts) < 1 and time.time() < deadline:
        time.sleep(0.02)
    mgr.prompt("wecom", "c1", "补充一")
    mgr.prompt("wecom", "c1", "补充二")
    stub.gate.set()
    deadline = time.time() + 5
    while len(stub.prompts) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(stub.prompts) == 2
    text = stub.prompts[1][0]["text"]
    assert text.index("补充一") < text.index("补充二")


def test_no_queue_when_turn_finished(tmp_path, monkeypatch):
    class FastStub(StubClient):
        def session_prompt(self, sid, blocks, timeout=None):
            with self.lock:
                self.prompts.append([dict(b) for b in blocks])
            return {"stopReason": "end_turn"}  # no gate: turns finish at once

    stub = FastStub()
    mgr = _make_mgr(tmp_path, monkeypatch, stub)
    res = mgr.prompt("wecom", "c1", "单独一条")
    assert res.get("queued") is None
    assert len(stub.prompts) == 1
    assert "[用户补充]" not in stub.prompts[0][0]["text"]
