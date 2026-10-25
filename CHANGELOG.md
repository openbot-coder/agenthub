# Changelog

## [0.2.0] — 2026-10-25

### 拆分（gateway / acpclient）
- 发行名 `agenthub` → **`agenthub-gateway`**（import 名仍为 `agenthub`，兼容旧部署与测试）。
- ACP 客户端独立成包 **`acpclient`**（新仓库 `vxquant/acpclient`）：
  - `acpclient.ACPClient` — stdio + ndjson/lsp-frame 客户端（自 agenthub 平移）。
  - `acpclient.RemoteACPClient` — 同 API，transport 为 HTTP POST（指令）+ WebSocket（事件回流），
    帧仍是 ACP JSON-RPC 原样透传；面向远程 worker-agentd（M-w1 目标）。
  - `acpclient.framing` — ndjson / LSP Content-Length 编解码。
- `agenthub/framing.py`、`agenthub/acp_client.py` 变 3 行 re-export shim，老 import 零破坏。
- `[project.scripts]` 增加 `agenthub-gateway`（保留 `agenthub` 别名）。

### 文档
- 新增 `docs/DESIGN_GATEWAY_WORKER.md`（gateway + worker 架构：deepagents 总管 + HTTP/WS 派活）。
- 新增 `docs/UPGRADE_SPLIT.md`（拆分实施清单）。

### 依赖
- 新增 `acpclient>=0.1.0` 运行时依赖。
