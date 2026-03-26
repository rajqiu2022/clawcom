# OpenClaw Manager 子agent 插件

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
│   (your-hub-host:8088)    │                      │   (部署在OpenClaw服务器) │
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

### 2. 配置环境变量

```bash
export MANAGER_URL="http://your-hub-host:8088"
export CLAW_ID="4"
export API_TOKEN="oc_tk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export OPENCLAW_DIR="/root/.qclaw"
```

### 3. 启动

```bash
python main.py
```

### 4. 作为服务运行 (systemd)

创建 `/etc/systemd/system/openclaw-agent.service`:

```ini
[Unit]
Description=OpenClaw Manager Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/openclaw-agent
Environment="MANAGER_URL=http://your-hub-host:8088"
Environment="CLAW_ID=4"
Environment="API_TOKEN=your_token_here"
Environment="OPENCLAW_DIR=/root/.qclaw"
ExecStart=/usr/bin/python3 /opt/openclaw-agent/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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
