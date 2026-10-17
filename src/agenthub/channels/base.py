"""Channel abstraction: every inbound/outbound front-end implements this.

A channel is identified by (channel, chat_id). All hub logic (session
binding, agent routing, permissions) is channel-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Protocol

# ---------------------------------------------------------------------------
# inbound message
# ---------------------------------------------------------------------------

@dataclass
class MediaFile:
    """One inbound/outbound media attachment (image/video/audio/file)."""
    kind: str          # "image" | "video" | "audio" | "file"
    data: bytes = b""
    filename: str = ""
    mime: str = "application/octet-stream"


@dataclass
class InboundMessage:
    channel: str                 # "wecom" | "wecom-app" | "weixin" | "nats"
    chat_id: str                 # chat/user identifier within the channel
    user_id: str                 # sender identity (for ACL)
    text: str = ""
    images: list[bytes] = field(default_factory=list)   # raw bytes
    image_mime: str = "image/png"
    files: list[MediaFile] = field(default_factory=list)  # all attachments
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


# ---------------------------------------------------------------------------
# structured renderers shared by all channels
# ---------------------------------------------------------------------------
def _interaction_text(payload: dict[str, Any]) -> str:
    """Render a structured approval/choice for chat display.

    Front-ends with card APIs should use payload['options'] (option_id/name/kind)
    to render buttons; text channels fall back to numbered replies.
    """
    itype = payload.get("interaction_type", "permission")
    icon = "🔐" if itype == "permission" else "❓"
    title = payload.get("tool") or payload.get("title") or "请选择"
    lines = [f"{icon} **{title}**"]
    if payload.get("kind"):
        lines.append(f"类型：{payload['kind']}")
    opts = payload.get("options") or []
    if opts:
        lines.append("")
        for i, o in enumerate(opts, 1):
            mark = {"allow_once": "✅", "always_once": "✅✅", "reject_once": "❌"}.get(o.get("kind"), "•")
            lines.append(f"{mark} {i}. {o.get('name', o.get('option_id'))}")
        lines.append("")
        lines.append("回复编号或选项名称；回复 r 拒绝。")
    lines.append(f"请求 ID：`{payload.get('request_id', '')}`")
    return "\n".join(lines)


def _command_result_text(payload: dict[str, Any]) -> str:
    t = payload.get("type", "")
    if t == "model_list":
        lines = ["**可用模型**"]
        for m in payload.get("models", []):
            cur = " *(当前)*" if m.get("current") else ""
            lines.append(f"- `{m.get('value')}`{cur}")
        return "\n".join(lines)
    if t == "model_set":
        return f"✅ 模型已切换为 `{payload.get('value')}`"
    if t == "new":
        return f"🆕 新会话已创建（agent: {payload.get('agent')}）"
    if t == "switch":
        extra = f"，迁移了 {payload.get('migrated')} 条历史" if payload.get("migrated") else ""
        return f"🔄 已切换到 agent `{payload.get('agent')}`{extra}"
    if t == "agents":
        lines = ["**可用 agents**"]
        for a in payload.get("agents", []):
            state = "🟢" if a.get("running") else "⚪"
            lines.append(f"{state} `{a.get('name')}`")
        return "\n".join(lines)
    if t == "status":
        if payload.get("bound"):
            return f"📌 绑定：`{payload.get('agent')}`\nsession: `{payload.get('acp_session_id')}`"
        return "未绑定会话，发消息即自动创建"
    if t == "help":
        return "**命令**\n" + "\n".join(f"- `{c}`" for c in payload.get("commands", []))
    if t == "error":
        return f"⚠️ {payload.get('error')}"
    return "```\n" + json.dumps(payload, ensure_ascii=False)[:2000] + "\n```"
