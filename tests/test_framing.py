"""M0 tests: Content-Length framing encode/decode."""

import pytest

from agenthub.framing import FrameError, decode_frames, encode_frame


def test_roundtrip_single():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    data = encode_frame(msg)
    out = list(decode_frames(data))
    assert out == [msg]


def test_roundtrip_multiple_in_one_buffer():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        {"jsonrpc": "2.0", "method": "session/update", "params": {"seq": 1}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "nope"}},
    ]
    data = b"".join(encode_frame(m) for m in msgs)
    assert list(decode_frames(data)) == msgs


def test_unicode_body():
    msg = {"text": "中文内容 emoji 🎉"}
    data = encode_frame(msg)
    # header must count bytes, not chars
    assert b"Content-Length: " in data
    assert list(decode_frames(data)) == [msg]


def test_partial_buffer_raises_incomplete():
    msg = {"a": 1}
    data = encode_frame(msg)
    with pytest.raises(FrameError, match="incomplete"):
        list(decode_frames(data[:-5]))


def test_partial_header_raises_no_terminator():
    with pytest.raises(FrameError, match="no header terminator"):
        list(decode_frames(b"Content-Length: 20\r\n"))


def test_missing_content_length():
    bad = b"Content-Type: text/plain\r\n\r\n{}"
    with pytest.raises(FrameError, match="missing Content-Length"):
        list(decode_frames(bad))


def test_bad_content_length():
    bad = b"Content-Length: abc\r\n\r\n{}"
    with pytest.raises(FrameError, match="bad Content-Length"):
        list(decode_frames(bad))


def test_bad_json_body():
    bad = b"Content-Length: 3\r\n\r\n{xx"
    with pytest.raises(FrameError, match="bad JSON body"):
        list(decode_frames(bad))


def test_incremental_feed():
    """Simulate a stream that arrives in fragments."""
    msg = {"jsonrpc": "2.0", "id": 7, "result": [1, 2, 3]}
    data = encode_frame(msg)
    got = []
    buf = b""
    for i in range(len(data)):
        buf += data[i : i + 1]
        try:
            got.extend(decode_frames(buf))
            buf = b""
        except FrameError:
            pass  # incomplete: keep feeding
    assert got == [msg]
