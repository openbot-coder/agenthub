"""agenthub runtime: wires IM channels + (optional NATS) to the SessionManager.

Bootstrap order:
  config.json -> channels + SessionManager -> event sink wiring -> start

Usage:
  agenthub --config /etc/agenthub/config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import threading
from typing import Any

from .channels.wecom import WecomAibotChannel
from .channels.wecom_app import WecomAppChannel
from .channels.weixin import WeixinChannel
from .session import AgentSpec, SessionManager

log = logging.getLogger("agenthub")


def load_config(path: str) -> dict[str, Any]:
    with open(path) as f:
        text = f.read()
    # tolerate JSONC (comments) like opencode does
    import re

    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return json.loads(text)


def build_manager(cfg: dict[str, Any]) -> SessionManager:
    agents = {
        name: AgentSpec(
            name=name,
            cmd=spec["cmd"],
            cwd_whitelist=spec.get("cwd_whitelist", []),
            default_model=spec.get("default_model"),
        )
        for name, spec in cfg.get("agents", {}).items()
    }
    if not agents:
        raise SystemExit("config: no agents defined")
    sess = cfg.get("session", {})
    db_path = sess.get("db_path")  # missing -> in-memory
    return SessionManager(
        agents=agents,
        idle_ttl_s=float(sess.get("idle_ttl_s", 1800)),
        max_sessions=int(sess.get("max_sessions", 8)),
        db_path=db_path,
    )


def build_channels(cfg: dict[str, Any], mgr: SessionManager) -> list:
    channels: list = []
    ccfg = cfg.get("channels", {})

    def sink_and_inbound(ch):
        # hub events -> channel outbound; channel inbound -> hub prompt
        ch.on_message(
            lambda m: _safe_inbound(mgr, ch, m)
        )
        mgr.set_event_sink(
            lambda key, kind, payload: _safe_event(ch, key, kind, payload)
        )
        channels.append(ch)

    if "wecom" in ccfg:
        from .channels.wecom_ws import make_ws_factory

        wcfg = ccfg["wecom"]
        # real long-connection transport unless the config says otherwise
        ws_factory = None if wcfg.get("mock_transport") else make_ws_factory()
        sink_and_inbound(WecomAibotChannel(wcfg, ws_factory=ws_factory))
    if "wecom-app" in ccfg:
        sink_and_inbound(WecomAppChannel(ccfg["wecom-app"]))
    if "weixin" in ccfg:
        sink_and_inbound(WeixinChannel(ccfg["weixin"]))
    return channels


def _safe_inbound(mgr: SessionManager, ch, m) -> None:
    """One inbound IM message: ack -> command dispatch OR agent prompt.

    ack = an immediate 'received' event so the upstream IM can react per its
    own conventions (WeCom: first stream frame '处理中…'; NATS: ack frame).
    """
    def run():
        try:
            # 1) immediate ack: status event -> channel may show typing/receipt
            mgr._emit(f"{ch.name}:{m.chat_id}", "ack", {
                "channel": ch.name, "chat_id": m.chat_id,
                "user_id": m.user_id, "text_len": len(m.text or ""),
            })
            # 2) pending interaction answer (approval / question): the numbered
            # or named reply routes to permission_reply, NOT to the agent.
            ans = mgr.take_interaction_answer(ch.name, m.chat_id, m.text or "")
            if ans == "consumed":
                mgr._emit(f"{ch.name}:{m.chat_id}", "status", {"text": "已收到你的选择，继续处理…"})
                return
            if ans == "not_an_answer":
                # pending interaction exists but text matches no option: prompt
                # the user instead of silently forwarding to the agent
                inter = mgr.pending_interaction(ch.name, m.chat_id) or {}
                hint = "、".join(
                    f"{i+1}. {o.get('name', o.get('option_id'))}"
                    for i, o in enumerate(inter.get("options", []))
                )
                mgr._emit(f"{ch.name}:{m.chat_id}", "final", {
                    "text": f"当前有待选择的请求（{inter.get('tool', '')}），请回复：{hint}，或 r 拒绝。"
                })
                return
            # 3) commands
            if (m.text or "").strip().startswith("/"):
                res = mgr.handle_command(ch.name, m.chat_id, m.text)
                if res is not None:
                    _safe_event(ch, f"{ch.name}:{m.chat_id}", "command_result", res)
                    return
            # 3) normal prompt
            mgr.prompt(ch.name, m.chat_id, m.text, images=m.images)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("agenthub").error(f"[{ch.name}:{m.chat_id}] inbound failed: {e}")
            friendly = _friendly_agent_error(str(e))
            try:
                mgr._emit(f"{ch.name}:{m.chat_id}", "final", {"text": friendly})
            except Exception:  # noqa: BLE001
                pass

    # prompt may block for the whole turn; run in a worker thread so the
    # channel receive loop never stalls
    threading.Thread(target=run, name=f"inbound-{ch.name}-{m.chat_id}", daemon=True).start()


def _friendly_agent_error(raw: str) -> str:
    """Translate agent/ACP errors into actionable chat text (keep original in logs)."""
    low = raw.lower()
    if "tool call delta is missing id or name" in low:
        return (
            "⚠️ 模型服务这次调用出了点问题（上游返回的工具调用流不完整，多为免费模型偶发）。\n"
            "请重发一次消息；若反复出现，发 /model 列表换一个模型再试。"
        )
    if "session not found" in low:
        return "⚠️ 会话已失效（服务重启或超时）。已自动重置，请重新发送消息。"
    if "timeout waiting for" in low:
        return "⚠️ 任务执行超时。任务可能还在后台继续，可稍后追问；或换个更简单的问法。"
    return f"⚠️ 出错了：{raw}"


def _safe_event(ch, key: str, kind: str, payload: dict[str, Any]) -> None:
    try:
        target, chat_id = key.split(":", 1)
        if target == ch.name and chat_id != "__channel__":
            ch.push_event(chat_id, kind, payload)
        # channel_state (and other chatless events) are recorded on the
        # channel object for the /status endpoint, not pushed per-chat
        elif kind == "channel_state":
            ch.connection_state = payload.get("state", ch.connection_state)
            ch.connection_error = payload.get("error", "")
    except Exception as e:  # noqa: BLE001
        log.warning(f"[{ch.name}] push_event {kind} failed: {e}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agenthub")
    ap.add_argument("--config", default=os.environ.get("AGENTHUB_CONFIG", "config.json"))
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("AGENTHUB_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    mgr = build_manager(cfg)
    chans = build_channels(cfg, mgr)
    if not chans:
        log.warning("no IM channels configured; running session manager only")

    mgr.start()
    for ch in chans:
        ch.start()
        log.info(f"channel started: {ch.name}")

    # status HTTP for nginx /agents reverse proxy
    try:
        from .http_admin import StatusHTTP

        http_cfg = cfg.get("http", {})
        http = StatusHTTP(
            mgr,
            host=http_cfg.get("listen_host", "127.0.0.1"),
            port=int(http_cfg.get("port", 30990)),
            admin_cfg=http_cfg.get("admin"),  # None -> UI/api fail-closed 503
        )
        http.channels = chans  # /status reports per-channel connection state
        http.effective_cfg = cfg  # /api/config (sanitized at render time)
        http.start()
        log.info(f"status http on {http.host}:{http.port}")
    except Exception as e:  # noqa: BLE001
        log.warning(f"status http failed to start: {e}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("agenthub running; Ctrl-C to stop")
    stop.wait()
    log.info("shutting down")
    for ch in chans:
        ch.stop()
    mgr.stop()


if __name__ == "__main__":
    main()
