"""Tests for admin write ops (channel/agent config + binding)."""

import json
import threading
from unittest.mock import MagicMock

import pytest

from agenthub.admin_ops import AdminWriteError, AdminWrites
from agenthub.http_admin import StatusHTTP
from agenthub.session import AgentSpec


@pytest.fixture
def ops(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "channels": {"wecom": {"bot_id": "b1", "secret": "s1"}},
        "agents": {"opencode": {"cmd": ["opencode", "acp"]}},
    }))
    mgr = MagicMock()
    mgr.agents = {"opencode": AgentSpec(name="opencode", cmd=["opencode", "acp"])}
    mgr.switch.return_value = {"acp_session_id": "sess-1", "migrated": 3}
    mgr.get_binding.return_value = True
    return AdminWrites(mgr, str(cfg))


# -- channel --

def test_channel_upsert_new_and_update(ops):
    r = ops.upsert_channel("weixin", {"token": "tok123"})
    assert r["existed"] is False and r["restart_required"]
    r2 = ops.upsert_channel("weixin", {"token": "tok456"})
    assert r2["existed"] is True
    cfg = json.load(open(ops.config_path))
    assert cfg["channels"]["weixin"]["token"] == "tok456"


def test_channel_missing_required_rejected(ops):
    with pytest.raises(AdminWriteError, match="secret"):
        ops.upsert_channel("wecom", {"bot_id": "x"})


def test_channel_unknown_type_rejected(ops):
    with pytest.raises(AdminWriteError, match="unknown channel"):
        ops.upsert_channel("slack", {"token": "t"})


def test_channel_unknown_field_rejected(ops):
    with pytest.raises(AdminWriteError, match="unknown fields"):
        ops.upsert_channel("wecom", {"bot_id": "b", "secret": "s", "foo": 1})


def test_channel_delete(ops):
    ops.delete_channel("wecom")
    assert "wecom" not in json.load(open(ops.config_path))["channels"]


# -- agent --

def test_agent_upsert_hot_registers(ops):
    r = ops.upsert_agent("coder", {"cmd": "coder acp", "default_model": "m1"})
    assert r["hot_registered"]
    assert "coder" in ops.mgr.agents
    assert ops.mgr.agents["coder"].default_model == "m1"
    cfg = json.load(open(ops.config_path))
    assert cfg["agents"]["coder"]["cmd"] == ["coder", "acp"]


def test_agent_delete_last_rejected(ops):
    ops2_cfg = json.load(open(ops.config_path))
    ops2_cfg["agents"] = {"solo": {"cmd": ["x"]}}
    json.dump(ops2_cfg, open(ops.config_path, "w"))
    ops.mgr.agents.clear()
    ops.mgr.agents["solo"] = AgentSpec(name="solo", cmd=["x"])
    with pytest.raises(AdminWriteError, match="last agent"):
        ops.delete_agent("solo")


# -- bind --

def test_bind_calls_switch(ops):
    r = ops.bind("wecom", "chat9", "opencode")
    assert r["binding"] == "wecom:chat9"
    assert r["migrated_turns"] == 3
    ops.mgr.switch.assert_called_once_with("wecom", "chat9", "opencode", cont=True)


def test_bind_unknown_agent_rejected(ops):
    with pytest.raises(AdminWriteError, match="not configured"):
        ops.bind("wecom", "c", "ghost")


def test_unbind_missing_binding_rejected(ops):
    ops.mgr.get_binding.return_value = None
    with pytest.raises(AdminWriteError, match="no binding"):
        ops.unbind("wecom", "c")


# -- HTTP layer --

def test_http_ops_endpoints_auth_gated(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"a": {"cmd": ["x"]}}, "channels": {}}))
    mgr = MagicMock()
    mgr.agents = {"a": AgentSpec(name="a", cmd=["x"])}
    mgr.switch.return_value = {"acp_session_id": "s", "migrated": 0}
    mgr.get_binding.return_value = None
    http = StatusHTTP(mgr, host="127.0.0.1", port=0, admin_cfg={"username": "u", "password": "p"})
    from agenthub.admin_ops import AdminWrites
    http.ops = AdminWrites(mgr, str(cfg))
    http.start()
    port = http._server.server_address[1]  # type: ignore[union-attr]
    import base64
    import urllib.error
    import urllib.request

    def post(path, body, auth=True):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST"
        )
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(b"u:p").decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # no auth -> 401
    code, _ = post("/api/agent", {"name": "b", "fields": {"cmd": "y"}}, auth=False)
    assert code == 401
    # with auth -> creates agent
    code, j = post("/api/agent", {"name": "b", "fields": {"cmd": "y"}})
    assert code == 200 and j["hot_registered"]
    # validation error -> 400
    code, j = post("/api/channel", {"name": "wecom", "fields": {"bot_id": "b"}})
    assert code == 400 and "secret" in j["error"]
    http.stop()  # type: ignore[union-attr]
