"""Tests for admin write ops (sqlite-backed CRUD: channel/agent/binding)."""

import base64
import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from agenthub.admin_ops import AdminStore, AdminWriteError, AdminWrites, seed_from_json
from agenthub.http_admin import StatusHTTP
from agenthub.session import AgentSpec

BOOT = {
    "channels": {"wecom": {"bot_id": "b1", "secret": "s1"}},
    "agents": {"opencode": {"cmd": ["opencode", "acp"]}},
}


def make_mgr():
    mgr = MagicMock()
    mgr.agents = {"opencode": AgentSpec(name="opencode", cmd=["opencode", "acp"])}
    mgr.switch.return_value = {"acp_session_id": "sess-1", "migrated": 3}
    mgr.get_binding.return_value = True
    return mgr


@pytest.fixture
def ops(tmp_path):
    boot = tmp_path / "config.json"
    boot.write_text(json.dumps(BOOT))
    store = AdminStore(str(tmp_path / "agenthub.db"))
    seed_from_json(store, str(boot))
    return AdminWrites(make_mgr(), store)


# -- store / seeding --

def test_store_seed_once(tmp_path):
    boot = tmp_path / "config.json"
    boot.write_text(json.dumps(BOOT))
    store = AdminStore(str(tmp_path / "db.sqlite"))
    assert seed_from_json(store, str(boot)) is True
    assert seed_from_json(store, str(boot)) is False  # second call no-op
    assert store.load()["channels"]["wecom"]["bot_id"] == "b1"
    store2 = AdminStore(str(tmp_path / "db2.sqlite"))
    with pytest.raises(AdminWriteError, match="not seeded"):
        store2.load()


def test_store_persists_across_reopen(tmp_path):
    boot = tmp_path / "config.json"
    boot.write_text(json.dumps(BOOT))
    store = AdminStore(str(tmp_path / "db.sqlite"))
    seed_from_json(store, str(boot))
    cfg = store.load()
    cfg["agents"]["x"] = {"cmd": ["x"]}
    store.save(cfg)
    assert "x" in AdminStore(str(tmp_path / "db.sqlite")).load()["agents"]


# -- channel CRUD --

def test_channel_list_masks_secret(ops):
    chans = ops.list_channels()
    assert chans[0]["name"] == "wecom"
    assert "REDACTED" in chans[0]["fields"]["secret"]
    assert chans[0]["fields"]["bot_id"] == "b1"
    assert "s1" not in json.dumps(chans)  # plaintext never leaks


def test_channel_update_blank_secret_keeps_old(ops):
    ops.upsert_channel("wecom", {"bot_id": "b2", "secret": ""})
    doc = ops.store.load()
    assert doc["channels"]["wecom"]["bot_id"] == "b2"
    assert doc["channels"]["wecom"]["secret"] == "s1"  # blank -> preserved


def test_channel_missing_required_rejected(ops):
    with pytest.raises(AdminWriteError, match="token"):
        ops.upsert_channel("weixin", {})


def test_channel_unknown_type_rejected(ops):
    with pytest.raises(AdminWriteError, match="unknown channel"):
        ops.upsert_channel("slack", {"token": "t"})


def test_channel_unknown_field_rejected(ops):
    with pytest.raises(AdminWriteError, match="unknown fields"):
        ops.upsert_channel("wecom", {"bot_id": "b", "secret": "s", "foo": 1})


def test_channel_delete_and_get_missing(ops):
    ops.delete_channel("wecom")
    assert ops.list_channels() == []
    with pytest.raises(AdminWriteError, match="not configured"):
        ops.get_channel("wecom")


# -- agent CRUD --

def test_agent_crud_full_cycle(ops):
    r = ops.upsert_agent("coder", {"cmd": "coder acp", "default_model": "m1"})
    assert r["existed"] is False and r["hot_registered"]
    assert ops.mgr.agents["coder"].default_model == "m1"
    assert ops.get_agent("coder")["cmd"] == "coder acp"
    # update keeps untouched fields (merge semantics)
    ops.upsert_agent("coder", {"cmd": "coder acp --new"})
    a = ops.get_agent("coder")
    assert a["cmd"] == "coder acp --new" and a["default_model"] == "m1"
    ops.delete_agent("coder")
    assert "coder" not in ops.mgr.agents
    with pytest.raises(AdminWriteError, match="not configured"):
        ops.get_agent("coder")


def test_agent_delete_last_rejected(ops):
    doc = ops.store.load()
    doc["agents"] = {"solo": {"cmd": ["x"]}}
    ops.store.save(doc)
    ops.mgr.agents.clear()
    ops.mgr.agents["solo"] = AgentSpec(name="solo", cmd=["x"])
    with pytest.raises(AdminWriteError, match="last agent"):
        ops.delete_agent("solo")


# -- binding --

def test_bind_calls_switch(ops):
    r = ops.bind("wecom", "chat9", "opencode")
    assert r["binding"] == "wecom:chat9" and r["migrated_turns"] == 3
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
    boot = tmp_path / "config.json"
    boot.write_text(json.dumps({"agents": {"a": {"cmd": ["x"]}}, "channels": {}}))
    store = AdminStore(str(tmp_path / "db.sqlite"))
    seed_from_json(store, str(boot))
    mgr = make_mgr()
    mgr.agents = {"a": AgentSpec(name="a", cmd=["x"])}
    mgr.switch.return_value = {"acp_session_id": "s", "migrated": 0}
    http = StatusHTTP(mgr, host="127.0.0.1", port=0,
                      admin_cfg={"username": "u", "password": "p"})
    http.ops = AdminWrites(mgr, store)
    http.start()
    port = http._server.server_address[1]  # type: ignore[union-attr]

    def post(path, body, auth=True):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST")
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(b"u:p").decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    code, _ = post("/api/agent", {"name": "b", "fields": {"cmd": "y"}}, auth=False)
    assert code == 401
    code, j = post("/api/agent", {"name": "b", "fields": {"cmd": "y"}})
    assert code == 200 and j["hot_registered"]
    code, j = post("/api/agent/get", {"name": "b"})
    assert code == 200 and j["cmd"] == "y"
    code, j = post("/api/channel", {"name": "wecom", "fields": {"bot_id": "b"}})
    assert code == 400 and "secret" in j["error"]
    code, j = post("/api/ops-state", {})
    assert code == 200 and j["storage"] == "sqlite"
    http.stop()  # type: ignore[union-attr]
