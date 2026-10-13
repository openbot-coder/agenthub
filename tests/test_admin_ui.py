"""Tests for the admin UI endpoint (served on / and /ui)."""

import json
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest

from agenthub.http_admin import StatusHTTP


class FakeBinding:
    def __init__(self, channel, chat_id, agent, acp_session_id):
        self.channel = channel
        self.chat_id = chat_id
        self.agent = agent
        self.acp_session_id = acp_session_id
        self.last_active = time.time()


@pytest.fixture
def server():
    mgr = MagicMock()
    mgr.list_agents.return_value = [
        {"name": "opencode", "cmd": "opencode acp", "running": True},
        {"name": "codex", "cmd": "codex acp", "running": False},
    ]
    mgr._bindings = {
        ("wecom", "c1"): FakeBinding("wecom", "c1", "opencode", "s1"),
    }
    mgr._lock = threading.Lock()
    http = StatusHTTP(mgr, host="127.0.0.1", port=0)  # port 0 = ephemeral
    http.start()
    port = http._server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    http.stop()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_root_serves_html(server):
    code, body = _get(server + "/")
    assert code == 200
    assert b"text/html" not in body  # header not in body; content check below
    assert b"agenthub" in body
    assert b"<!DOCTYPE html>" in body


def test_ui_alias(server):
    code, body = _get(server + "/ui")
    assert code == 200 and b"<!DOCTYPE html>" in body


def test_json_endpoints_still_work(server):
    code, body = _get(server + "/status")
    d = json.loads(body)
    assert d["service"] == "agenthub"
    assert len(d["agents"]) == 2
    code, body = _get(server + "/sessions")
    d = json.loads(body)
    assert d["bindings"][0]["agent"] == "opencode"


def test_html_references_fetch_targets(server):
    _, body = _get(server + "/")
    # UI fetches status/sessions relatively — must not hardcode hosts
    assert b"fetch('status')" in body
    assert b"fetch('sessions')" in body
    assert b"botflow_truncated" not in body  # sanity: unrelated content absent
