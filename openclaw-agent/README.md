# OpenClaw Manager 子agent 插件（**LEGACY / FALLBACK**）

> ⚠️ **自 Phase 1 起，主推路径是 `mcp-servers/openclaw-hub`**（一个 stdio MCP server，
> 由 AI host 直接 spawn，进程内含 SSE client，工具集让 AI 真实回复消息）。
>
> 本目录的 Python sidecar 仅作为 **MCP 不可用时的兜底**：
> - host 不支持 MCP（极端环境）
> - 仅有 cron / nohup 形式部署的 OpenClaw
> - 调试/审计用途
>
> 兜底模式下，sidecar **不再**写"已转交"罐头回复，也**不再**把 todos_pending
> 反向 POST 回 `/messages`（消除自循环），改为把所有 SSE 事件写入
> `~/.qclaw/inbox/pending/`，由 AI host 通过 `hub-inbox` skill 在每回合开始时
> 主动处理。
>
> 详见 `mcp-servers/openclaw-hub/README.md` 与 `openclaw-agent/skills/hub-inbox/SKILL.md`。

---

部署在 OpenClaw 所在服务器上，作为 OpenClaw 和 Manager 之间的桥梁。

## 功能特性

- **SSE长连接**：主动连接 Manager，实时接收任务
- **配置同步**：读取/修改 OpenClaw 的 `openclaw.json` 配置
- **文件操作**：读写 workspace 目录下的 md 文件
- **命令执行**：执行 shell 命令（如重启服务）
- **结果上报**：任务执行完成后上报结果给 Manager

## 架构

```
┌─────────────────────────┐         SSE          ┌─────────────────────────┐
│     openclaw-manager     │◄─────────────────────│      子agent 插件        │
│   (clawteam.woa.com:18800)    │                      │   (部署在OpenClaw服务器) │
│                         │                      │                         │
│  GET /api/openclaws/<id>/│                      │  SSE Client             │
│       events (SSE)       │                      │  Task Executor          │
│                         │                      │  Claw Operator          │
└─────────────────────────┘                      └─────────────────────────┘
```

## 任务类型

| task_type | 说明 | 参数 |
|-----------|------|------|
| `modify_config` | 修改openclaw.json | payload: 配置更新内容 |
| `execute_command` | 执行shell命令 | command: 命令字符串 |
| `update_file` | 更新文件 | target_path, payload.content |
| `read_file` | 读取文件 | target_path |
| `delete_file` | 删除文件 | target_path |
| `list_files` | 列出文件 | payload.pattern |
| `restart` | 重启OpenClaw | - |
| `get_status` | 查询状态 | - |

## 部署

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置连接信息

**方式一：写入 agent.md（推荐）**

在 `~/.qclaw/agent.md` 中写入 YAML front matter：

```markdown
---
hub_url: http://clawteam.woa.com:18800
claw_id: 4
api_token: oc_tk_your_token_here
---

# 你的 OpenClaw 名字

你的 OpenClaw 人格描述...
```

> agent 启动时优先从 `agent.md` 读取配置，无需设置环境变量。

**方式二：环境变量（备选）**

```bash
export MANAGER_URL="http://clawteam.woa.com:18800"
export CLAW_ID="4"
export API_TOKEN="oc_tk_your_token_here"
export OPENCLAW_DIR="/root/.qclaw"
```

### 3. 启动

```bash
python main.py
```

### 4. 作为服务运行 (systemd)

创建 `/etc/systemd/system/openclaw-agent.service`:

> 推荐使用 agent.md 配置，systemd 中只需设置 OPENCLAW_DIR：

```ini
[Unit]
Description=OpenClaw Manager Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/openclaw-agent
Environment="OPENCLAW_DIR=/root/.qclaw"
ExecStart=/usr/bin/python3 /opt/openclaw-agent/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> 如果仍需使用环境变量覆盖，可添加 `Environment="MANAGER_URL=..."` 等。

```bash
sudo systemctl daemon-reload
sudo systemctl enable openclaw-agent
sudo systemctl start openclaw-agent
```

## Manager API

### SSE事件订阅
```
GET /api/openclaws/<claw_id>/events
Authorization: Bearer <api_token>
```

### 上报任务结果
```
POST /api/openclaws/<claw_id>/report
Authorization: Bearer <api_token>
Content-Type: application/json

{
    "task_id": "task_123456_abc123",
    "status": "completed",  // completed/failed
    "result": "执行结果...",
    "error": "错误信息（可选）"
}
```

### 派发任务 (Manager管理接口)
```
POST /api/openclaws/<claw_id>/dispatch
Content-Type: application/json

{
    "task_type": "modify_config",
    "command": "optional",
    "target_path": "optional",
    "payload": {
        "mcp": {
            "servers": {...}
        }
    }
}
```
