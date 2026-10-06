"""Channel abstraction: every inbound/outbound front-end implements this.

A channel is identified by (channel, chat_id). All hub logic (session
binding, agent routing, permissions) is channel-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# ---------------------------------------------------------------------------
# inbound message
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    channel: str                 # "wecom" | "wecom-app" | "weixin" | "nats"
    chat_id: str                 # chat/user identifier within the channel
    user_id: str                 # sender identity (for ACL)
    text: str = ""
    images: list[bytes] = field(default_factory=list)   # raw bytes
    image_mime: str = "image/png"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelUserRef:
    channel: str
    chat_id: str
    user_id: str = ""


# ---------------------------------------------------------------------------
# outbound event sink — channels decide how to present progress
# ---------------------------------------------------------------------------

EventSink = Callable[[str, str, dict[str, Any]], None]
"""sink(chat_id, kind, payload); kind in:
    "delta"        - text chunk (streaming append)
    "replace"      - full replacement of current bubble text
    "status"       - transient status line (tool running, thinking...)
    "final"        - terminal text for the turn
    "permission"   - permission request payload (needs user decision)
"""


class IMChannel(Protocol):
    """Lifecycle + IO contract for all IM front-ends."""

    name: str

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def on_message(self, cb: Callable[[InboundMessage], None]) -> None: ...
    def set_event_sink(self, sink: EventSink) -> None: ...

    def reply(self, chat_id: str, text: str) -> None: ...
    def push_event(self, chat_id: str, kind: str, payload: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------
# helpers shared by channels
# ---------------------------------------------------------------------------

def split_message_text(value: str, limit: int) -> list[str]:
    """Split text into chunks <= limit chars, preferring \\n then space breaks.

    Mirrors dsh-im shared/editable-message-stream.splitMessageText.
    """
    text = (value or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining.encode("utf-8")) > limit:
        # operate on utf-8 byte budget like WeCom requires
        cut = _byte_safe_cut(remaining, limit)
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _byte_safe_cut(text: str, byte_limit: int) -> int:
    """Largest char index whose utf-8 prefix fits byte_limit, cut at \\n/space if reasonable."""
    cut = len(text)
    acc = 0
    for i, ch in enumerate(text):
        w = 1 if ord(ch) < 0x80 else (2 if ord(ch) < 0x800 else (3 if ord(ch) < 0x10000 else 4))
        if acc + w > byte_limit:
            cut = i
            break
        acc += w
    window = text[:cut]
    nl = window.rfind("\n")
    if nl >= int(cut * 0.55):
        return nl
    sp = window.rfind(" ")
    if sp >= int(cut * 0.55):
        return sp
    return cut


def clean_control_chars(value: str, max_len: int = 80) -> str:
    """Strip Cc/Cf (zero-width etc.) — WeCom/ZWSP hygiene."""
    out = []
    for ch in (value or "")[: max_len * 4]:
        o = ord(ch)
        if o in (0x200B, 0x200C, 0x200D, 0xFEFF) or (o < 0x20 and ch not in "\n\t"):
            continue  # drop, don't substitute
        out.append(ch)
    return "".join(out).strip()[:max_len]
