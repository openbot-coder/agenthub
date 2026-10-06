"""Tests for SessionManager: bindings, switch, reaper, cwd whitelist, persistence.

Uses the mock ACP agent (tests/fixtures/mock_acp_agent.py) as real subprocess.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.acp_client import ACPClient  # noqa: E402
from agenthub.session import (  # noqa: E402
    AgentSpec,
    CwdNotAllowed,
    SessionManager,
    UnknownAgent,
)

MOCK = os.path.join(os.path.dirname(__file__), "fixtures", "mock_acp_agent.py")


def make_manager(tmp_path, agents=None, **kw) -> SessionManager:
    agents = agents or {"mock": AgentSpec(name="mock", cmd=[sys.executable, MOCK, "--chunks", "2"])}
    return SessionManager(agents=agents, transport="ndjson", **kw)


def test_prompt_creates_binding_and_returns_turn(tmp_path):
    m = make_manager(tmp_path)
    res = m.prompt("wecom", "chat1", "hello")
    assert res["stop_reason"] == "end_turn"
    assert res["agent"] == "mock"
    b = m.get_binding("wecom", "chat1")
    assert b is not None and b.acp_session_id == res["session_id"]
    # second prompt reuses the same ACP session
    res2 = m.prompt("wecom", "chat1", "again")
    assert res2["session_id"] == res["session_id"]
    m.stop()


def test_prompt_explicit_agent_rebinds(tmp_path):
    m = make_manager(tmp_path, agents={
        "a": AgentSpec(name="a", cmd=[sys.executable, MOCK]),
        "b": AgentSpec(name="b", cmd=[sys.executable, MOCK]),
    })
    r1 = m.prompt("wx", "c1", "hi", agent="a")
    r2 = m.prompt("wx", "c1", "hi", agent="b")
    assert r1["agent"] == "a" and r2["agent"] == "b"
    assert r2["session_id"] != r1["session_id"]
    m.stop()


def test_unknown_agent_rejected(tmp_path):
    m = make_manager(tmp_path)
    try:
        m.prompt("wx", "c", "hi", agent="nope")
        assert False, "should raise"
    except UnknownAgent:
        pass
    m.stop()


def test_cwd_whitelist(tmp_path):
    spec = AgentSpec(name="mock", cmd=[sys.executable, MOCK], cwd_whitelist=[str(tmp_path) + "/allowed/*"])
    m = make_manager(tmp_path, agents={"mock": spec})
    try:
        m.prompt("wx", "c", "hi", cwd=str(tmp_path / "other"))
        assert False, "should raise CwdNotAllowed"
    except CwdNotAllowed:
        pass
    ok = m.prompt("wx", "c", "hi", cwd=str(tmp_path / "allowed" / "sub"))
    assert ok["stop_reason"] == "end_turn"
    m.stop()


def test_switch_migrates_text_only_cross_agent(tmp_path):
    m = make_manager(tmp_path, agents={
        "a": AgentSpec(name="a", cmd=[sys.executable, MOCK]),
        "b": AgentSpec(name="b", cmd=[sys.executable, MOCK]),
    })
    m.prompt("wecom", "chat9", "question one", agent="a")
    res = m.switch("wecom", "chat9", "b", cont=True)
    assert res["agent"] == "b" and res["migrated"] >= 1
    rec = m._records["wecom:chat9"]
    assert rec.agent == "b"
    # old binding gone, new binding present
    b = m.get_binding("wecom", "chat9")
    assert b.acp_session_id == res["acp_session_id"]
    m.stop()


def test_switch_unknown_agent(tmp_path):
    m = make_manager(tmp_path)
    try:
        m.switch("wx", "c", "ghost")
        assert False
    except UnknownAgent:
        pass
    m.stop()


def test_events_relayed_via_sink(tmp_path):
    m = make_manager(tmp_path)
    events = []
    m.set_event_sink(lambda key, kind, payload: events.append((key, kind)))
    m.prompt("wecom", "chatE", "hello")
    kinds = {k for _, k in events}
    assert "delta" in kinds and "final" in kinds
    assert all(key == "wecom:chatE" for key, _ in events)
    m.stop()


def test_sqlite_persistence_roundtrip(tmp_path):
    db = str(tmp_path / "sessions.db")
    m = make_manager(tmp_path, db_path=db)
    m.prompt("wecom", "chatP", "persist me")
    m.stop()  # closes clients but DB rows persist

    m2 = make_manager(tmp_path, db_path=db)
    b = m2.get_binding("wecom", "chatP")
    assert b is not None and b.agent == "mock"
    rec = m2._records["wecom:chatP"]
    assert any(e["text"] == "persist me" for e in rec.text_log)
    m2.stop()


def test_max_sessions_eviction(tmp_path):
    agents = {f"a{i}": AgentSpec(name=f"a{i}", cmd=[sys.executable, MOCK]) for i in range(3)}
    m = make_manager(tmp_path, agents=agents, max_sessions=2)
    m.prompt("wx", "c1", "hi", agent="a0")
    m.prompt("wx", "c2", "hi", agent="a1")
    assert set(m._clients) == {"a0", "a1"}
    m.prompt("wx", "c3", "hi", agent="a2")  # a0 evicted (unbound LRU)
    assert "a2" in m._clients and "a0" not in m._clients
    m.stop()


def test_idle_reaper_unbinds_and_closes(tmp_path):
    agents = {"mock": AgentSpec(name="mock", cmd=[sys.executable, MOCK])}
    m = make_manager(tmp_path, agents=agents, idle_ttl_s=0.05)
    m.prompt("wx", "cR", "hi")
    time.sleep(0.2)
    m._reap_once()
    assert m.get_binding("wx", "cR") is None
    assert m._clients == {}
    m.stop()


def test_unbind_removes_only_binding(tmp_path):
    m = make_manager(tmp_path)
    m.prompt("wx", "cU", "hi")
    m.unbind("wx", "cU")
    assert m.get_binding("wx", "cU") is None
    m.stop()
