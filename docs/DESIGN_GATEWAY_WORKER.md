# agenthub → gateway / worker 拆分设计（v1 草案）

## 目标形态

```
IM(企微) ──► gateway(api 机 hub)                worker(quant-host)
              │  总管 agent = deepagents         opencode acp (stdio, worker 本地)
              │  (进程内, LLM tool-calling)        ▲
              │      │ dispatch_task tool          │
              │      ▼                             │
              │  dispatcher ──HTTP/WS──► worker-agentd（ACP client + 请求透传）
              │                                    │
              └── 进度/结果/审批事件 ◄──HTTP/WS────┘
```

- **gateway**：现有 hub 原样保留（IM 通道、session 绑定、审批卡片、follow-up 队列、admin UI）。
  新增 `worker:<name>` agent 类型：对 SessionManager 来说它是一个普通 agent，但 ACP client
  不在本地拉起子进程，而是把 ACP 帧经 HTTP/WS 转发给远端 worker。
- **总管 agent**：deepagents（进程内），见关键设计 1。
- **worker**：新服务 `worker-agentd`，跑在 worker 机器上：本地 stdio 拉起 `opencode acp`，
  HTTP 收指令、WS/SSE 回事件（详见关键设计 2/3）。

## 关键设计决定

### 1. 总管 agent = deepagents（gateway 进程内，不再用 opencode）
- `pip install deepagents`（LangChain 官方 harness，LangGraph 之上），
  `create_deep_agent(model=..., system_prompt=..., tools=[dispatch_task, ...])`。
- 好处：派活就是普通 tool call（结构化、schema 校验、LangSmith 可观测），
  不用解析自由文本标记；sub-agents / planning / context 管理 / 权限中间件
  （human-in-the-loop）开箱即用，与 hub 审批语义天然对齐。
- 模型可配（tool-calling 模型均可），gateway 配置里加 `orchestrator` 段。
- 总管**不执行任务**，只有 dispatch_task 工具；task 描述由 LLM 生成。

### 2. 派活传输 = HTTP（POST）+ WS（事件回流），不用 NATS
- **指令面**：gateway dispatcher POST `http://<worker>:<port>/acp/dispatch`
  body {conn_id, frames:[...]}（ACP JSON-RPC 帧数组）→ 202。
- **事件面**：worker-agentd 主动连 gateway 的 WS `/acp/worker-events`
  （worker→gateway 单向更符合防火墙现实：worker 出站连 gateway），
  推送 {conn_id, frame}——update 流、request_permission、response 全走这里。
- 选型理由（vs ACP-over-HTTP RFD）：RFD 未定稿且 opencode 不支持 server 模式；
  这里的 HTTP/WS 是 **agenthub 私有信封**（帧仍 ACP JSON-RPC），将来
  opencode 支持官方传输时，worker-agentd 的信封可整体替换。
- 依赖最小：gateway 已有 stdlib ThreadingHTTPServer 先例；WS 用 websockets 库
  （或先 SSE 单向，v1 够用）。

### 3. worker-agentd（worker 机器新服务）
1. 本地 stdio 拉起 `opencode acp`（不变）；
2. HTTP POST 接收 dispatch：把帧写入本地 opencode stdin；
3. WS/SSE 把 opencode stdout 帧回传 gateway；
4. 心跳 + conn_id 复用（session/load 恢复）。

### 4. 审批链路零新代码
worker opencode 的 `session/request_permission` 帧原样经 WS 回 gateway →
现有审批卡片逻辑直接工作；点击→reply 帧沿 dispatch POST 回 worker。
depth+1 护栏在 dispatcher 注入。

### 5. 生命周期
- conn_id = gateway 侧 ACP session id；绑定表/idle reaper//new/switch 语义不变；
  reaper 关「NATS/WS 会话」→ worker-agentd 杀本地 opencode。
- worker 掉线：gateway pending ACP 请求以 ACPProcessDead 失败（现成机制）。

## 分期

- **M-w1（worker-agentd MVP）**：单 worker 单 agent。HTTP dispatch + SSE/WS 事件回流，
  stdio 透传。gateway 侧加 `worker:<name>` agent 类型（ACP transport=remote）。
  验收：curl 直接 dispatch → worker opencode 回帧全环（不经过总管）。
- **M-w2（deepagents 总管）**：gateway 进程内跑 deepagents 总管，
  tools=[dispatch_task(worker 名单+能力描述)]；system prompt 定义派活策略；
  LangSmith 可选。验收：IM 消息 → 总管派活 → worker 执行 → 结果/审批回 IM。
- **M-w3（可观测与运维）**：worker 心跳、gateway 状态页 worker 列表、dispatch 审计、
  总管派活失败重试策略。

## 风险

| 风险 | 缓解 |
|---|---|
| HTTP/WS 上大 payload（agent 输出大文本/文件） | 沿用 hub 900KB 分片先例；文件走 workspace 不走帧 |
| 双跳审批延迟 | 帧透传无额外 RTT 协商；卡片点击→gateway→HTTP→worker 单向链 |
| deepagents 依赖较重（langchain 栈） | 仅 gateway 安装；总管失败可降级直连默认 worker |
| 总管误派 | 结构化解析失败即不派；depth 护栏；worker 能力清单限定 |
| worker-agentd 崩溃丢会话 | gateway 端 ACPProcessDead 兜底 + 用户重试（与进程死亡同语义） |
