# agenthub 设计方案

> 版本：v0.2（2026-10-05）
> 目标：把本地 ACP 编码 agent 桥接到 NATS 消息总线上，形成可发现、可派活、
> 可编排的 agent 服务网格。IM（Octop）只是前端的一种。

## 0. 问题定义与形态演进

现状两条链路各自独立：

- Octop/botflow：**IM 用户 ↔ Agent**，传输 NATS（`bots.*` 主题）
- 编码 agent（Claude Code / Codex / Gemini CLI / OpenCode …）：**编辑器 ↔ Agent**，传输 ACP（stdio JSON-RPC）

### 形态定位（v0.2 关键修正）

agenthub **不绑定任何前端**。它的入口就是消息总线：

```
形态 B（本设计）：
  IM(Octop) ──┐
  总管 agent ──┼──▶ NATS ──▶ agenthub ──▶ codex / opencode / pi / gemini...
  cron/CI  ───┘        （stdin/ACP 子进程）     （可再派生子任务，最终汇总回入口）
```

- 对前：暴露统一的 op 接口（prompt/new_session/...），调用方是 IM 网关、
  supervisor agent、cron，**一视同仁**
- 对后：把 ACP agent 变成总线上的可发现服务

**为什么走 ACP 而不是直接 shell 调 CLI**：
- 统一权限审批（ACP 反转文件系统所有权，客户端可审计每次 read/edit/execute）
- 统一事件流（session/update 分类事件，天然映射成总线消息）
- 一次接入，所有 ACP 兼容 agent 通用（40+，还在增长）

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
                      NATS bus
   ┌──────────┐   ┌───────────────────────────┐   ┌──────────────┐
   │ Octop IM │──▶│ bots.agenthub.in (svc)    │◀──│ supervisor   │
   └──────────┘   └────────────┬──────────────┘   │ agent/cron等 │
                               │                  └──────────────┘
                    ┌──────────▼──────────┐
                    │      agenthub       │
                    │  ┌───────────────┐  │
                    │  │ nats_service  │  │  订阅/发布/发现广播
                    │  ├───────────────┤  │
                    │  │ session mgr   │  │  nats req <-> acp session
                    │  ├───────────────┤  │
                    │  │ agent pool    │  │  子进程生命周期管理
                    │  ├───────────────┤  │
                    │  │ acp_client    │  │  stdio JSON-RPC 分帧
                    │  └───────────────┘  │
                    └──────────┬──────────┘
                    stdio/ACP  │
        ┌──────────────┬───────┴─────┬──────────────┐
   ┌────▼────┐    ┌────▼────┐   ┌────▼────┐   ┌─────▼────┐
   │claude-  │    │ codex   │   │gemini   │   │ opencode │
   │acp shim │    │ (native)│   │cli --acp│   │ (native) │
   └─────────┘    └─────────┘   └─────────┘   └──────────┘
```

角色说明：agenthub 在 ACP 协议里是 **client**；agent 子进程是 server。
NATS 侧 agenthub 是 **server**。hub 是两种协议之间的翻译层。

## 3. 总线接口设计

### 3.1 主题约定（沿用 bots.* 体系）

| 主题 | 方向 | 用途 |
|---|---|---|
| `bots.agenthub.in` | 入 | 服务请求（request/reply） |
| `bots.agenthub.out` | 出 | 事件流（会话进度，供订阅） |
| `bots.registry.upsert` / `.down` | 出 | 服务发现广播（见 §6） |

### 3.2 请求协议（NATS request，JSON）

```jsonc
// 派任务
// PUB bots.agenthub.in  <reply inbox>
{
  "v": 1,
  "op": "prompt",
  "session_id": "s-abc123",        // 可选；缺省=新建会话
  "agent": "claude-code",          // agent 名（见 list_agents）
  "cwd": "/path/to/workdir",       // 必须在白名单内（§7）
  "depth": 0,                      // 编排深度，默认 0
  "text": "把 config.py 里的超时改成 60s 并跑通测试",
  "timeout_ms": 600000
}
```

响应（reply inbox）分两类：

- **终态响应**（request 的直接回包）：
  ```jsonc
  { "v": 1, "op": "prompt", "ok": true,
    "session_id": "s-abc123", "depth": 0,
    "stop_reason": "end_turn",
    "result_text": "已改完，测试 8/8 通过" }
  ```
- **过程事件**（PUB 到 `bots.agenthub.out`，不占 request）：
  ```jsonc
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

配置驱动（config.example.yaml）：

```yaml
agents:
  claude-code:
    cmd: ["claude", "--acp"]         # 或 npx @anthropic-ai/claude-agent-acp
    cwd_whitelist: ["~/projects/*"]
    default_model: null
  opencode:
    cmd: ["opencode"]
    cwd_whitelist: ["~/projects/*"]
session:
  idle_ttl_s: 1800        # 空闲会话进程回收
  max_sessions: 8
orchestration:
  max_depth: 3            # supervisor 递归上限
nats:
  url: "nats://127.0.0.1:4222"
  inbox: "bots.agenthub.in"
  events: "bots.agenthub.out"
```

- 启动时不预拉进程；首个 new_session 拉起，idle_ttl 到期 kill。
- 崩溃检测：进程退出 → 该会话所有挂起 request 返回 `session_crashed`
  错误事件；M2 支持自动重启 + `load_session` 恢复。

## 6. 服务发现

agenthub 启动后向 `bots.registry.upsert` 广播自述（A2A Agent Card 的总线版）：

```jsonc
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

## 8. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M0** | acp_client 分帧层 + initialize/session/new/prompt 通路；接通 claude-code 跑通一次真实会话（stdio 直连测试，不进总线） | 单测 + 手工脚本输出完整 update 流 |
| **M1** | nats_service + 事件映射 + permission 挂起/恢复 + 发现广播；e2e：NATS 客户端发 prompt → 收全事件流 → 终态回包 | pytest e2e 全绿（含 cancel、crash） |
| **M2** | 会话恢复（session/load）、进程池、get_state 补拉、JetStream KV 注册中心接入、depth 护栏 | 杀进程重启恢复会话；递归派生被拒绝 |
| **M3** | supervisor 形态验证：codex 作为 supervisor 派子任务给 opencode 并汇总 | 一次 IM 指令 → 多 agent 协作 → 汇总回包 |
| **M4** | Octop 侧接 `/code` 命令走通 IM 端到端 | 企微里下指令看到流式进度 |

## 9. 风险与已知坑

- ACP 官方 SDK 主要是 Rust/TS；Python 侧手写分帧（M0 决策，减依赖）
- `claude --acp` shim 的认证：agent 自己管 API key，agenthub 不碰凭据
- stdout 混入非协议输出会破坏分帧 → 解析失败整会话标记 crashed，日志落盘
- 事件回放：NATS core 不持久化，消费者掉线丢事件 → `get_state` 补拉
  兜底（M2），或迁 JetStream
- supervisor 自我递归：同 agent 既当 supervisor 又当 worker 时 depth
  护栏是唯一防线，max_depth 不可配置过大

## 10. 命名与发布

- 仓库：`vxquant/agenthub`（Gitea 主仓；按发布流程，稳定后镜像 GitHub）
- import/包名：`agenthub`（PyPI 的 `agenthub` 当前空闲，可用；
  `bothub`/`agent-hub` 均已被占，弃用）
- 未来若发布 PyPI：直接用 `agenthub` 名
