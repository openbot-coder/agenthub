"""Keepalive + approval-hardening regressions (2026-10-07 outage lessons).

Covers:
- zombie connection: reader dead -> recv() raises (was: None forever, no reconnect)
- unlimited reconnects by default (network loss must not kill the bot)
- approval not shown yet -> reply rejected, not turned into an approval
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.channels.wecom import WecomAibotChannel  # noqa: E402
from agenthub.channels.wecom_ws import AibotWSAdapter  # noqa: E402
from agenthub.session import PermissionRequest, SessionError, SessionManager  # noqa: E402


# ---------------------------------------------------------------------------
# zombie connection detection
# ---------------------------------------------------------------------------

def test_recv_raises_when_reader_dead():
    """Reader task done + empty queue -> recv must raise, not return None."""
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "s", "ws_url": "ws://127.0.0.1:1"})
    loop = __import__("asyncio").new_event_loop()
    adapter._loop = loop
    done_evt = threading.Event()

    async def _die():
        await __import__("asyncio").sleep(0)
        done_evt.set()

    adapter._reader_task = loop.create_task(_die())
    # pump the loop on a thread until the task actually completes
    pump = threading.Thread(target=loop.run_forever, daemon=True)
    pump.start()
    assert done_evt.wait(5)
    with pytest.raises(ConnectionError, match="zombie|reader"):
        adapter.recv(timeout=0.2)
    loop.call_soon_threadsafe(loop.stop)


def test_recv_raises_on_stale_inbound():
    """No inbound for > stale window -> ConnectionError (half-open TCP)."""
    adapter = AibotWSAdapter({"bot_id": "b", "secret": "s", "ws_url": "ws://127.0.0.1:1"})
    loop = __import__("asyncio").new_event_loop()
    adapter._loop = loop
    import asyncio as _a
    adapter._reader_task = loop.create_task(_a.sleep(3600))
    adapter._last_inbound = 0.0  # ancient
    with pytest.raises(ConnectionError, match="stale|no inbound"):
        adapter.recv(timeout=0.2)


# ---------------------------------------------------------------------------
# unlimited reconnects
# ---------------------------------------------------------------------------

def test_default_reconnect_is_unlimited():
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s"})
    assert ch.max_reconnect == 0  # 0 = unlimited


def test_reconnect_never_exhausts_when_unlimited():
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s"})
    assert ch.max_reconnect == 0

    calls = {"n": 0}

    def boom(cfg):
        calls["n"] += 1
        raise ConnectionError("dead")

    ch._ws_factory = boom
    ch._stop = threading.Event()
    t = threading.Thread(target=ch._run, daemon=True)
    t.start()
    import time as _t
    _t.sleep(4)  # attempt 1 immediately, attempt 2 after the 2s backoff
    ch._stop.set()
    assert calls["n"] >= 2  # kept retrying well past the old limit of 10 backoffs
    assert ch.connection_state == "connecting"


def test_explicit_limit_still_exhausts():
    ch = WecomAibotChannel({"bot_id": "b", "secret": "s", "max_reconnect_attempts": 2})
    def boom(cfg):
        raise ConnectionError("dead")
    ch._ws_factory = boom
    t = threading.Thread(target=ch._run, daemon=True)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert ch.connection_state == "failed"


# ---------------------------------------------------------------------------
# approval hardening
# ---------------------------------------------------------------------------

def _mgr_with_pending(presented: bool) -> SessionManager:
    class FakeAcp:
        def is_alive(self):
            return True
        def session_prompt(self, *a, **k):
            return {"stopReason": "end_turn"}
    mgr = SessionManager(agents={}, client_factory=lambda spec: FakeAcp())
    pr = PermissionRequest(
        request_id="req-1", agent="a", acp_request_id=None, session_id="s1",
        key="wecom:c1", tool_title="read file", tool_kind="read", raw_request_id=None,
        options=[{"option_id": "allow_once", "name": "Allow once", "kind": "allow_once"}],
    )
    pr.presented = presented
    mgr._pending_permissions["req-1"] = pr
    return mgr


def test_reply_to_unpresented_approval_is_rejected():
    """A decision that arrives before the card was rendered must NOT approve."""
    mgr = _mgr_with_pending(presented=False)
    with pytest.raises(SessionError, match="not yet shown"):
        mgr.permission_reply("wecom", "c1", "req-1", "allow_once")


def test_reply_to_presented_approval_works():
    mgr = _mgr_with_pending(presented=True)
    mgr.permission_reply("wecom", "c1", "req-1", "allow_once")
    pr = mgr._pending_permissions["req-1"]
    assert pr.choice == "allow_once"
    assert pr.answered.is_set()


def test_numbered_answer_via_take_interaction_answer():
    mgr = _mgr_with_pending(presented=True)
    assert mgr.take_interaction_answer("wecom", "c1", "1") == "consumed"
    pr = mgr._pending_permissions["req-1"]
    assert pr.choice == "allow_once"
    assert pr.answered.is_set()


def test_stale_permissions_reaped():
    mgr = _mgr_with_pending(presented=True)
    mgr.permission_timeout_s = 300
    pr = mgr._pending_permissions["req-1"]
    pr.created_at -= 1000  # ancient
    mgr._reap_once()
    assert "req-1" not in mgr._pending_permissions
