"""Shared test isolation.

Several tests drive ``SessionManager.prompt(files=...)`` / ``_media_dir()`` with
agents that have no ``cwd_whitelist``, which makes the fallback path write to
``~/agenthub-media``. Pointing ``HOME`` at a per-test temp dir keeps the suite
from littering the real home directory.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Per-test ``$HOME`` so nothing is written to the real home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
