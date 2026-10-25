# 升级方案：拆分为 agenthub-gateway / acpclient 两个包

## 目标

把现有单体 `agenthub` 升级为两个独立发布的 Python 包：

1. **acpclient**（新独立包，零 gateway 依赖）
   - 迁入：`framing.py`（ndjson / lsp-frame 分帧）+ `acp_client.py`（ACPClient、
     ACPError、ACPProcessDead、handler 协议）
   - 新增（M-w1 需要的远程传输）：`RemoteACPClient`——与 ACPClient 同一接口
     （request/notify/register_*/initialize/session_*），transport 不是子进程
     stdin/stdout，而是 HTTP POST（指令）+ WS（事件回流），帧仍是 ACP JSON-RPC 原样。
   - 传输层抽象：`start()/close()/is_alive()/_send()/读循环` 收敛为 Transport 协议，
     StdioTransport（现有逻辑原样）与 RemoteTransport（新）两个实现。上层
     （session.py 的 pending/permission/relay 逻辑）完全不感知差别。
   - 发布名：`acpclient`（PyPI 空闲已确认过 agenthub 被占但 acpclient 待查），
     单独 repo `vxquant/acpclient`，CI 同 agenthub（Gitea 先行→GitHub）。

2. **agenthub-gateway**（原 agenthub 改名重发布）
   - 目录 `src/agenthub/` 不动（import 名保留 `agenthub`，避免大改 140 个测试与
     三台部署脚本），只改发行元数据：
     - `name = "agenthub-gateway"`，`[project.scripts] agenthub-gateway = "agenthub.__main__:main"`
     - 依赖加 `acpclient`（本地 path/内网 PyPI），删除内部 framing/acp_client 模块，
       `from acpclient import ...` 兼容 shim：`agenthub/acp_client.py` 留 3 行 re-export，
       老测试/老 import 不破坏。
   - gateway 侧后续新增（本升级不实现，仅留口）：`worker:<name>` agent 类型
     （cmd 字段换 url 字段）、deepagents orchestrator 段。

## 为什么保留 import 名 `agenthub`

- 140 tests + 三台部署（zd git pull、api rsync、proxy git）+ supervisor unit 文件
  都引用 `agenthub` 包与 `agenthub` 命令；改名 import 是纯机械风险零收益。
- 发行名与 import 名不一致是 Python 常态（`pip install agent-client-protocol`
  → `import acp`）。

## 实施清单（一次升级，向后兼容）

| # | 事项 | 验证 |
|---|---|---|
| 1 | 新 repo `vxquant/acpclient`：src/acpclient/{framing,client,transports}.py（代码从 agenthub 平移） | uv run pytest（迁过去的老测试 + RemoteTransport 单测 mock HTTP/WS） |
| 2 | acpclient pyproject：独立版本 0.1.0，零依赖（stdlib only，WS 用 websockets optional extra） | uv build + 安装冒烟 |
| 3 | agenthub pyproject：rename `agenthub-gateway`，deps + acpclient，scripts 改名，旧脚本 `agenthub` 保留别名 | 全量 140 tests 过（import 不变） |
| 4 | agenthub/acp_client.py 与 agenthub/framing.py 变 re-export shim | 老测试不动全绿 |
| 5 | Gitea 建 vxquant/acpclient、推送；agenthub-gateway 版本 bump 0.2.0 | push 成功 |
| 6 | 三台部署：zd/proxy git pull（requirements 解析 acpclient 需内网可装——Gitea 直装 `agenthub-gateway @ gitea+https` 或 vendor）；api rsync | 目标机 pytest + 重启 + status |
| 7 | CHANGELOG + DESIGN_GATEWAY_WORKER.md 更新实施状态 | — |

## RemoteACPClient 接口草案

```python
class RemoteACPClient:
    """ACPClient 兼容接口，帧走 HTTP/WS 到 worker-agentd。"""
    def __init__(self, base_url: str, worker_token: str | None = None): ...
    # 与 ACPClient 相同：request/notify/register_notification_handler/
    # register_request_handler/initialize/session_new/session_prompt/session_cancel
    # start() = 打开 WS 事件流 + POST initialize；close() = POST /close
    # is_alive() = WS 连接健康 + 心跳未超时
```

worker-agentd（后续 M-w1 实现，不在本升级）作为 acpclient 的姊妹服务，
复用 acpclient 的 framing/handler 协议。
