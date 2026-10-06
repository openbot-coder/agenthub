# ACP 协议速查（Agent Client Protocol, Zed 系）

> 供 acp-bridge 实现时对照。规范：agentclientprotocol.com
> （spec 源：zed-industries/zed repo docs/src/ai/external-agents.md）

## 传输与分帧

- JSON-RPC 2.0 over stdio，LSP 风格 `Content-Length: N\r\n\r\n` 头分帧。
- agent 子进程 = ACP **server**；桥 = ACP **client**。

## 生命周期（client → server）

1. `initialize`：`{protocolVersion, clientCapabilities}`
   → 返回 agent 能力（loadSession、promptCapabilities…）
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

## 实现提醒

- id 复用规则：同方向自增；notification 无 id。
- stdout 只能走协议帧——任何 agent 混入的裸日志都会破坏分帧。
- 权限请求必须回应，否则 agent turn 会挂死；桥侧超时默认拒绝。
- 版本协商失败（protocolVersion 不兼容）→ 直接报错，不要猜。
