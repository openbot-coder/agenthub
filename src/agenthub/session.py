"""Session manager: (channel, chat_id) -> agent/process/session binding + idle reaper.

Implements DESIGN §9 semantics:
- one ACP subprocess per agent (shared across bindings of that agent),
  sessions live inside the process
- binding table (channel, chat_id) -> {agent, acp_session_id}
- prompt(): resolve/create binding, run one turn, return turn result;
  deltas route through a per-binding event sink
- /switch semantics: unbind (old session idles out via reaper), optionally
  --continue migrates hub-side conversation text only (tool state does NOT
  migrate across agents)
- idle reaper closes agent processes with no live bindings after idle_ttl_s,
  respecting max_sessions (LRU eviction of unbound clients)
- cwd whitelist enforcement per agent config
- sqlite persistence (optional; in-memory dict when no path given) —
  fact source = DB per DESIGN §8, config.json is only bootstrap
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .acp_client import ACPClient, ACPProcessDead

EventSink = Callable[[str, str, dict[str, Any]], None]  # (binding_key, kind, payload)


# ---------------------------------------------------------------------------
# dataclasses / errors
# ---------------------------------------------------------------------------


@dataclass
class Binding:
    channel: str
    chat_id: str
    agent: str
    acp_session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


@dataclass
class AgentSpec:
    name: str
    cmd: list[str]
    cwd_whitelist: list[str] = field(default_factory=list)
    default_model: str | None = None


@dataclass
class SessionRecord:
    """Hub-side conversation text (for cross-agent --continue migration)."""

    binding_key: str  # "channel:chat_id"
    agent: str
    acp_session_id: str
    text_log: list[dict[str, str]] = field(default_factory=list)  # [{role, text}]
    updated_at: float = field(default_factory=time.time)


class SessionError(Exception):
    pass


class UnknownAgent(SessionError):
    pass


class CwdNotAllowed(SessionError):
    pass


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------


class SessionManager:
    def __init__(
        self,
        agents: dict[str, AgentSpec],
        idle_ttl_s: float = 1800,
        max_sessions: int = 8,
        db_path: str | None = None,
        transport: str = "ndjson",
        client_factory: Callable[[AgentSpec], ACPClient] | None = None,
        logger: Any = None,
    ):
        self.agents = agents
        self.idle_ttl_s = idle_ttl_s
        self.max_sessions = max_sessions
        self.transport = transport
        self._client_factory = client_factory or self._default_client_factory()
        self._logger = logger or _log()

        self._clients: dict[str, ACPClient] = {}  # agent_name -> client
        self._bindings: dict[tuple[str, str], Binding] = {}
        self._records: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self._sink: EventSink | None = None
        self._prompt_locks: dict[tuple[str, str], threading.Lock] = {}

        self._db: sqlite3.Connection | None = None
        if db_path:
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._load_db()

    def _default_client_factory(self) -> Callable[[AgentSpec], ACPClient]:
        def factory(spec: AgentSpec) -> ACPClient:
            client = ACPClient(cmd=spec.cmd, transport=self.transport)
            client.start()
            client.initialize()
            return client

        return factory

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._reaper = threading.Thread(target=self._reap_loop, name="session-reaper", daemon=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()
            self._bindings.clear()

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    def _emit(self, binding_key: str, kind: str, payload: dict[str, Any]) -> None:
        if self._sink:
            self._sink(binding_key, kind, payload)

    # -- binding table / status ----------------------------------------------
    @staticmethod
    def key(channel: str, chat_id: str) -> tuple[str, str]:
        return (channel, chat_id)

    def get_binding(self, channel: str, chat_id: str) -> Binding | None:
        with self._lock:
            return self._bindings.get(self.key(channel, chat_id))

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": a.name, "cmd": a.cmd, "running": a.name in self._clients}
                for a in self.agents.values()
            ]

    def status(self, channel: str, chat_id: str) -> dict[str, Any]:
        b = self.get_binding(channel, chat_id)
        if not b:
            return {"bound": False, "agents": self.list_agents()}
        return {
            "bound": True,
            "agent": b.agent,
            "acp_session_id": b.acp_session_id,
            "last_active": b.last_active,
            "agents": self.list_agents(),
        }

    # -- /switch (DESIGN §9) ---------------------------------------------------
    def switch(self, channel: str, chat_id: str, agent_name: str, cont: bool = False) -> dict[str, Any]:
        """Rebind (channel, chat_id) to another agent; optionally migrate text."""
        if agent_name not in self.agents:
            raise UnknownAgent(f"agent {agent_name!r} not configured")
        key = f"{channel}:{chat_id}"
        with self._lock:
            old = self._bindings.get(self.key(channel, chat_id))
        migrated: list[dict[str, str]] = []
        if old and cont:
            rec = self._records.get(key)
            if rec and rec.agent != agent_name:
                # cross-agent continue: migrate hub-side conversation text ONLY.
                # tool state does NOT migrate (documented limitation, DESIGN §9).
                migrated = [e for e in rec.text_log if e.get("role") in ("user", "assistant")]
        self.unbind(channel, chat_id)
        client, session_id = self._ensure_session(self.key(channel, chat_id), agent_name, cwd=None)
        with self._lock:
            self._bindings[self.key(channel, chat_id)] = Binding(
                channel=channel, chat_id=chat_id, agent=agent_name, acp_session_id=session_id
            )
        for entry in migrated:
            self._append_text(key, agent_name, session_id, entry)
        return {"agent": agent_name, "acp_session_id": session_id, "migrated": len(migrated)}

    # -- core: prompt -----------------------------------------------------------
    def prompt(
        self,
        channel: str,
        chat_id: str,
        text: str,
        agent: str | None = None,
        cwd: str | None = None,
        images: list[bytes] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Run one turn on the bound (or newly created) session."""
        k = self.key(channel, chat_id)
        with self._lock_for(k):  # serialize turns per chat
            if agent and agent not in self.agents:
                raise UnknownAgent(f"agent {agent!r} not configured")
            with self._lock:
                b = self._bindings.get(k)
            if b is None or (agent and b.agent != agent):
                use_agent = agent or self._default_agent()
                client, session_id = self._ensure_session(k, use_agent, cwd)
                with self._lock:
                    b = Binding(channel=channel, chat_id=chat_id, agent=use_agent, acp_session_id=session_id)
                    self._bindings[k] = b
                    self._persist_binding(b)
            client = self._client_for(b.agent)

            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
            for img in images or []:
                blocks.append({"type": "image", "data": _b64(img), "mimeType": "image/png"})
            key = f"{channel}:{chat_id}"
            self._append_text(key, b.agent, b.acp_session_id, {"role": "user", "text": text})
            self._attach_update_relay(b)

            try:
                res = client.session_prompt(b.acp_session_id, blocks, timeout=timeout_s)
            except ACPProcessDead as e:
                # process died: drop client + binding, surface error (M2 adds auto-restart)
                self._drop_client(b.agent)
                with self._lock:
                    self._bindings.pop(k, None)
                raise SessionError(f"session crashed: {e}") from e
            self._append_text(key, b.agent, b.acp_session_id, {"role": "assistant", "text": str(res.get("stopReason", ""))})
            # turn terminal: emit final (mock + real agents that lack turn_end updates)
            self._emit(key, "final", {"stop_reason": res.get("stopReason")})
            with self._lock:
                b.last_active = time.time()
            return {"stop_reason": res.get("stopReason"), "session_id": b.acp_session_id, "agent": b.agent}

    # -- permission ---------------------------------------------------------------
    def permission_reply(self, channel: str, chat_id: str, request_id: str, allow: bool) -> None:
        with self._lock:
            b = self._bindings.get(self.key(channel, chat_id))
        if not b:
            raise SessionError("no binding for permission_reply")
        client = self._client_for(b.agent)
        option = "allow" if allow else "deny"
        try:
            rid = int(request_id)
        except (TypeError, ValueError):
            raise SessionError(f"invalid request_id: {request_id!r}") from None
        with client._lock:
            slot = client._pending.pop(rid, None)
        if slot is None:
            raise SessionError(f"no pending request {rid}")
        ev, holder = slot
        holder.append({"outcome": {"outcome": "selected", "optionId": option}})
        ev.set()

    # -- internal: session/process ensure --------------------------------------------
    def _default_agent(self) -> str:
        names = sorted(self.agents)
        if not names:
            raise SessionError("no agents configured")
        return names[0]

    def _ensure_session(self, k: tuple[str, str], agent_name: str, cwd: str | None) -> tuple[ACPClient, str]:
        spec = self.agents.get(agent_name)
        if spec is None:
            raise UnknownAgent(f"agent {agent_name!r} not configured")
        if cwd:
            _check_cwd(cwd, spec.cwd_whitelist)
        client = self._client_for(agent_name, spec)
        session_id = client.session_new(cwd=cwd or os.path.expanduser("~"))
        return client, session_id

    def _client_for(self, agent_name: str, spec: AgentSpec | None = None) -> ACPClient:
        with self._lock:
            client = self._clients.get(agent_name)
            if client and client.is_alive():
                return client
        if client:
            self._drop_client(agent_name)
        spec = spec or self.agents[agent_name]
        self._evict_if_full(exclude=agent_name)
        self._logger.debug(f"[sessions] starting agent process: {agent_name}")
        client = self._client_factory(spec)
        with self._lock:
            self._clients[agent_name] = client
        return client

    def _evict_if_full(self, exclude: str) -> None:
        with self._lock:
            if len(self._clients) < self.max_sessions:
                return
            bound = {b.agent for b in self._bindings.values()}
            candidates = sorted(
                (getattr(c, "last_active", time.time()), name)
                for name, c in self._clients.items()
                if name != exclude and name not in bound
            )
            if candidates:
                self._drop_client(candidates[0][1])
                return
            # all processes bound: hard-cap by dropping the LRU bound one too
            all_sorted = sorted(
                (getattr(c, "last_active", time.time()), name)
                for name, c in self._clients.items()
                if name != exclude
            )
            if all_sorted:
                self._drop_client(all_sorted[0][1])

    def _drop_client(self, agent_name: str) -> None:
        with self._lock:
            client = self._clients.pop(agent_name, None)
        if client:
            try:
                client.close(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

    def unbind(self, channel: str, chat_id: str) -> None:
        """Remove binding; the agent process idles out via reaper (not killed now)."""
        k = self.key(channel, chat_id)
        with self._lock:
            self._bindings.pop(k, None)
        if self._db is not None:
            self._db.execute("DELETE FROM bindings WHERE channel=? AND chat_id=?", k)
            self._db.commit()

    # -- update relay: ACP session/update -> hub events ---------------------------------
    def _attach_update_relay(self, b: Binding) -> None:
        key = f"{b.channel}:{b.chat_id}"
        session_id = b.acp_session_id

        def handler(method: str, params: dict[str, Any]) -> None:
            if method != "session/update" or params.get("sessionId") != session_id:
                return
            upd = params.get("update", {})
            ut = upd.get("sessionUpdate")
            if ut == "agent_message_chunk":
                text = (upd.get("content") or {}).get("text", "")
                self._emit(key, "delta", {"text": text})
            elif ut == "agent_thought_chunk":
                self._emit(key, "thinking", {"text": (upd.get("content") or {}).get("text", "")})
            elif ut == "tool_call":
                self._emit(key, "tool_start", {"tool": upd.get("title") or upd.get("toolCallId"), "payload": upd})
            elif ut == "tool_call_update":
                self._emit(key, "tool_end", {"tool": upd.get("title") or upd.get("toolCallId"), "payload": upd})
            elif ut == "plan":
                self._emit(key, "plan", {"entries": upd.get("entries", [])})

        client = self._client_for(b.agent)
        client.register_notification_handler(handler)

    # -- hub-side text log ----------------------------------------------------------------
    def _append_text(self, key: str, agent: str, session_id: str, entry: dict[str, str]) -> None:
        with self._lock:
            rec = self._records.setdefault(key, SessionRecord(binding_key=key, agent=agent, acp_session_id=session_id))
            rec.text_log.append(entry)
            rec.text_log = rec.text_log[-500:]
            rec.updated_at = time.time()
            rec.agent, rec.acp_session_id = agent, session_id
        self._persist_record(rec)

    # -- reaper ------------------------------------------------------------------------------
    def _reap_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._reap_once()
            except Exception as e:  # noqa: BLE001
                self._logger.warning(f"[sessions] reap error: {e}")

    def _reap_once(self) -> None:
        now = time.time()
        with self._lock:
            bound = {b.agent for b in self._bindings.values()}
            victims = [k for k, b in self._bindings.items() if now - b.last_active > self.idle_ttl_s]
            remaining = {b.agent for k, b in self._bindings.items() if k not in set(victims)}
            idle_clients = [n for n in self._clients if n not in remaining]
        for k in victims:
            self._logger.info(f"[sessions] idle binding expired: {k}")
            self.unbind(*k)
        for name in idle_clients:
            self._logger.info(f"[sessions] idle reaper closing unbound agent process: {name}")
            self._drop_client(name)

    def _lock_for(self, k: tuple[str, str]) -> threading.Lock:
        with self._lock:
            return self._prompt_locks.setdefault(k, threading.Lock())

    # -- sqlite persistence ---------------------------------------------------------------------
    def _init_db(self) -> None:
        assert self._db is not None
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bindings (
                channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                agent TEXT NOT NULL, acp_session_id TEXT NOT NULL,
                updated_at REAL, PRIMARY KEY (channel, chat_id));
            CREATE TABLE IF NOT EXISTS session_records (
                binding_key TEXT PRIMARY KEY, agent TEXT,
                acp_session_id TEXT, text_log TEXT, updated_at REAL);
            """
        )
        self._db.commit()

    def _load_db(self) -> None:
        assert self._db is not None
        for channel, chat_id, agent, sid in self._db.execute(
            "SELECT channel, chat_id, agent, acp_session_id FROM bindings"
        ):
            self._bindings[(channel, chat_id)] = Binding(channel, chat_id, agent, sid)
        for key, agent, sid, log_json in self._db.execute(
            "SELECT binding_key, agent, acp_session_id, text_log FROM session_records"
        ):
            self._records[key] = SessionRecord(
                binding_key=key, agent=agent, acp_session_id=sid, text_log=json.loads(log_json or "[]")
            )

    def _persist_binding(self, b: Binding) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO bindings(channel,chat_id,agent,acp_session_id,updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(channel,chat_id) DO UPDATE SET agent=?,acp_session_id=?,updated_at=?",
            (b.channel, b.chat_id, b.agent, b.acp_session_id, b.last_active,
             b.agent, b.acp_session_id, b.last_active),
        )
        self._db.commit()

    def _persist_record(self, rec: SessionRecord) -> None:
        if self._db is None:
            return
        log_json = json.dumps(rec.text_log, ensure_ascii=False)
        self._db.execute(
            "INSERT INTO session_records(binding_key,agent,acp_session_id,text_log,updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(binding_key) DO UPDATE SET agent=?,acp_session_id=?,text_log=?,updated_at=?",
            (rec.binding_key, rec.agent, rec.acp_session_id, log_json, rec.updated_at,
             rec.agent, rec.acp_session_id, log_json, rec.updated_at),
        )
        self._db.commit()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_cwd(cwd: str, whitelist: list[str]) -> None:
    """Enforce the agent's cwd whitelist (fnmatch patterns, ~ expanded)."""
    if not whitelist:
        return
    real = os.path.realpath(os.path.expanduser(cwd))
    for pattern in whitelist:
        expanded = os.path.expanduser(pattern)
        if fnmatch.fnmatch(real, os.path.realpath(expanded)):
            return
        # pattern like ~/projects/* also matches base dir itself
        base = expanded.rstrip("*").rstrip("/")
        base_real = os.path.realpath(base)
        if real == base_real or real.startswith(base_real + os.sep):
            return
    raise CwdNotAllowed(f"cwd {cwd!r} not in whitelist {whitelist}")


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def _log():
    import logging

    return logging.getLogger("agenthub.sessions")
