#!/usr/bin/env bash
# Add /agents reverse proxy to api.vxquant.com site.
#  - public map entry so the prefix bypasses the internal-only 404 rule
#  - location /agents/ -> 127.0.0.1:30990 (agenthub status http)
#  - POST /agents/prompt guarded to internal clients only
# Idempotent: skips if already present. Requires sudo.
set -euo pipefail

SITE=/etc/nginx/sites-enabled/api.vxquant.com
MARK="# --- agenthub /agents (added by deploy) ---"

if sudo grep -qF "$MARK" "$SITE"; then
  echo "[nginx] /agents already configured; nothing to do"
  exit 0
fi

# 1) public path map: add /agents before the closing brace of the map block
if ! sudo grep -qE '^\s*~\^/agents\(/\|\$\)' "$SITE"; then
  sudo python3 - "$SITE" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path).read()
anchor = "    ~^/quantdata(/|$)   1;\n"
assert anchor in src, "map anchor not found"
src = src.replace(anchor, anchor + "    ~^/agents(/|$)     1;\n", 1)
open(path, "w").write(src)
print("[nginx] public map: added /agents")
PY
fi

# 2) location blocks, inserted before the final "location / {" fallback
sudo python3 - "$SITE" "$MARK" <<'PY'
import sys
path, mark = sys.argv[1], sys.argv[2]
src = open(path).read()
block = f"""
    {mark}
    location /agents/ {{
        proxy_pass http://127.0.0.1:30990/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        client_max_body_size 10m;
    }}
    location = /agents/prompt {{
        # driving an agent is an operator action: loopback/intranet only
        if ($is_internal = 0) {{ return 404; }}
        proxy_pass http://127.0.0.1:30990/prompt;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
        client_max_body_size 1m;
    }}
    # --- end agenthub /agents ---

"""
anchor = "\n    # 其他路径 — 仅内网可访问\n"
assert anchor in src, "fallback anchor not found"
src = src.replace(anchor, block + anchor, 1)
open(path, "w").write(src)
print("[nginx] location blocks: added /agents/ and = /agents/prompt")
PY

echo "[nginx] testing config"
sudo nginx -t
echo "[nginx] reloading"
sudo systemctl reload nginx
echo "[nginx] done"
