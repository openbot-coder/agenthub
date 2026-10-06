"""ndjson transport tests (the transport used by current ACP SDK agents)."""

import json

from agenthub.framing import decode_ndjson_lines, encode_ndjson


def test_ndjson_roundtrip():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    data = encode_ndjson(msg)
    assert data.endswith(b"\n")
    msgs, consumed = decode_ndjson_lines(data)
    assert msgs == [msg]
    assert consumed == len(data)


def test_ndjson_partial_tail():
    a = {"id": 1}
    b = {"id": 2}
    buf = encode_ndjson(a) + encode_ndjson(b)[:10]  # b is incomplete
    msgs, consumed = decode_ndjson_lines(buf)
    assert msgs == [a]
    assert consumed == len(encode_ndjson(a))


def test_ndjson_unicode():
    msg = {"text": "中文🎉"}
    data = encode_ndjson(msg)
    assert json.loads(data.decode("utf-8")) == msg
