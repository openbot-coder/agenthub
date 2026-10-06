"""Editable-message stream: create one bubble, edit it periodically, flush remainder.

Generic pattern for platforms with an "edit message" API (WeCom template
card update / aibot replyStream / any edit endpoint). Mirrors dsh-im's
shared/editable-message-stream.

Usage:
    stream = EditableMessageStream(
        create=lambda text: api.send(chat_id, text),          # -> msg_id
        edit=lambda msg_id, text: api.edit(msg_id, text),
        send_remainder=lambda text: api.send(chat_id, text),
        limit=4000, update_interval_ms=800,
    )
    stream.append("chunk...")      # call from event thread
    stream.close()                 # final flush
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .base import split_message_text


class EditableMessageStream:
    def __init__(
        self,
        create: Callable[[str], Any],
        edit: Callable[[Any, str], Any],
        send_remainder: Callable[[str], Any] | None = None,
        limit: int = 4000,
        update_interval_s: float = 0.8,
        initial_text: str = "正在处理…",
        on_error: Callable[[Exception], None] | None = None,
    ):
        self._create = create
        self._edit = edit
        self._send_remainder = send_remainder
        self._limit = limit
        self._interval = update_interval_s
        self._on_error = on_error
        self._msg_id: Any = None
        self._buffer: list[str] = []
        self._sent_text = ""
        self._lock = threading.Lock()
        self._last_edit = 0.0
        self._closed = False
        self._started = False

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - delivery errors must not kill the turn
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception:
                    pass

    def append(self, text: str) -> None:
        if self._closed or not text:
            return
        with self._lock:
            self._buffer.append(text)
            pending = self._sent_text + "".join(self._buffer)
            now = time.monotonic()
            if self._msg_id is None:
                if not self._started:
                    self._started = True
                    self._msg_id = self._safe(self._create, initial_bubble(pending))
                    self._last_edit = now
                return
            if now - self._last_edit >= self._interval:
                self._flush_locked(pending, now)

    def _flush_locked(self, pending: str, now: float) -> None:
        shown = pending[: self._limit * 4]
        if len(shown.encode("utf-8")) > self._limit:
            shown = shown[: max(1, self._limit // 4) * 4]
            # re-trim to a char count whose utf-8 fits
            while len(shown.encode("utf-8")) > self._limit and shown:
                shown = shown[:-1]
        self._safe(self._edit, self._msg_id, shown)
        self._sent_text = pending
        self._buffer.clear()
        self._last_edit = now

    def close(self, final_text: str | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = self._sent_text + "".join(self._buffer)
            self._buffer.clear()
        text = final_text if final_text is not None else pending
        if not text:
            return
        # if too long for one bubble: edit remainder into follow-up messages
        chunks = split_message_text(text, self._limit)
        if self._msg_id is None:
            for c in chunks:
                self._safe(self._create, c)
            return
        first, rest = (chunks[0], chunks[1:]) if chunks else ("", [])
        self._safe(self._edit, self._msg_id, first)
        send = self._send_remainder or self._create
        for c in rest:
            self._safe(send, c)


def initial_bubble(pending: str) -> str:
    return (pending[:120] + "…") if pending else "正在处理…"
