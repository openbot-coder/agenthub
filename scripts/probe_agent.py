"""Probe a local ACP agent: initialize handshake + capability dump.

Usage:
    uv run python scripts/probe_agent.py [--agent CMD] [--transport ndjson|lsp-frame]

Example:
    uv run python scripts/probe_agent.py --agent "node_modules/.bin/claude-code-acp"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agenthub.acp_client import ACPClient, ACPError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="node_modules/.bin/claude-code-acp")
    ap.add_argument("--transport", default="ndjson", choices=["ndjson", "lsp-frame"])
    ap.add_argument("--cwd", default="/tmp")
    ap.add_argument("--prompt", default=None, help="optionally send one prompt")
    args = ap.parse_args()

    client = ACPClient(
        cmd=[args.agent],
        cwd=os.getcwd(),
        transport=args.transport,
        env={**os.environ, "PATH": os.path.expanduser("~/.npm-global/node_modules/.bin") + ":" + os.environ.get("PATH", "")},
    )
    client.start()
    try:
        info = client.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            },
            timeout=30,
        )
        print("== initialize ==")
        print("protocolVersion:", info.get("protocolVersion"))
        print("agentCapabilities:", info.get("agentCapabilities"))
        print("authMethods:", info.get("authMethods"))

        sid = client.session_new(cwd=args.cwd)
        print("== session/new == sessionId:", sid)

        updates: list[dict] = []
        client.register_notification_handler(lambda m, p: updates.append(p))

        if args.prompt:
            try:
                res = client.session_prompt(sid, [{"type": "text", "text": args.prompt}], timeout=120)
                print("== session/prompt ==", res)
            except ACPError as e:
                print("== session/prompt rejected ==", e.code, e.message)
            kinds = [u["update"]["sessionUpdate"] for u in updates if "update" in u]
            print("updates seen:", kinds[:20], f"(total {len(kinds)})")
        else:
            print("(no --prompt given; handshake only)")

        if client.stderr_tail:
            print("== agent stderr tail ==")
            for line in client.stderr_tail[-5:]:
                print(" ", line)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
