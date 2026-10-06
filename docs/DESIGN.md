# agenthub 设计方案

> 版本：v0.4（2026-10-05）
> 目标：把本地 ACP 编码 agent 桥接到 NATS 消息总线上，形成可发现、可派活、
> 可编排的 agent 服务网格。IM（Octop）只是前端的一种。

## 0. 问题定义与形态演进

现状两条链路各自独立：

- Octop/botflow：**IM 用户 ↔ Agent**，传输 NATS（`bots.*` 主题）
- 编码 agent（Claude Code / Codex / Gemini CLI / OpenCode …）：**编辑器 ↔ Agent**，传输 ACP（stdio JSON-RPC）

### 形态定位（v0.4 关键修正）

```
   IM 通道 ──┐
             ├──▶  agenthub  ──▶  claude acp / codex acp / opencode acp / pi acp ...
  NATS 通道 ──┘
```

**IM 与 NATS 地位完全一致，是平级的两个前端通道**：

- 区别只在形态：IM 是面向人的会话通道；NATS 是面向程序的总线通道
- **NATS 的职责仅限于"消息如何到达、结果如何回复"**（request/reply +
  事件订阅），不承担任何业务语义
- agenthub 对两者一视同仁：同一套 op 接口（prompt/new_session/...）、
  同一套事件类型，只是回包通道不同
- 两条通道可同时启用、可各自独立配置（见 §8 Web 管理 UI）

- 对前：暴露统一的 op 接口，调用方是 IM、总线 client、supervisor agent、
  cron，**一视同仁**
- 对后：把 ACP agent 变成可发现、可派活的服务

**为什么走 ACP 而不是直接 shell 调 CLI**：
- 统一权限审批（ACP 反转文件系统所有权，客户端可审计每次 read/edit/execute）
- 统一事件流（session/update 分类事件，天然映射成总线消息）
- 一次接入，所有 ACP 兼容 agent 通用（40+，还在增长）

### 功能范围（v0.3 修订）

原始三件套之外的补充功能：

| 功能 | 章节 |
|---|---|
| Web 管理 UI（配前端通道 / 后端 agent） | §8 |
| `/switch`：IM 会话切换 agent 进程 | §9 |
| 可视化 dashboard（系统监控） | §10 |

## 1. 角色模型：三责分离（编排怎么长出来）

```
┌────────────────────────────────────────────┐
│  supervisor agent（总管，如 codex）          │  ← 决定"派什么活"（LLM）
│  它自己也是 ACP agent，也跑在 agenthub 上    │
└──────────────┬─────────────────────────────┘
               │ 派子任务：prompt op（带 session_id + depth）
┌──────────────▼─────────────────────────────┐
│  agenthub（= gateway + registry + 护栏）     │
│  ① gateway：op 路由到正确的 agent            │
│  ② registry：哪些 agent 在线、能力是什么      │
│  ③ 护栏：预算/超时/深度上限（代码管，非 LLM 管）│
└──────────────┬─────────────────────────────┘
┌──────────────▼─────────────────────────────┐
│  worker agents：opencode / pi / gemini ...  │  ← 干活
└────────────────────────────────────────────┘
```

原则：

- **supervisor 是 agent，agenthub 是普通服务**。分拆/汇总的"智能"在
  supervisor 的 LLM 里；agenthub 只提供机制
- **汇总不需要专门机制**：supervisor 用 session_id 收各子任务 turn 终态，
  把结果拼进自己的上下文，最后一轮输出回调用方——ACP 会话语义天然支持
- **递归防护**：`new_session` 请求带 `depth`（默认 0），worker 会话派生
  子会话时 agenthub 强制 depth+1，超过 `max_depth`（默认 3）直接拒绝。
  没有这个，supervisor 套 supervisor 会无限递归烧穿

## 2. 总体架构

```
   前端通道（平级，可并存）                        agenthub（单进程）                 后端 agent（ACP 子进程）

 ┌──────────────────────┐                ┌────────────────────────────┐
 │  IM 通道             │──┐             │  ┌──────────────────────┐  │
 │  (Octop / 企微 / …)   │  │             │  │ channel adapters     │  │  IM/NATS 归一化
 └──────────────────────┘  │             │  ├──────────────────────┤  │
                           ├── 接入 ────▶│  │ session manager      │  │  会话 ↔ agent 绑定
 ┌──────────────────────┐  │             │  ├──────────────────────┤  │
 │  NATS 通道           │──┘             │  │ agent pool           │  │  子进程生命周期
 │  (NATS client)       │                │  ├──────────────────────┤  │
 └──────────────────────┘                │  │ acp_client           │  │  stdio JSON-RPC 分帧
          │                              │  └──────────────────────┘  │
          │ 消息到达 / 回复               └─────────────┬──────────────┘
          ▼                                            │ stdio / ACP
   ┌──────────────┐                                    │
   │ NATS Server  │ 独立部署               ┌────────────┼────────────┬──────────────┐
   │ (总线本体)    │                       │            │            │              │
   └──────────────┘                  ┌────▼────┐  ┌────▼────┐  ┌────▼────┐   ┌────▼─────┐
                                     │claude-  │  │  codex  │  │opencode │   │ pi-agent │
                                     │code acp │  │   acp   │  │  acp    │   │   acp    │
                                     └─────────┘  └─────────┘  └─────────┘   └──────────┘
```

角色说明：

- **ACP 侧**：agenthub 是 **client**，agent 子进程是 server
- **NATS 侧**：NATS Server 是**独立部署**的总线本体；agenthub 是
  **NATS client**——作为客户端接入 NATS Server，负责把消息收进来、
  把结果/事件发回去（`bots.agenthub.in` / `.out`，见 §3）
- **IM 侧**：agenthub 通过对应的 IM channel adapter 收发消息
  （复用 Octop 的 channel 机制）
- agenthub 整体定位：**前端通道与 ACP agent 之间的归一化 + 会话管理层**

## 3. NATS 通道接口设计

> 本节只描述 NATS 通道的报文约定；IM 通道走各自 channel adapter 的
> 消息格式，语义与本节 op/event 一一对应。

### 3.1 主题约定（沿用 bots.* 体系）

| 主题 | 方向 | 用途 |
|---|---|---|
| `bots.agenthub.in` | 入 | 服务请求（request/reply） |
| `bots.agenthub.out` | 出 | 事件流（会话进度，供订阅） |
| `bots.registry.upsert` / `.down` | 出 | 服务发现广播（见 §6） |

### 3.2 请求协议（NATS request，JSON）

`prompt` 请求字段说明：`session_id` 可选（缺省=新建会话）；`agent` 为
`list_agents` 中的名字；`cwd` 必须在白名单内（§7）；`depth` 编排深度
默认 0；`timeout_ms` 为客户端侧超时。

```json
{
  "v": 1,
  "op": "prompt",
  "session_id": "s-abc123",
  "agent": "claude-code",
  "cwd": "/path/to/workdir",
  "depth": 0,
  "text": "把 config.py 里的超时改成 60s 并跑通测试",
  "timeout_ms": 600000
}
```

响应（reply inbox）分两类：

- **终态响应**（request 的直接回包）：

  ```json
  { "v": 1, "op": "prompt", "ok": true,
    "session_id": "s-abc123", "depth": 0,
    "stop_reason": "end_turn",
    "result_text": "已改完，测试 8/8 通过" }
  ```
- **过程事件**（PUB 到 `bots.agenthub.out`，不占 request）：

  ```json
  { "v": 1, "event": "agent_message_delta", "session_id": "s-abc123",
    "seq": 42, "ts": 1728100000.123, "payload": { "text_chunk": "..." } }
  ```

### 3.3 op 目录

| op | 语义 | 对应 ACP 方法 |
|---|---|---|
| `list_agents` | 列出可用 agent 及能力 | Agent Card 思路（自报家门） |
| `new_session` | 建会话（cwd/mcp_servers/model/depth） | `session/new` |
| `load_session` | 恢复历史会话 | `session/load` |
| `prompt` | 提交一轮任务 | `session/prompt` |
| `cancel` | 打断进行中的 turn | `session/cancel` |
| `permission_reply` | 回答权限请求 | （对应 `session/request_permission` 的应答） |
| `get_state` | 补拉会话当前状态（丢事件兜底，M2） | — |

### 3.4 事件类型（ACP session/update → NATS event 1:1 映射）

| ACP update | NATS event | 说明 |
|---|---|---|
| agent_message_chunk | `agent_message_delta` | 流式文本增量 |
| agent_thought_chunk | `agent_thinking` | 思考流 |
| tool_call (start) | `tool_start` | 读/编辑/执行等，带 diff |
| tool_call_update | `tool_end` | 工具结果 |
| plan | `plan` | todo 列表变化 |
| request_permission | `permission_request` | **同时挂起 turn**，等 `permission_reply` |
| turn 结束 | `turn_end` | 终态，含 stop_reason |

事件带单调 `seq`（每会话独立），消费者可检测丢失并触发 `get_state` 补拉。

## 4. ACP 客户端层（acp_client.py）

- 传输：`subprocess.Popen(stdin/stdout)`，JSON-RPC 2.0 按
  `Content-Length` 头分帧（LSP 风格）。M0 手写分帧，零依赖
  （协议只有一层帧格式，不值得引 SDK）。
- 初始化握手：`initialize`（协议版本协商 + capabilities 交换）→
  确认 loadSession 等能力。
- 必须实现的 **client 侧方法**（agent 会反向调用）：
  - `fs/read_text_file` / `fs/write_text_file`：默认透传真实文件系统
    （本地全权模式；未来需要审批时在这两个入口挂检查点）
  - `terminal/create`：透传，记录审计日志
  - `session/request_permission`：**不直接回答**，转成总线
    `permission_request` 事件挂起，等 `permission_reply` 到达后回包
    （超时按保守项处理，默认 deny）
- 每个会话独立一个 agent 子进程（M0；进程池复用 M2）。

## 5. Agent 进程池（agent_registry.py）

配置驱动（config.json，JSON 格式；DB 建立前的引导配置）：

```json
{
  "agents": {
    "claude-code": {
      "cmd": ["claude", "--acp"],
      "cwd_whitelist": ["~/projects/*"],
      "default_model": null
    },
    "opencode": {
      "cmd": ["opencode"],
      "cwd_whitelist": ["~/projects/*"]
    }
  },
  "session": {
    "idle_ttl_s": 1800,
    "max_sessions": 8
  },
  "orchestration": {
    "max_depth": 3
  },
  "nats": {
    "url": "nats://127.0.0.1:4222",
    "inbox": "bots.agenthub.in",
    "events": "bots.agenthub.out"
  }
}
```

- 启动时不预拉进程；首个 new_session 拉起，idle_ttl 到期 kill。
- 崩溃检测：进程退出 → 该会话所有挂起 request 返回 `session_crashed`
  错误事件；M2 支持自动重启 + `load_session` 恢复。

## 6. 服务发现

agenthub 启动后向 `bots.registry.upsert` 广播自述（A2A Agent Card 的总线版）：

```json
{ "service": "agenthub", "node": "<hostname>",
  "inbox": "bots.agenthub.in",
  "agents": [ {"name": "claude-code", "modes": ["text"],
               "capabilities": ["load_session"]} ],
  "version": "0.2.0" }
```

心跳 20s（TTL 60s）。M1 简单广播；M2 若引入 JetStream KV 注册中心
（nats-sync 加 KV 子集后），直接挂接，支持客户端冷启动全量拉取与
崩溃自动下线。

## 7. 安全边界

1. **cwd 白名单**：new_session 的 cwd 必须命中 `cwd_whitelist` glob
2. **权限守门**：request_permission 一律上抛总线（人工/规则回答），
   agenthub 绝不自动 allow 危险操作（delete/execute 类）
3. **输出限流**：事件流单会话限速（50 事件/s，超出合并 delta），
   防止 agent 疯狂输出打爆总线
4. **深度护栏**：max_depth 递归上限（§1）
5. **无鉴权假设**：内网 NATS 信任域内运行；暴露公网前必须加
   NATS 账号体系 + 每会话 token

## 8. Web 管理 UI（admin）

一个内嵌的本地 Web 控制台（M2+），单进程内置（FastAPI + 静态页，
默认 `127.0.0.1:8399`，只绑 loopback），不用独立前端工程。

### 8.1 配置管理

| 页面 | 可配置项 | 落点 |
|---|---|---|
| **前端通道** | 两个平级通道各自独立配置：**IM**（哪个会话/群 ↔ 默认 agent、事件流是否回推、权限请求是否弹卡片）与 **NATS**（url、inbox/events 主题、订阅模式） | DB（sqlite）+ 热生效（IM/agent/编排项；NATS 连接项改后需重启） |
| **Agent 定义** | agent 名、启动 cmd、cwd 白名单、default_model、MCP servers 注入、idle_ttl、并发上限 | DB + agent_registry 热加载（下个新会话生效，存量会话不动） |
| **编排** | max_depth、事件限流阈值、预算上限 | DB |

- 配置来源演进：v0.2 起以 **sqlite DB 为唯一事实源**，json 只做首次
  引导导入（config.json → DB）；UI 改 DB，运行时读 DB
- 配置变更写审计日志（who/when/what）

### 8.2 运行操作

- 会话列表：强制断开、cancel 当前 turn
- agent 进程：手动重启、kill、查看最近 stdout/stderr 环形缓冲
- 权限请求队列：UI 直接 allow/deny（和 IM 卡片、总线 `permission_reply` 三入口同权）

### 8.3 鉴权

- 默认仅 loopback + 本地登录（首次启动生成一次性 token 打印到终端）
- 若要远程访问：走 nginx 反代 + 域名 + TLS（沿用 Gitea 的部署模式），
  且必须启用账号密码（argon2 存储）

## 9. `/switch`：IM 会话的 agent 切换

IM 前端命令（Octop 侧 `/switch` 或文本指令），作用于 **IM 会话 →
agent 进程** 的绑定关系：

```
/code 修复登录 bug            # 走当前绑定 agent（默认 claude-code）
/switch codex                 # 当前会话改绑 codex（新建会话，历史不带走）
/switch codex --continue      # 若 codex 有该会话的 ACP session 存档则 load
/switch                       # 列出可用 agent（来自 list_agents）
/status                       # 查看当前绑定 + 进程状态
```

语义细节：

- 绑定关系存在 **DB：`(channel, chat_id) -> agent_name, acp_session_id`**，
  IM 网关每次 prompt 前查表；agenthub 只存全局默认
- 切换 = 解绑旧会话（旧 ACP 子进程按 idle_ttl 自然回收，不立即杀）
  + 新建目标 agent 会话；`--continue` 才尝试 `session/load` 迁移历史
  （注意：不同 agent 的会话历史格式不互通，跨 agent continue 只迁移
  agenthub 侧保存的对话文本，tool state 不迁移——文档必须写明）
- 权限校验：`/switch` 需要配置允许的 agent 集合（防止 IM 用户切到
  白名单外的 agent）；企微侧按用户身份过滤

## 10. 可视化 dashboard

Web UI 中的只读监控页（与 §8 同进程），数据全部来自 agenthub 内存
指标 + 环形缓冲，零外部依赖（不引 Prometheus，M3 起可选暴露
`/metrics`）：

| 面板 | 内容 |
|---|---|
| **拓扑总览** | 前端通道（IM/NATS）→ agenthub → 各 agent 进程的实时连线图；每条边上滚动显示最近事件类型 |
| **Agent 进程** | 每进程：状态、CPU/内存、会话数、最近 turn、最近错误 |
| **会话流水** | 活跃会话表：绑定关系、当前 turn 耗时、token 消耗估算、depth；点开看事件时间线（tool_start/tool_end/delta 时间轴） |
| **吞吐与延迟** | 事件速率曲线（按类型）、prompt 端到端延迟分位数（p50/p95）、排队数 |
| **权限与安全** | 权限请求历史（谁批的、多久）、被拒次数、depth 触顶/限流触发计数 |
| **总线健康** | NATS 连接状态、inbox 请求速率、事件发布失败数 |

实现：后端 WebSocket 推送增量 + REST 快照；前端单页静态 JS
（无构建链，直接引 CDN 或本地 vendor 文件）。

## 11. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M0** | acp_client 分帧层 + initialize/session/new/prompt 通路；接通 claude-code 跑通一次真实会话（stdio 直连测试，不进总线） | 单测 + 手工脚本输出完整 update 流 |
| **M1** | nats_service + 事件映射 + permission 挂起/恢复 + 发现广播；sqlite 配置/绑定存储 | pytest e2e 全绿（含 cancel、crash） |
| **M2** | 会话恢复、进程池、get_state 补拉、JetStream KV 注册中心、depth 护栏；**Web admin 基础版**（agent/通道配置页 + 会话管理） | 杀进程恢复会话；UI 改配置热生效 |
| **M3** | supervisor 形态验证 + **dashboard 监控页**（拓扑/流水/延迟面板） | 一次 IM 指令 → 多 agent 协作 → 汇总；面板数据与实际一致 |
| **M4** | Octop 侧 `/code` + **`/switch`/`/status`** 端到端 | 企微切换 agent 后派活、流式进度、汇总回包 |

## 12. 风险与已知坑

- ACP 官方 SDK 主要是 Rust/TS；Python 侧手写分帧（M0 决策，减依赖）
- `claude --acp` shim 的认证：agent 自己管 API key，agenthub 不碰凭据
- stdout 混入非协议输出会破坏分帧 → 解析失败整会话标记 crashed，日志落盘
- 事件回放：NATS core 不持久化，消费者掉线丢事件 → `get_state` 补拉
  兜底（M2），或迁 JetStream
- supervisor 自我递归：同 agent 既当 supervisor 又当 worker 时 depth
  护栏是唯一防线，max_depth 不可配置过大

## 13. 命名与发布

- 仓库：`vxquant/agenthub`（Gitea 主仓；按发布流程，稳定后镜像 GitHub）
- import/包名：`agenthub`（PyPI 的 `agenthub` 当前空闲，可用；
  `bothub`/`agent-hub` 均已被占，弃用）
- 未来若发布 PyPI：直接用 `agenthub` 名
