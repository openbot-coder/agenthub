"""ACP JSON-RPC over stdio: Content-Length framing (LSP style).

Frame format used by ACP (same as LSP):
    Content-Length: <N>\r\n
    \r\n
    <N bytes of UTF-8 JSON>
"""

from __future__ import annotations

import json
from typing import Any, Iterator

_HEADER_SEP = b"\r\n\r\n"
_HEADER_END = b"\r\n"


class FrameError(Exception):
    """Raised when the byte stream cannot be parsed as a valid frame."""


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize a JSON-RPC message to one ACP frame."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = b"Content-Length: " + str(len(body)).encode() + _HEADER_END * 2
    return header + body


def _read_exact(buf: memoryview, pos: int, n: int) -> tuple[memoryview, int]:
    if pos + n > len(buf):
        raise FrameError(f"incomplete frame: need {n} bytes at pos {pos}, have {len(buf) - pos}")
    return buf[pos : pos + n], pos + n


def decode_frames(buf: bytes | bytearray | memoryview) -> Iterator[dict[str, Any]]:
    """Parse all complete frames from a buffer.

    Raises FrameError on malformed headers or truncated frames;
    returns an iterator of decoded JSON objects.
    """
    view = memoryview(bytes(buf))
    pos = 0
    while pos < len(view):
        # find header terminator
        idx = bytes(view[pos:]).find(_HEADER_SEP)
        if idx == -1:
            raise FrameError(f"no header terminator after pos {pos}")
        header_blob = bytes(view[pos : pos + idx])
        pos += idx + len(_HEADER_SEP)

        length: int | None = None
        content_type: str | None = None
        for line in header_blob.split(_HEADER_END):
            if not line:
                continue
            name, _, value = line.partition(b":")
            name = name.strip().lower()
            if name == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError as e:
                    raise FrameError(f"bad Content-Length: {value!r}") from e
            elif name == b"content-type":
                content_type = value.strip().decode("ascii", "replace")
        if length is None:
            raise FrameError(f"missing Content-Length header: {header_blob!r}")
        if length < 0:
            raise FrameError(f"negative Content-Length: {length}")

        body_view, pos = _read_exact(view, pos, length)
        try:
            yield json.loads(bytes(body_view).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise FrameError(f"bad JSON body ({length} bytes): {e}") from e


# ---------------------------------------------------------------------------
# newline-delimited JSON (ndjson) transport — used by current ACP SDK agents
# (e.g. @zed-industries/claude-code-acp via @agentclientprotocol/sdk).
# LSP-style Content-Length frames are kept in encode_frame/decode_frames for
# agents that use them; transport is auto-detected by ACPClient.
# ---------------------------------------------------------------------------

def encode_ndjson(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_ndjson_lines(buf: bytes | bytearray):
    """Yield (messages, consumed_bytes) for complete newline-terminated lines."""
    view = bytes(buf)
    idx = view.rfind(b"\n")
    if idx == -1:
        return [], 0
    out = []
    for line in view[:idx].split(b"\n"):
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line.decode("utf-8")))
    return out, idx + 1
