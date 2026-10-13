"""Admin write operations — sqlite-backed config CRUD.

Config lives in agenthub.db table `config` (single JSON doc, id=1).
The JSON boot file is only used to seed the doc on first run.

CRUD semantics:
  - list/get: secrets masked (UI tables/forms never see plaintext)
  - upsert: blank/omitted secret fields merge with existing values
    (so the edit form can leave them empty)
  - delete: guarded (last agent protected; unknown name -> 400)
  - channels need supervisor restart to take effect (long-lived IM
    connections); agents hot-register into the live SessionManager
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from .session import AgentSpec

VALID_CHANNELS = {"wecom", "wecom-app", "weixin"}

CHANNEL_REQUIRED: dict[str, list[str]] = {
    "wecom": ["bot_id", "secret"],
    "wecom-app": ["corp_id", "secret", "agent_id"],
    "weixin": ["token"],
}
CHANNEL_OPTIONAL: dict[str, list[str]] = {
    "wecom": ["mock_transport"],
    "wecom-app": [],
    "weixin": [],
}
AGENT_REQUIRED = ["cmd"]
AGENT_OPTIONAL = ["cwd_whitelist", "default_model"]
SECRET_KEYS = {"secret", "token"}


class AdminWriteError(ValueError):
    pass


class AdminStore:
    """sqlite-backed config document store (agenthub.db table `config`)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS config ("
            "id INTEGER PRIMARY KEY CHECK(id=1), doc TEXT NOT NULL)"
        )
        self._db.commit()

    def load(self) -> dict[str, Any]:
        row = self._db.execute("SELECT doc FROM config WHERE id=1").fetchone()
        if row is None:
            raise AdminWriteError("config not seeded")
        return json.loads(row[0])

    def save(self, cfg: dict[str, Any]) -> None:
        doc = json.dumps(cfg, ensure_ascii=False, indent=2)
        with self._lock:
            self._db.execute(
                "INSERT INTO config(id,doc) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET doc=excluded.doc",
                (doc,),
            )
            self._db.commit()

    def seeded(self) -> bool:
        return self._db.execute("SELECT 1 FROM config WHERE id=1").fetchone() is not None


def seed_from_json(store: AdminStore, json_path: str) -> bool:
    """First-run bootstrap: copy config.json into sqlite. True if seeded now."""
    if store.seeded():
        return False
    with open(json_path, encoding="utf-8") as f:
        store.save(json.load(f))
    return True


class AdminWrites:
    """CRUD over the sqlite config doc + live session manager."""

    def __init__(self, mgr: Any, store: AdminStore):
        self.mgr = mgr
        self.store = store
        self.pending_restart = False

    # -- helpers ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        return self.store.load()

    def _save(self, cfg: dict[str, Any]) -> None:
        self.store.save(cfg)

    @staticmethod
    def _mask(v: Any) -> str:
        s = str(v)
        return (s[:2] + "…[REDACTED]") if len(s) > 6 else "[REDACTED]"

    @staticmethod
    def _clean_fields(kind: str, fields: dict[str, Any], required: list[str],
                      optional: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in required:
            v = fields.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                raise AdminWriteError(f"{kind}: missing required field {k!r}")
            if isinstance(v, (int, float, list)):
                out[k] = v
            else:
                out[k] = str(v).strip()
        for k in optional:
            if fields.get(k) not in (None, ""):
                out[k] = fields[k]
        unknown = set(fields) - set(required) - set(optional)
        if unknown:
            raise AdminWriteError(f"{kind}: unknown fields {sorted(unknown)}")
        return out

    # -- CHANNEL CRUD -------------------------------------------------------

    def list_channels(self) -> list[dict[str, Any]]:
        ccfg = self._load().get("channels", {})
        return [
            {
                "name": name,
                "fields": {
                    k: (self._mask(v) if k in SECRET_KEYS else v)
                    for k, v in fields.items()
                },
            }
            for name, fields in sorted(ccfg.items())
        ]

    def get_channel(self, name: str) -> dict[str, Any]:
        fields = self._load().get("channels", {}).get(name)
        if fields is None:
            raise AdminWriteError(f"channel {name!r} not configured")
        return {
            "name": name,
            "fields": {k: (self._mask(v) if k in SECRET_KEYS else v) for k, v in fields.items()},
        }

    def upsert_channel(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        if name not in VALID_CHANNELS:
            raise AdminWriteError(f"unknown channel {name!r}; valid: {sorted(VALID_CHANNELS)}")
        cfg = self._load()
        merged = dict(cfg.get("channels", {}).get(name, {}))
        for k, v in fields.items():
            if v is None or (isinstance(v, str) and not v.strip()):
                continue  # blank = keep existing (edit form leaves secrets empty)
            merged[k] = v
        cleaned = self._clean_fields(f"channel {name}", merged,
                                     CHANNEL_REQUIRED[name], CHANNEL_OPTIONAL[name])
        existed = name in cfg.get("channels", {})
        cfg.setdefault("channels", {})[name] = cleaned
        self._save(cfg)
        self.pending_restart = True
        return {"channel": name, "existed": existed, "restart_required": True}

    def delete_channel(self, name: str) -> dict[str, Any]:
        cfg = self._load()
        if name not in cfg.get("channels", {}):
            raise AdminWriteError(f"channel {name!r} not configured")
        del cfg["channels"][name]
        self._save(cfg)
        self.pending_restart = True
        return {"channel": name, "deleted": True, "restart_required": True}

    # -- AGENT CRUD ---------------------------------------------------------

    def list_agents(self) -> list[dict[str, Any]]:
        acfg = self._load().get("agents", {})
        return [
            {
                "name": name,
                "cmd": " ".join(s["cmd"]) if isinstance(s.get("cmd"), list) else s.get("cmd", ""),
                "default_model": s.get("default_model") or "",
                "cwd_whitelist": ", ".join(s.get("cwd_whitelist") or []),
            }
            for name, s in sorted(acfg.items())
        ]

    def get_agent(self, name: str) -> dict[str, Any]:
        s = self._load().get("agents", {}).get(name)
        if s is None:
            raise AdminWriteError(f"agent {name!r} not configured")
        return {
            "name": name,
            "cmd": " ".join(s["cmd"]) if isinstance(s.get("cmd"), list) else s.get("cmd", ""),
            "default_model": s.get("default_model") or "",
            "cwd_whitelist": ", ".join(s.get("cwd_whitelist") or []),
        }

    def upsert_agent(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not name or "/" in name or " " in name:
            raise AdminWriteError(f"invalid agent name {name!r}")
        cfg = self._load()
        merged = dict(cfg.get("agents", {}).get(name, {}))
        for k, v in fields.items():
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            merged[k] = v
        if isinstance(merged.get("cmd"), str):
            merged["cmd"] = merged["cmd"].split()
        wl = merged.get("cwd_whitelist")
        if wl is not None and not isinstance(wl, list):
            merged["cwd_whitelist"] = [p.strip() for p in str(wl).split(",") if p.strip()]
        cleaned = self._clean_fields(f"agent {name}", merged, AGENT_REQUIRED, AGENT_OPTIONAL)
        existed = name in cfg.get("agents", {})
        cfg.setdefault("agents", {})[name] = cleaned
        self._save(cfg)
        self.mgr.agents[name] = AgentSpec(
            name=name, cmd=cleaned["cmd"],
            cwd_whitelist=cleaned.get("cwd_whitelist", []),
            default_model=cleaned.get("default_model"),
        )
        return {"agent": name, "existed": existed, "hot_registered": True}

    def delete_agent(self, name: str) -> dict[str, Any]:
        cfg = self._load()
        if name not in cfg.get("agents", {}):
            raise AdminWriteError(f"agent {name!r} not configured")
        if len(cfg["agents"]) == 1:
            raise AdminWriteError("cannot delete the last agent")
        del cfg["agents"][name]
        self._save(cfg)
        self.mgr.agents.pop(name, None)
        return {"agent": name, "deleted": True}

    # -- BINDING CRUD -------------------------------------------------------

    def bind(self, channel: str, chat_id: str, agent: str,
             continue_history: bool = True) -> dict[str, Any]:
        if not channel or not chat_id:
            raise AdminWriteError("channel and chat_id are required")
        if agent not in self.mgr.agents:
            raise AdminWriteError(f"agent {agent!r} not configured; known: {sorted(self.mgr.agents)}")
        res = self.mgr.switch(channel, chat_id, agent, cont=continue_history)
        return {"binding": f"{channel}:{chat_id}", "agent": agent,
                "acp_session_id": res.get("acp_session_id"), "migrated_turns": res.get("migrated", 0)}

    def unbind(self, channel: str, chat_id: str) -> dict[str, Any]:
        if self.mgr.get_binding(channel, chat_id) is None:
            raise AdminWriteError(f"no binding for {channel}:{chat_id}")
        self.mgr.unbind(channel, chat_id)
        return {"binding": f"{channel}:{chat_id}", "deleted": True}

    # -- overview -----------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "channels": self.list_channels(),
            "agents": self.list_agents(),
            "pending_restart": self.pending_restart,
            "storage": "sqlite",
        }
