# acp-bridge 设计方案

> 版本：draft v0.1（2026-10-05）
> 目标：IM/NATS 总线 ↔ 本地 ACP agent 的双向桥

## 0. 问题定义

现状两条链路各自独立：

- Octop/botflow：**IM 用户 ↔ Agent**，传输 NATS（`bots.*` 主题）
- 编码 agent（Claude Code 等）：**编辑器 ↔ Agent**，传输 ACP（stdio JSON-RPC）

缺口：总线上没有"编码 agent"这个服务类别。写一个桥，把本地 ACP agent
注册成总线服务，编排方（Octop 机器人、其他 worker、未来 orchestrator）
就能像调普通服务一样派编码任务。

**为什么走 ACP 而不是直接 shell 调 CLI**：
- 统一权限审批（ACP 反转文件系统所有权，客户端可审计每次 read/edit）
- 统一事件流（session/update 分类事件，天然映射成总线消息）
- 一次接入，所有 ACP 兼容 agent 通用（40+，还在增长）

## 1. 总体架构

```
                      NATS bus
   ┌──────────┐   ┌───────────────────────────┐   ┌──────────────┐
   │ Octop IM │──▶│ bots.acp-bridge.in (svc)  │◀──│ 其他 worker  │
   └──────────┘   └────────────┬──────────────┘   └──────────────┘
                               │
                    ┌──────────▼──────────┐
                    │     acp-bridge      │
                    │  ┌───────────────┐  │
                    │  │ nats_service  │  │  订阅 svc 主题、发现广播
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

角色说明：acp-bridge 在 ACP 协议里是 **client**；agent 子进程是 server。
NATS 侧 acp-bridge 是 **server**。桥是两种协议之间的翻译层。

## 2. 总线接口设计

### 2.1 主题约定（沿用 bots.* 体系）

| 主题 | 方向 | 用途 |
|---|---|---|
| `bots.acp-bridge.in` | 入 | 服务请求（request/reply） |
| `bots.acp-bridge.out` | 出 | 事件流（会话进度，供订阅） |
| `bots.registry.upsert` / `.down` | 出 | 服务发现广播（见 §5） |

### 2.2 请求协议（NATS request，JSON）

```jsonc
// 派任务
// PUB bots.acp-bridge.in  <reply inbox>
{
  "v": 1,
  "op": "prompt",
  "session_id": "s-abc123",        // 可选；缺省=新建会话
  "agent": "claude-code",          // agent 名（见 2.4 发现接口）
  "cwd": "/path/to/workdir",       // 必须在白名单内（§6）
  "text": "把 config.py 里的超时改成 60s 并跑通测试",
  "timeout_ms": 600000
}
```

响应（reply inbox）分两类：

- **终态响应**（request 的直接回包）：
  ```jsonc
  { "v": 1, "op": "prompt", "ok": true,
    "session_id": "s-abc123", "stop_reason": "end_turn",
    "result_text": "已改完，测试 8/8 通过" }
  ```
- **过程事件**（PUB 到 `bots.acp-bridge.out`，不占 request）：
  ```jsonc
  { "v": 1, "event": "agent_message_delta", "session_id": "s-abc123",
    "seq": 42, "ts": 1728100000.123, "payload": { "text_chunk": "..." } }
  ```

### 2.3 op 目录

| op | 语义 | 对应 ACP 方法 |
|---|---|---|
| `list_agents` | 列出可用 agent 及能力 | Agent Card 思路（自报家门） |
| `new_session` | 建会话（可带 cwd/mcp_servers/model） | `session/new` |
| `load_session` | 恢复历史会话 | `session/load` |
| `prompt` | 提交一轮任务 | `session/prompt` |
| `cancel` | 打断进行中的 turn | `session/cancel` |
| `permission_reply` | 回答权限请求 | （对应 `session/request_permission` 的应答） |

### 2.4 事件类型（ACP session/update → NATS event 1:1 映射）

| ACP update | NATS event | 说明 |
|---|---|---|
| agent_message_chunk | `agent_message_delta` | 流式文本增量 |
| agent_thought_chunk | `agent_thinking` | 思考流 |
| tool_call (start) | `tool_start` | 读/编辑/执行等，带 diff |
| tool_call_update | `tool_end` | 工具结果 |
| plan | `plan` | todo 列表变化 |
| request_permission | `permission_request` | **同时挂起 turn**，等 `permission_reply` |
| turn 结束 | `turn_end` | 终态，含 stop_reason |

事件带单调 `seq`（每会话独立），消费者可检测丢失并触发全量补拉
（`op: get_state`，M2 实现）。

## 3. ACP 客户端层（acp_client.py）

- 传输：`subprocess.Popen(stdin/stdout)`，JSON-RPC 2.0 按
  `Content-Length` 头分帧（LSP 风格）。
- 初始化握手：`initialize`（协议版本协商 + capabilities 交换）→
  确认 `loadSession` 等能力。
- 必须实现的 **client 侧方法**（agent 会反向调用）：
  - `fs/read_text_file` / `fs/write_text_file`：默认透传真实文件系统
    （本地桥是全权模式；未来需要审批时在这两个入口挂检查点）
  - `terminal/create`：透传，但记录审计日志
  - `session/request_permission`：**不直接回答**，转成总线
    `permission_request` 事件挂起，等 `permission_reply` 到达后回包
    （超时按 `allow` 选项中保守项处理，默认 deny）
- 每个会话独立一个 agent 子进程（M0 简化；进程池复用见 §4）。

## 4. Agent 进程池（agent_registry.py）

配置驱动：

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
```

- 启动时**不**预拉进程；首个 new_session 拉起，idle_ttl 到期 kill。
- 崩溃检测：进程退出 → 该会话所有挂起 request 返回错误事件
  `session_crashed`；支持配置自动重启 + `load_session` 恢复（M2）。

## 5. 服务发现

桥启动后向 `bots.registry.upsert` 广播自述（A2A Agent Card 的总线版）：

```jsonc
{ "service": "acp-bridge", "node": "<hostname>",
  "inbox": "bots.acp-bridge.in",
  "agents": [ {"name": "claude-code", "modes": ["text"],
               "capabilities": ["load_session"]} ],
  "version": "0.1.0" }
```

心跳 20s 一次（TTL 60s）。M1 先做简单广播；M2 若引入 JetStream KV
注册中心（见总体规划），直接挂上去。

## 6. 安全边界

1. **cwd 白名单**：`new_session` 的 cwd 必须命中 `cwd_whitelist` glob。
2. **权限守门**：`request_permission` 一律上抛到总线（人工/规则回答），
   桥自身绝不自动 allow 危险操作（delete/execute 类）。
3. **输出限流**：事件流单会话限速（如 50 事件/s，超出合并 delta），
   防止 agent 疯狂输出打爆总线。
4. **无鉴权假设**：内网 NATS 信任域内运行；如果暴露到公网必须先加
   NATS 账号体系 + 每会话 token。

## 7. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M0** | acp_client 分帧层 + initialize/session/new/prompt 通路；接通 claude-code 跑通一次真实会话（stdio 直连测试，不进总线） | 单测 + 手工脚本输出完整 update 流 |
| **M1** | nats_service + 事件映射 + permission 挂起/恢复；e2e：NATS 客户端发 prompt → 收全事件流 → 终态回包 | pytest e2e 全绿（含 cancel、crash） |
| **M2** | 会话恢复（session/load）、进程池、注册中心接入、get_state 补拉 | 杀进程重启恢复会话 |
| **M3** | Octop 侧接一个 `/code` 命令走通 IM 端到端 | 企微里下指令看到流式进度 |

## 8. 风险与已知坑

- ACP 官方 SDK 主要是 Rust/TS；Python 侧走 `acp` PyPI 包或直接手写
  分帧（协议简单，M0 选择手写，减依赖）。
- `claude --acp` shim 的认证：agent 自己管 API key，桥不碰凭据。
- stdout 混入非协议输出（某些 agent 打日志到 stdout）会破坏分帧 →
  解析失败时整会话标记 crashed，日志落盘排查。
- 事件回放：NATS core 不持久化，消费者掉线丢事件 → M2 用
  `get_state` 补拉兜底，或迁 JetStream。
