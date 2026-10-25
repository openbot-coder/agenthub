"""Backward-compat shim: framing codecs moved to the acpclient package."""

from acpclient.framing import (  # noqa: F401
    FrameError,
    decode_frames,
    decode_ndjson_lines,
    encode_frame,
    encode_ndjson,
)

__all__ = [
    "FrameError",
    "decode_frames",
    "decode_ndjson_lines",
    "encode_frame",
    "encode_ndjson",
]
