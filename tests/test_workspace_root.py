"""Agent workspace root derivation from ``cwd_whitelist`` patterns.

Regression: whitelist entries are fnmatch patterns (``~/workspace/*``), so a
naive ``expanduser`` of the first entry produced a literal ``*`` directory and
every inbound attachment landed in ``~/workspace/*/media_in``.
"""
from __future__ import annotations

import os

import pytest

from agenthub.session import SessionManager, _glob_root


def test_glob_root_strips_glob_segment():
    assert _glob_root("/home/qbot/workspace/*") == "/home/qbot/workspace"
    assert _glob_root("/home/qbot/projects/*") == "/home/qbot/projects"


def test_glob_root_keeps_plain_path():
    assert _glob_root("/home/qbot/workspace") == "/home/qbot/workspace"


def test_glob_root_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _glob_root("~/workspace/*") == str(tmp_path / "workspace")


def test_glob_root_mid_path_glob():
    assert _glob_root("/home/*/workspace") == "/home"


def test_glob_root_edge_cases():
    assert _glob_root("/*") == os.sep
    assert _glob_root("*.py") == "."
    assert _glob_root("/home/qbot/workspace/??") == "/home/qbot/workspace"


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    from agenthub.session import AgentSpec

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "workspace").mkdir()
    return SessionManager(
        {
            "deepseek": AgentSpec(
                name="deepseek",
                cmd=["opencode acp"],
                cwd_whitelist=[str(tmp_path / "workspace") + "/*", str(tmp_path / "projects") + "/*"],
            )
        },
        db_path=str(tmp_path / "s.db"),
    )


def test_media_dir_not_literal_star(mgr, tmp_path):
    d = mgr._media_dir("deepseek")
    assert d == str(tmp_path / "workspace" / "media_in")
    assert os.path.isdir(d)
    assert not (tmp_path / "workspace" / "*").exists()


def test_agent_cwd_is_glob_base(mgr, tmp_path):
    assert mgr._agent_cwd("deepseek") == str(tmp_path / "workspace")
