"""NATS channel for agenthub: request/reply over NATS subjects.

M1 channel. NATS is a peer front-end (same IMChannel contract as wecom/
weixin); agenthub is a NATS client. Protocol:

- inbound:  subscribe to ``subject_prefix`` (default ``agenthub.in.>``).
  Message body = JSON {"chat_id", "user_id"?, "text"} (extra fields kept
  in raw). If the message carries a ``reply`` subject, final answers are
  published back to it; otherwise to ``subject_prefix`` with ``.out``
  semantics (see below).
- outbound: reply(chat_id, text) publishes JSON {"chat_id", "text"} to
  ``out_subject`` (default ``agenthub.out.<chat_id>``).

Channel name is "nats"; chat keys become "nats:<chat_id>" downstream.

Config (dict):
    servers: str = "nats://localhost:4222"   # comma separated
    username / password: str = ""
    in_subject: str = "agenthub.in.>"        # subscription pattern
    out_subject: str = "agenthub.out"        # replies published to out_subject.<chat_id>
    tls_ca: str = ""                     # CA cert path (enable TLS if set)
    tls_insecure: bool = False           # skip cert verification (dev only)
    allowed_users: list[str] = []

Requires the ``nats`` extra (nats-py). Reuses octop-gateway's SyncNATS
wrapper (vendored copy, asyncio-only nats-py behind a thread).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from .base import (
    IMChannel,
    InboundMessage,
    _command_result_text,
    _interaction_text,
    clean_control_chars,
    split_message_text,
)

log = logging.getLogger("agenthub.nats")

MAX_TEXT_BYTES = 900_000  # NATS default max_payload is 1MB; stay under it


def _log():
    return logging.getLogger("agenthub.nats")


class NatsChannel:
    name = "nats"

    def __init__(
        self,
        config: dict[str, Any],
        nats_factory: Callable[..., Any] | None = None,
        logger: Any = None,
    ):
        self.servers = (config.get("servers") or "nats://localhost:4222").strip()
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.in_subject = config.get("in_subject", "agenthub.in.>")
        self.out_subject = (config.get("out_subject") or "agenthub.out").rstrip(".")
        self.tls_ca = config.get("tls_ca", "")
        self.tls_insecure = bool(config.get("tls_insecure", False))
        self.allowed_users = set(config.get("allowed_users") or [])
        self._nats_factory = nats_factory  # injectable for tests
        self._nc: Any = None
        self._sub_id: str | None = None
        self._msg_cb: Callable[[InboundMessage], None] | None = None
        self._sink: Callable[[str, str, dict], None] | None = None
        self._logger = logger or _log()
        self._stop = threading.Event()

    # -- IMChannel ---------------------------------------------------------
    def start(self) -> None:
        from ._nats_sync import SyncNATS  # vendored sync wrapper

        kwargs: dict[str, Any] = {}
        if self.tls_ca or self.tls_insecure:
            import ssl

            ctx = ssl.create_default_context(
                cafile=self.tls_ca or None
            )
            if self.tls_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs["tls"] = ctx

        factory = self._nats_factory or SyncNATS
        self._nc = factory(
            self.servers,
            username=self.username,
            password=self.password,
            on_connect=lambda: self._logger.info("[nats] connected"),
            on_disconnect=lambda: self._logger.warning("[nats] disconnected; reconnecting"),
            **kwargs,
        )
        self._sub_id = self._nc.subscribe(self.in_subject, self._on_nats_msg)
        self._logger.info(f"[nats] subscribed {self.in_subject} (out: {self.out_subject}.*)")

    def stop(self) -> None:
        self._stop.set()
        if self._nc:
            try:
                if self._sub_id:
                    self._nc.unsubscribe(self._sub_id)
                self._nc.close()
            except Exception as e:  # noqa: BLE001
                self._logger.warning(f"[nats] close error: {e}")
            self._nc = None

    def on_message(self, cb: Callable[[InboundMessage], None]) -> None:
        self._msg_cb = cb

    def set_event_sink(self, sink: Callable[[str, str, dict], None]) -> None:
        self._sink = sink

    # -- inbound -----------------------------------------------------------
    def _on_nats_msg(self, msg: Any) -> None:
        """NATS thread callback — must never raise (sync wrapper logs)."""
        try:
            data = json.loads(msg.decode())
            chat_id = str(data.get("chat_id") or "").strip()
            if not chat_id:
                # fall back to the subject's last token as chat id
                chat_id = msg.subject.rsplit(".", 1)[-1]
            user_id = str(data.get("user_id") or chat_id)
            if self.allowed_users and user_id not in self.allowed_users:
                self._logger.warning(f"[nats] user {user_id!r} not allowed; drop")
                return
            text = clean_control_chars(str(data.get("text") or ""), max_len=100_000).strip()
            if not text:
                return
            if self._msg_cb:
                self._msg_cb(
                    InboundMessage(
                        channel=self.name,
                        chat_id=chat_id,
                        user_id=user_id,
                        text=text,
                        raw={"subject": msg.subject, "reply": msg.reply or ""},
                    )
                )
        except Exception as e:  # noqa: BLE001 — never kill the subscription
            self._logger.error(f"[nats] inbound dispatch failed: {e}")

    # -- outbound ----------------------------------------------------------
    def _publish_out(self, chat_id: str, payload: dict[str, Any]) -> None:
        if not self._nc:
            return
        subject = f"{self.out_subject}.{chat_id}"
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > MAX_TEXT_BYTES:
            self._logger.warning(f"[nats] payload {len(body)}B > {MAX_TEXT_BYTES}B; dropping")
            return
        try:
            self._nc.publish(subject, body)
        except Exception as e:  # noqa: BLE001
            self._logger.error(f"[nats] publish to {subject} failed: {e}")

    def reply(self, chat_id: str, text: str) -> None:
        # split under the per-message byte budget (envelope counts too)
        budget = MAX_TEXT_BYTES - 200
        for chunk in split_message_text(text, budget):
            self._publish_out(chat_id, {"chat_id": chat_id, "text": chunk})

    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None:
        if kind == "delta":
            # NATS has no edit semantics; stream deltas as status lines is
            # noise — accumulate and deliver on final only.
            return
        if kind == "final":
            self.reply(chat_id, str(payload.get("text", "")))
        elif kind == "status":
            self._publish_out(chat_id, {"chat_id": chat_id, "status": str(payload.get("text", ""))})
        elif kind in ("interaction", "permission"):
            self.reply(chat_id, _interaction_text(payload))
        elif kind == "command_result":
            self.reply(chat_id, _command_result_text(payload))
