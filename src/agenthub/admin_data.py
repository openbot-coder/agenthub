"""Admin UI data endpoints + auth for agenthub (added to StatusHTTP).

Auth: HTTP Basic. Credentials live in config["http"]["admin"] = {"username",
"password"} (config.json / agenthub-etc/config.json, 0600 — secrets never in
code or docs). If admin block is absent, UI+data endpoints serve 503 with a
hint (fail-closed); /healthz and /status stay open (nginx already gates them).

New read-only endpoints (all auth-gated):
  GET /api/conversations           -> hub-side conversation text per binding
  GET /api/approvals               -> pending permission requests
  GET /api/sessions                -> bindings + pinned model config
  GET /api/config                  -> sanitized effective config
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def check_auth(header_value: str | None, admin_cfg: dict[str, Any] | None) -> bool:
    """Constant-time Basic auth check. Fail-closed when admin_cfg is None/empty."""
    if not admin_cfg or not admin_cfg.get("username") or not admin_cfg.get("password"):
        return False
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header_value[6:]).decode("utf-8", "replace")
        user, _, pw = raw.partition(":")
    except Exception:  # noqa: BLE001
        return False
    ok_u = hmac.compare_digest(user, str(admin_cfg["username"]))
    ok_p = hmac.compare_digest(pw, str(admin_cfg["password"]))
    return ok_u and ok_p


def sanitize_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Config view for the UI: structure only, secret-looking values redacted."""
    SECRET_KEYS = {"secret", "password", "token", "key", "app_secret", "api_key"}

    def redact(obj: Any, parent_key: str = "") -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if any(s in k.lower() for s in SECRET_KEYS) and isinstance(v, str) and v:
                    out[k] = v[:4] + "…[REDACTED]"
                else:
                    out[k] = redact(v, k)
            return out
        if isinstance(obj, list):
            return [redact(x) for x in obj]
        return obj

    return redact(cfg)


def conversations_payload(mgr: Any) -> dict[str, Any]:
    """Hub-side text logs (SessionRecord.text_log), newest first, tail-limited."""
    items = []
    with mgr._lock:
        for key, rec in getattr(mgr, "_records", {}).items():
            log = rec.text_log[-50:]
            items.append(
                {
                    "binding_key": key,
                    "agent": rec.agent,
                    "acp_session_id": rec.acp_session_id,
                    "updated_at": rec.updated_at,
                    "turns": len(rec.text_log),
                    "log": log,
                }
            )
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"conversations": items}


def approvals_payload(mgr: Any) -> dict[str, Any]:
    """Pending permission requests (live memory state)."""
    now = time.time()
    items = []
    with mgr._lock:
        for rid, p in getattr(mgr, "_pending_permissions", {}).items():
            items.append(
                {
                    "request_id": rid,
                    "agent": p.agent,
                    "session_id": p.session_id,
                    "key": p.key,
                    "tool_title": p.tool_title,
                    "tool_kind": p.tool_kind,
                    "options": p.options,
                    "presented": p.presented,
                    "age_s": round(now - p.created_at, 1),
                    "answered": p.answered.is_set(),
                    "choice": p.choice,
                }
            )
    items.sort(key=lambda x: x["age_s"], reverse=True)
    return {"approvals": items}


def sessions_payload(mgr: Any) -> dict[str, Any]:
    """Bindings enriched with the pinned per-session model (if known)."""
    out = []
    with mgr._lock:
        bindings = list(mgr._bindings.items())
    for (channel, chat_id), b in bindings:
        cfg_model = None
        try:
            sc = mgr._session_config(b)  # {"model": {"current": ..., "options": [...]}}
            cfg_model = sc.get("model", {}).get("current")
        except Exception:  # noqa: BLE001
            pass
        out.append(
            {
                "channel": channel,
                "chat_id": chat_id,
                "agent": b.agent,
                "acp_session_id": b.acp_session_id,
                "model": cfg_model,
                "created_at": b.created_at,
                "last_active": b.last_active,
                "idle_s": round(time.time() - b.last_active, 1),
            }
        )
    out.sort(key=lambda x: x["last_active"], reverse=True)
    return {"sessions": out}
