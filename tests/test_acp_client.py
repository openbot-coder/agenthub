"""M0 tests: ACPClient end-to-end against the mock ACP agent subprocess."""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agenthub.acp_client import ACPClient, ACPError, ACPProcessDead  # noqa: E402

MOCK = ROOT / "tests" / "fixtures" / "mock_acp_agent.py"


def make_client(with_permission: bool = False, chunk_count: int = 3) -> ACPClient:
    cmd = [sys.executable, str(MOCK)]
    if with_permission:
        cmd.append("--with-permission")
    cmd += ["--chunks", str(chunk_count)]
    return ACPClient(cmd=cmd)


def test_initialize_and_prompt_streams_updates():
    client = make_client()
    client.start()
    try:
        info = client.initialize()
        assert info["protocolVersion"] == 1

        events: list[tuple[str, dict]] = []
        client.register_notification_handler(lambda m, p: events.append((m, p)))

        sid = client.session_new(cwd="/tmp")
        assert sid.startswith("s-")

        res = client.session_prompt(sid, [{"type": "text", "text": "hi"}], timeout=20)
        assert res["stopReason"] == "end_turn"

        # wait a beat for notifications to drain
        time.sleep(0.2)
        updates = [p["update"]["sessionUpdate"] for m, p in events if m == "session/update"]
        assert updates.count("agent_message_chunk") == 3
        assert "tool_call" in updates
    finally:
        client.close()


def test_permission_request_is_dispatched_to_handler():
    client = make_client(with_permission=True)
    client.start()
    try:
        client.initialize()
        answered: list[tuple[str, dict]] = []

        def on_request(method: str, params: dict):
            answered.append((method, params))
            return {"outcome": {"optionId": "allow_once"}}

        client.register_request_handler(on_request)
        sid = client.session_new(cwd="/tmp")
        res = client.session_prompt(sid, [{"type": "text", "text": "go"}], timeout=20)
        assert res["stopReason"] == "end_turn"
        time.sleep(0.1)
        assert answered and answered[0][0] == "session/request_permission"
        assert answered[0][1]["toolCall"]["title"] == "rm -rf /tmp/x"
    finally:
        client.close()


def test_unknown_server_request_gets_method_not_found():
    """If no handler claims a server->client request, the client answers -32601
    rather than hanging the agent."""
    client = make_client(with_permission=True)
    client.start()
    try:
        client.initialize()
        # no request handler registered at all
        sid = client.session_new(cwd="/tmp")
        res = client.session_prompt(sid, [{"type": "text", "text": "go"}], timeout=20)
        assert res["stopReason"] == "end_turn"
    finally:
        client.close()


def test_unknown_method_raises_acp_error():
    client = make_client()
    client.start()
    try:
        client.initialize()
        with pytest.raises(ACPError) as ei:
            client.request("does/not/exist", {}, timeout=5)
        assert ei.value.code == -32601
    finally:
        client.close()


def test_cancel_mid_prompt():
    client = make_client(chunk_count=200)  # long stream
    client.start()
    try:
        client.initialize()
        sid = client.session_new(cwd="/tmp")
        result: list = []

        def run():
            try:
                result.append(client.session_prompt(sid, [{"type": "text", "text": "long"}], timeout=20))
            except Exception as e:  # noqa: BLE001
                result.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.2)
        client.session_cancel(sid)
        t.join(timeout=5)
        assert result and result[0].get("stopReason") == "cancelled"
    finally:
        client.close()


def test_dead_process_fails_pending_requests():
    client = make_client(chunk_count=200)
    client.start()
    try:
        client.initialize()
        sid = client.session_new(cwd="/tmp")
        errs: list = []

        def run():
            try:
                client.session_prompt(sid, [{"type": "text", "text": "x"}], timeout=15)
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.2)
        client._proc.kill()  # simulate agent crash
        t.join(timeout=10)
        assert errs and isinstance(errs[0], ACPProcessDead)
        assert not client.is_alive()
    finally:
        client.close()
