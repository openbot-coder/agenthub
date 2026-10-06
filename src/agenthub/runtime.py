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
            lambda m: _safe_prompt(mgr, ch.name, m.chat_id, m.text, m.images)
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


def _safe_prompt(mgr: SessionManager, channel: str, chat_id: str, text: str, images=None) -> None:
    def run():
        try:
            mgr.prompt(channel, chat_id, text, images=images)
        except Exception as e:  # noqa: BLE001
            log.error(f"[{channel}:{chat_id}] prompt failed: {e}")
            # surface the failure into the chat via the sink path (best effort)
            try:
                mgr._emit(f"{channel}:{chat_id}", "final", {"text": f"出错了：{e}"})
            except Exception:  # noqa: BLE001
                pass

    # prompt blocks for the whole turn; run in worker thread so the channel
    # receive loop never stalls
    threading.Thread(target=run, name=f"prompt-{channel}-{chat_id}", daemon=True).start()


def _safe_event(ch, key: str, kind: str, payload: dict[str, Any]) -> None:
    try:
        chat_id = key.split(":", 1)[1]
        ch.push_event(chat_id, kind, payload)
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
        http = StatusHTTP(mgr, host=http_cfg.get("listen_host", "127.0.0.1"), port=int(http_cfg.get("port", 30990)))
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
