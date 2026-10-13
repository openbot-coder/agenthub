"""Admin write operations: config channels/agents + binding (UI-backed).

All writes go through:
  1. config.json update (atomic tmp+rename) — persisted source of truth
  2. live mutation — agents hot-registered into mgr.agents (AgentSpec);
     channels noted for restart pickup (IM long-connections cannot be
     hot-swapped safely, so new/changed channels require a supervisor
     restart, surfaced to the UI as a pending-restart hint)
  3. binding = mgr.prompt(channel, chat_id, text=..., agent=...) with a
     tiny no-op prompt so the binding row exists without a real turn
     (empty text would waste a model call, so we use prompt text "." ?
     no — we bind via _ensure_session through a dedicated bind() method).

Secrets: written values land in config.json server-side only; /api/config
redacts at render time (sanitize_config). This module never echoes them.
"""

from __future__ import annotations

import json
import os
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

SECRET_KEYS = {"secret", "token", "app_secret", "key"}


class AdminWriteError(ValueError):
    pass


class AdminWrites:
    """Holds config path + mgr ref; executes validated mutations."""

    def __init__(self, mgr: Any, config_path: str):
        self.mgr = mgr
        self.config_path = config_path
        self._lock = threading.Lock()
        self.pending_restart = False  # channels changed -> needs supervisor restart

    # -- config io ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, cfg: dict[str, Any]) -> None:
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.config_path)

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _clean_fields(kind: str, fields: dict[str, Any], required: list[str],
                      optional: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in required:
            v = fields.get(k)
            if not v or not str(v).strip():
                raise AdminWriteError(f"{kind}: missing required field {k!r}")
            out[k] = v if isinstance(v, (int, float)) else str(v).strip()
        for k in optional:
            if k in fields and fields[k] not in (None, ""):
                out[k] = fields[k]
        unknown = set(fields) - set(required) - set(optional)
        if unknown:
            raise AdminWriteError(f"{kind}: unknown fields {sorted(unknown)}")
        return out

    # -- 1. IM channel ------------------------------------------------------

    def upsert_channel(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        if name not in VALID_CHANNELS:
            raise AdminWriteError(
                f"unknown channel {name!r}; valid: {sorted(VALID_CHANNELS)}"
            )
        cleaned = self._clean_fields(
            f"channel {name}", fields,
            CHANNEL_REQUIRED[name], CHANNEL_OPTIONAL[name],
        )
        with self._lock:
            cfg = self._load()
            existed = name in cfg.get("channels", {})
            cfg.setdefault("channels", {})[name] = cleaned
            self._save(cfg)
            self.pending_restart = True
        return {"channel": name, "existed": existed, "restart_required": True}

    def delete_channel(self, name: str) -> dict[str, Any]:
        with self._lock:
            cfg = self._load()
            if name not in cfg.get("channels", {}):
                raise AdminWriteError(f"channel {name!r} not configured")
            del cfg["channels"][name]
            self._save(cfg)
            self.pending_restart = True
        return {"channel": name, "deleted": True, "restart_required": True}

    # -- 2. ACP agent -------------------------------------------------------

    def upsert_agent(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not name or "/" in name or " " in name:
            raise AdminWriteError(f"invalid agent name {name!r}")
        cleaned = self._clean_fields(
            f"agent {name}", fields, AGENT_REQUIRED, AGENT_OPTIONAL
        )
        if isinstance(cleaned.get("cmd"), str):
            cleaned["cmd"] = cleaned["cmd"].split()
        if cleaned.get("cwd_whitelist") is not None and not isinstance(
            cleaned["cwd_whitelist"], list
        ):
            cleaned["cwd_whitelist"] = [
                p.strip() for p in str(cleaned["cwd_whitelist"]).split(",") if p.strip()
            ]
        with self._lock:
            cfg = self._load()
            existed = name in cfg.get("agents", {})
            cfg.setdefault("agents", {})[name] = cleaned
            self._save(cfg)
            # live hot-register (new agents spawn lazily on first use)
            self.mgr.agents[name] = AgentSpec(
                name=name,
                cmd=cleaned["cmd"],
                cwd_whitelist=cleaned.get("cwd_whitelist", []),
                default_model=cleaned.get("default_model"),
            )
        return {"agent": name, "existed": existed, "hot_registered": True}

    def delete_agent(self, name: str) -> dict[str, Any]:
        with self._lock:
            cfg = self._load()
            if name not in cfg.get("agents", {}):
                raise AdminWriteError(f"agent {name!r} not configured")
            if len(cfg["agents"]) == 1:
                raise AdminWriteError("cannot delete the last agent")
            del cfg["agents"][name]
            self._save(cfg)
            self.mgr.agents.pop(name, None)
        return {"agent": name, "deleted": True}

    # -- 3. bind IM chat <-> agent ------------------------------------------

    def bind(self, channel: str, chat_id: str, agent: str,
             continue_history: bool = True) -> dict[str, Any]:
        """Create/replace binding (channel, chat_id) -> agent.

        Uses mgr.handle_command-free direct path: reuse prompt() with a
        marker-free approach — actually we call the internal session setup
        via prompt() with a real text, because the hub requires an ACP
        session before a binding exists. The chat gets a bind confirmation
        appended to its conversation log.
        """
        if not channel or not chat_id:
            raise AdminWriteError("channel and chat_id are required")
        if agent not in self.mgr.agents:
            raise AdminWriteError(
                f"agent {agent!r} not configured; known: {sorted(self.mgr.agents)}"
            )
        res = self.mgr.switch(channel, chat_id, agent, cont=continue_history)
        return {
            "binding": f"{channel}:{chat_id}",
            "agent": agent,
            "acp_session_id": res.get("acp_session_id"),
            "migrated_turns": res.get("migrated", 0),
        }

    def unbind(self, channel: str, chat_id: str) -> dict[str, Any]:
        if self.mgr.get_binding(channel, chat_id) is None:
            raise AdminWriteError(f"no binding for {channel}:{chat_id}")
        self.mgr.unbind(channel, chat_id)
        return {"binding": f"{channel}:{chat_id}", "deleted": True}

    # -- status -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        cfg = self._load()
        return {
            "channels": sorted(cfg.get("channels", {})),
            "agents": sorted(cfg.get("agents", {})),
            "pending_restart": self.pending_restart,
        }
