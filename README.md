# agenthub

IM/NATS ↔ 本地 ACP 桥：让 IM 用户（企业微信 / 微信）与总线上的编排方
（Octop / botflow / 任意 worker）能够**发现并驱动**运行在本地机器上的
ACP 编码 agent（Claude Code、Codex、Gemini CLI、OpenCode …），
并收回结构化事件流。

```
IM 用户 ─┐
         ├─ agenthub ── stdio JSON-RPC (ACP, ndjson) ── coding agent
NATS bus ┘
```

- IM 与 NATS 是**平级前端通道**：NATS 只负责消息如何来、怎么回；
  IM 通道把企业微信/微信消息归一化成同一套 op 语义。
- agenthub 在 ACP 协议里是 **client**（agent 子进程是 server）。

详细设计见 [docs/DESIGN.md](docs/DESIGN.md)，
ACP 协议速查（方法/生命周期/实测坑）见 [docs/ACP_PRIMER.md](docs/ACP_PRIMER.md)。

## 安装（uv 管理）

```bash
# 从源码
git clone https://gitea.vxquant.com/vxquant/agenthub.git
cd agenthub
uv sync                      # 创建 .venv、装运行时依赖 + dev 组
uv run pytest tests/ -q      # 43 个测试应全绿

# 从 PyPI（测试源）
uv pip install --index-url https://test.pypi.org/simple/ agenthub
```

运行依赖刻意保持最小（当前仅 `cryptography`，供企微自建应用消息加解密）。
NATS 通道的 `nats-py` 放在 extra：`uv pip install "agenthub[nats]"`。

## 配置

配置用 JSON（`config.json`，首次引导；运行期事实源是 sqlite DB）：

```json
{
  "agents": {
    "claude-code": {
      "cmd": ["claude", "--acp"],
      "cwd_whitelist": ["~/projects/*"]
    }
  },
  "session":   { "idle_ttl_s": 1800, "max_sessions": 8 },
  "orchestration": { "max_depth": 3 },
  "nats": { "url": "nats://127.0.0.1:4222",
            "inbox": "bots.agenthub.in", "events": "bots.agenthub.out" },
  "channels": {
    "wecom":      { "bot_id": "...", "secret": "..." },
    "wecom-app":  { "corp_id": "...", "agent_id": "...", "secret": "...",
                    "token": "...", "encoding_aes_key": "..." },
    "weixin":     { "token": "扫码登录后持久化的 token" }
  }
}
```

## 通道

| 通道 | 目录 | 传输 | 流式 |
|---|---|---|---|
| `wecom` 企微 aibot | `src/agenthub/channels/wecom.py` | WebSocket 长连接（免公网回调） | ✅ 同气泡流式编辑，delta 0.8s 合并 |
| `wecom-app` 企微自建应用 | `src/agenthub/channels/wecom_app.py` | HTTP 回调 + qyapi API；企微客户端与微信（微信插件）两端可用 | ❌ 无编辑 API，整段发送 |
| `weixin` 微信智能机器人 | `src/agenthub/channels/weixin.py` | QR 扫码登录 + `getupdates` 长轮询 | ❌ 整段发送 |

事件语义（三通道与 NATS 通道一致）：
`delta / thinking / tool_start / tool_end / plan / permission / final`。

## 会话与 /switch

- 绑定表：`(channel, chat_id) → {agent, acp_session_id}`，sqlite 持久化。
- `/switch <agent> [--continue]`：切换 IM 会话绑定的 agent；
  `--continue` 跨 agent 只迁移 hub 侧对话文本（tool state 不迁移）。
- cwd 白名单按 agent 配置校验；`idle_ttl_s` 到期回收，`max_sessions` LRU 逐出。

## 仓库结构

```
agenthub/
├── docs/DESIGN.md            # 设计方案（总）
├── docs/ACP_PRIMER.md        # ACP 协议速查（ndjson 实测修正等）
├── src/agenthub/
│   ├── framing.py            # ndjson / LSP-frame 双传输分帧
│   ├── acp_client.py         # ACP JSON-RPC over stdio 客户端（含反向请求）
│   ├── session.py            # 绑定表 + /switch + 事件中继 + idle reaper + sqlite
│   ├── channels/             # IM 前端通道（wecom / wecom-app / weixin）
│   ├── nats_service.py       # NATS 订阅/发布（M1 后续）
│   ├── agent_registry.py     # agent 进程池（M2）
│   └── config.py             # 配置加载
├── tests/                    # 43 tests（mock agent 子进程 + mock 传输）
├── scripts/probe_agent.py    # ACP agent 探针
└── config.json               # 示例配置
```

## 状态

- **M0 ✅** ACP 客户端（ndjson 传输实测修正，真实 claude-code-acp 验证）
- **M1 ✅** SessionManager（绑定表 / switch / reaper / sqlite）
- **M1.5 ✅（代码+mock e2e）** 三 IM 通道；真机验收需真实凭据
- **待做**：NATS 通道服务化、Web admin、supervisor + dashboard

## 发布

```bash
uv build                          # 产 dist/*.tar.gz + *.whl
uv publish --publish-url https://test.pypi.org/legacy/   # 测试源
uv publish                        # 正式 PyPI（Gitea 测试通过后再发）
```
