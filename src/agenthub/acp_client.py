"""Backward-compat shim: ACP client moved to the acpclient package."""

from acpclient.client import ACPClient, ACPError, ACPProcessDead  # noqa: F401

__all__ = ["ACPClient", "ACPError", "ACPProcessDead"]
