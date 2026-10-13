"""Tests for admin UI + auth + data endpoints (served on /, /ui, /api/*)."""

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from agenthub.admin_data import check_auth, sanitize_config
from agenthub.http_admin import StatusHTTP

ADMIN = {"username": "ops", "password": "s3cret"}


class FakeBinding:
    def __init__(self, channel, chat_id, agent, acp_session_id):
        self.channel = channel
        self.chat_id = chat_id
        self.agent = agent
        self.acp_session_id = acp_session_id
        self.created_at = time.time()
        self.last_active = time.time()


class FakeRecord:
    def __init__(self, key, agent, sid, log):
        self.binding_key = key
        self.agent = agent
        self.acp_session_id = sid
        self.text_log = log
        self.updated_at = time.time()


def make_mgr():
    mgr = MagicMock()
    mgr.list_agents.return_value = [
        {"name": "opencode", "cmd": "opencode acp", "running": True},
    ]
    b = FakeBinding("wecom", "c1", "opencode", "s1")
    mgr._bindings = {("wecom", "c1"): b}
    mgr._records = {
        "wecom:c1": FakeRecord(
            "wecom:c1", "opencode", "s1",
            [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
        )
    }
    mgr._pending_permissions = {}
    mgr._lock = threading.Lock()
    mgr._session_config.return_value = {"model": {"current": "opencode/mimo-v2.6-flash-free"}}
    return mgr


@pytest.fixture
def auth_server():
    http = StatusHTTP(make_mgr(), host="127.0.0.1", port=0, admin_cfg=ADMIN)
    http.start()
    port = http._server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    http.stop()


@pytest.fixture
def open_server():
    http = StatusHTTP(make_mgr(), host="127.0.0.1", port=0, admin_cfg=None)
    http.start()
    port = http._server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    http.stop()


def _get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- auth unit ---

def test_check_auth_ok_and_bad():
    assert check_auth("Basic " + base64.b64encode(b"ops:s3cret").decode(), ADMIN)
    assert not check_auth("Basic " + base64.b64encode(b"ops:wrong").decode(), ADMIN)
    assert not check_auth(None, ADMIN)
    assert not check_auth("Basic abc", ADMIN)          # malformed b64 -> False
    assert not check_auth("Basic " + base64.b64encode(b"ops:s3cret").decode(), None)  # fail-closed


def test_sanitize_config_redacts_secrets():
    cfg = {"channels": [{"name": "wecom", "secret": "abcdef123456"}], "http": {"port": 30990}}
    out = sanitize_config(cfg)
    assert out["channels"][0]["secret"].endswith("[REDACTED]")
    assert "abcdef123456" not in json.dumps(out)
    assert out["http"]["port"] == 30990


# --- auth-gated server ---

def test_root_requires_auth(auth_server):
    code, _ = _get(auth_server + "/")
    assert code == 401


def test_ui_ok_with_auth(auth_server):
    code, body = _get(auth_server + "/ui", auth="ops:s3cret")
    assert code == 200 and b"<!DOCTYPE html>" in body


def test_api_endpoints(auth_server):
    code, body = _get(auth_server + "/api/sessions", auth="ops:s3cret")
    d = json.loads(body)
    assert d["sessions"][0]["model"] == "opencode/mimo-v2.6-flash-free"
    code, body = _get(auth_server + "/api/conversations", auth="ops:s3cret")
    d = json.loads(body)
    assert d["conversations"][0]["turns"] == 2
    assert d["conversations"][0]["log"][0]["text"] == "hi"
    code, body = _get(auth_server + "/api/approvals", auth="ops:s3cret")
    assert json.loads(body)["approvals"] == []


def test_open_server_ui_fail_closed(open_server):
    code, body = _get(open_server + "/ui")
    assert code == 503
    assert b"not configured" in body


def test_open_server_legacy_json_survives(open_server):
    code, body = _get(open_server + "/status")
    assert code == 200
    code, body = _get(open_server + "/sessions")
    assert code == 200 and b"bindings" in body
