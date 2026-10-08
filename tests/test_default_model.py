"""Tests for pinning the configured default model on a fresh ACP session.

The free-tier opencode agent rotates its built-in default model and some
models' upstreams go dark, so agenthub must set `default_model` itself
right after `session/new`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agenthub.session import AgentSpec, Binding, SessionManager  # noqa: E402


class StubClient:
    """Records JSON-RPC requests; returns canned session/new + configOptions."""

    def __init__(self, current_model: str = "opencode/other-free", fail_set: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.current_model = current_model
        self.fail_set = fail_set

    def is_alive(self) -> bool:
        return True

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        self.calls.append((method, params))
        if method == "session/new":
            return {
                "sessionId": "sess-1",
                "configOptions": [
                    {
                        "id": "model",
                        "currentValue": self.current_model,
                        "options": [
                            {"value": self.current_model, "name": self.current_model},
                            {"value": "opencode/ling-3.1-flash-free", "name": "ling"},
                        ],
                    }
                ],
            }
        if method == "session/set_config_option":
            if self.fail_set:
                raise RuntimeError("boom")
            self.current_model = params["value"]
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "currentValue": self.current_model,
                        "options": [{"value": self.current_model, "name": self.current_model}],
                    }
                ]
            }
        raise AssertionError(f"unexpected method {method}")

    def set_calls(self) -> list[dict]:
        return [p for m, p in self.calls if m == "session/set_config_option"]


def make_manager(model: str | None, stub: StubClient) -> SessionManager:
    agents = {
        "oc": AgentSpec(name="oc", cmd=["true"], default_model=model),
    }
    m = SessionManager(agents=agents, transport="ndjson")
    m._client_for = lambda *a, **kw: stub  # noqa: SLF001
    return m


def test_default_model_is_pinned_on_new_session():
    stub = StubClient(current_model="opencode/exo-free")
    m = make_manager("opencode/ling-3.1-flash-free", stub)
    client, sid = m._ensure_session(("wecom", "c1"), "oc", None)  # noqa: SLF001

    assert sid == "sess-1"
    sets = stub.set_calls()
    assert len(sets) == 1, sets
    assert sets[0] == {
        "sessionId": "sess-1",
        "configId": "model",
        "value": "opencode/ling-3.1-flash-free",
    }
    # snapshot reflects the pinned model, so /model reports it as current
    snap = m._session_config(Binding("wecom", "c1", "oc", "sess-1"))  # noqa: SLF001
    assert snap["model"]["current"] == "opencode/ling-3.1-flash-free"
    m.stop()


def test_no_pin_when_default_model_unset():
    stub = StubClient(current_model="opencode/exo-free")
    m = make_manager(None, stub)
    m._ensure_session(("wecom", "c1"), "oc", None)  # noqa: SLF001
    assert stub.set_calls() == []
    m.stop()


def test_no_pin_when_agent_default_already_matches():
    stub = StubClient(current_model="opencode/ling-3.1-flash-free")
    m = make_manager("opencode/ling-3.1-flash-free", stub)
    m._ensure_session(("wecom", "c1"), "oc", None)  # noqa: SLF001
    assert stub.set_calls() == [], "must not re-set an already-current model"
    m.stop()


def test_pin_failure_is_non_fatal():
    """A dead/renamed model must not break session creation."""
    stub = StubClient(current_model="opencode/exo-free", fail_set=True)
    m = make_manager("opencode/does-not-exist", stub)
    client, sid = m._ensure_session(("wecom", "c1"), "oc", None)  # noqa: SLF001
    assert sid == "sess-1", "session must still be usable"
    assert len(stub.set_calls()) == 1
    m.stop()
