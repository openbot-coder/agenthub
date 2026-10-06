# ACP 协议速查（Agent Client Protocol, Zed 系）

> 供 agenthub 实现时对照。规范：agentclientprotocol.com
> （spec 源：zed-industries/zed repo docs/src/ai/external-agents.md）
>
> ⚠️ **实测修正（M0 验证）**：当前 ACP 实现的 stdio 传输是
> **newline-delimited JSON（ndjson）**——一行一条 JSON-RPC 消息，以 `\n` 分隔，
> **不是** LSP 的 `Content-Length` 帧。依据：官方
> `@agentclientprotocol/sdk` 的 `ndJsonStream()`，以及
> `@zed-industries/claude-code-acp` 实测（发 Content-Length 帧会报
> `Failed to parse JSON message: Content-Length: ...`）。
> agenthub 默认 transport=`ndjson`，保留 `lsp-frame` 兼容模式备旧实现。

## 传输与分帧

- JSON-RPC 2.0 over stdio：
  - **ndjson（默认/当前）**：`{"jsonrpc":...}\n`
  - lsp-frame（兼容备选）：`Content-Length: N\r\n\r\n{...}`
- agent 子进程 = ACP **server**；agenthub = ACP **client**。

## 生命周期（client → server）

1. `initialize`：`{protocolVersion, clientCapabilities}`
   → 返回 agent 能力（loadSession、promptCapabilities…）+ authMethods
2. `session/new`：`{cwd, mcpServers}` → `{sessionId}`
3. `session/load`（可选能力）：恢复会话，agent 重放全部历史为 update 流
4. `session/prompt`：`{sessionId, prompt: [content_block]}` → `{stopReason}`
   - content block: text / image / audio / resource_link
   - stopReason: end_turn | max_tokens | max_turn_requests | refused | cancelled
5. `session/cancel`：notification，随时打断

## server → client 通知与请求

- `session/update`（notification，核心事件流）：
  - sessionUpdate: agent_message_chunk | agent_thought_chunk |
    tool_call | tool_call_update | plan | available_commands_update
  - tool_call 字段：kind(read/edit/delete/move/search/execute/think/fetch)、
    status(pending/in_progress/completed/failed)、content、rawInput、locations
- `session/request_permission`（**request，需要回包**）：
  options 含 allow_once/allow_always/reject_once/reject_always → 回 optionId
- client 反向方法：`fs/read_text_file`、`fs/write_text_file`、
  `terminal/create`(output截断/exit)、`terminal/wait_for_exit`…

## 实测到的真实行为（claude-code-acp v0.16 / claude CLI 2.1.290）

- `initialize` 返回：`{protocolVersion: 1, agentCapabilities: {promptCapabilities:
  {image, embeddedContext}, mcpCapabilities: {http, sse}, loadSession: true,
  sessionCapabilities: {fork, list, resume}}, authMethods: [{id: "claude-login"}]}`
- `session/new` **未登录也能成功**（sessionId 立即返回）
- `session/prompt` 未登录返回 JSON-RPC 错误 **`-32000 Authentication required`**
  —— 认证失败走 error 通道，不是 stopReason
- 每个会话会先推一条 `available_commands_update` 通知

## 实现提醒

- id 复用规则：同方向自增；notification 无 id。
- stdout 只能走协议帧——任何 agent 混入的裸日志都会破坏分帧。
- 权限请求必须回应，否则 agent turn 会挂死；桥侧超时默认拒绝。
- 版本协商失败（protocolVersion 不兼容）→ 直接报错，不要猜。
- JSON-RPC 业务错误（如未认证 -32000）与传输层错误要分开处理：
  前者是 agent 的正常回复，不能当崩溃。
