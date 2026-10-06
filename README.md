# acp-bridge

IM/NATS ↔ 本地 ACP 桥：让总线上的编排方（Octop / botflow / 任意 worker）
能够**发现并驱动**运行在本地机器上的 ACP 编码 agent
（Claude Code、Codex、Gemini CLI、OpenCode …），并收回结构化事件流。

```
IM 用户 ── Octop ── NATS bus ── acp-bridge ── stdio JSON-RPC (ACP) ── coding agent
```

## 定位

- **ACP agent 侧代理（client 角色）**：acp-bridge 作为 ACP client 拉起/管理
  本地 agent 子进程，把 NATS 请求翻译成 ACP 会话。
- **总线侧服务**：对外表现为 NATS 上的一个可发现服务（类似 A2A 的
  Agent Card 思路，但传输是 NATS）。

详细设计见 [docs/DESIGN.md](docs/DESIGN.md)。

## 仓库结构

```
acp-bridge/
├── docs/DESIGN.md        # 设计方案（总）
├── docs/ACP_PRIMER.md    # ACP 协议速查（方法/生命周期/坑）
├── src/acp_bridge/
│   ├── __init__.py
│   ├── config.py         # 配置加载
│   ├── agent_registry.py # 本地 agent 进程池（拉起/复用/回收）
│   ├── acp_client.py     # ACP JSON-RPC over stdio 客户端（frame 层）
│   ├── session.py        # 会话状态机：nats 请求 <-> acp session 映射
│   ├── events.py         # ACP session/update -> NATS 事件主题 映射
│   └── nats_service.py   # NATS 订阅/发布、服务发现广播
├── tests/
└── pyproject.toml
```

## 快速开始（规划中）

```bash
uv sync
uv run acp-bridge --config config.yaml
```

## 状态

设计阶段 → M0 骨架实现中。
