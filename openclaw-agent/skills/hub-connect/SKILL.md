# Hub 连接注册 (hub-connect)

## 简介

本 Skill 用于将 OpenClaw 客户端连接到 Hub 管理中心，完成身份认证并建立通信。

**适用场景**：新 OpenClaw 客户端首次启动时，需要连接到 Hub 获取任务、上报状态。

---

## 前置条件

OpenClaw 客户端需要在 Hub 管理界面提前注册，获得以下信息：
- `HUB_API_TOKEN` - 从 Hub 管理界面获取的 API Token
- `CLAW_ID` - 本 OpenClaw 在 Hub 注册时的 ID

---

## Hub 连接信息

```
Hub 地址: http://your-hub-host:8088
API 版本: v1
```

---

## 连接方式

Hub 支持两种通信方式：**SSE 长连接**（推荐）和**轮询**。

### 方式一：SSE 长连接（推荐）

SSE (Server-Sent Events) 建立持久连接，Hub 可实时推送任务和消息。

**获取任务流**：
```
GET /api/openclaws/{CLAW_ID}/events

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Accept: text/event-stream
```

**响应示例**：
```
event: connected
data: {"claw_id": 4, "name": "龙虾王", "server_time": "2026-03-29T10:00:00"}

event: heartbeat
data: {"server_time": "2026-03-29T10:05:00"}

event: task
data: {"task_id": "task_xxx", "task_type": "code_review", "command": "审查代码"}
```

**重要提示**：SSE 端点路径是 `/api/openclaws/{CLAW_ID}/events`（注意：没有 `/v1/` 前缀）。如果连接失败，请先验证 Token 是否有效：`GET /api/v1/openclaws/{CLAW_ID}/config`

---

### 方式二：轮询

定期调用 API 查询最新任务和消息。

**查询待处理任务**：
```
GET /api/v1/openclaws/{CLAW_ID}/tasks?status=pending

Header:
  Authorization: Bearer {HUB_API_TOKEN}
```

**响应示例**：
```json
{
  "tasks": [
    {
      "id": 123,
      "type": "code_review",
      "content": "审查 PR #456",
      "priority": "high",
      "created_at": "2026-03-29T10:00:00Z"
    }
  ]
}
```

---

## 核心 API

### 1. 验证连接

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/config`

**说明**：验证 Token 有效，获取本 OpenClaw 的配置信息。

**响应示例**：
```json
{
  "id": 4,
  "name": "龙虾王",
  "claw_tag": "claw-lobster",
  "project_name": "智能助手",
  "module_name": "nlp",
  "report_schedule": "15:00,21:00",
  "hub_url": "http://your-hub-host:8088"
}
```

---

### 2. 心跳上报

**接口**：`POST /api/v1/openclaws/{CLAW_ID}/heartbeat`

**说明**：定期发送心跳，证明 OpenClaw 在线。建议**每 30 秒**调用一次。

**请求体**：无

**响应示例**：
```json
{"status": "ok", "server_time": "2026-03-29T15:00:00Z"}
```

---

### 3. 获取分配的任务

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/tasks`

**说明**：获取 Hub 分配给本 OpenClaw 的任务列表。

**查询参数**：
- `status`：任务状态 (`pending` | `in_progress` | `completed`)
- `limit`：返回数量（默认 10）

**响应示例**：
```json
{
  "tasks": [
    {
      "id": 123,
      "type": "code_review",
      "title": "审查 PR #456",
      "description": "请审查以下代码...",
      "priority": "high",
      "status": "pending",
      "created_at": "2026-03-29T10:00:00Z"
    }
  ],
  "total": 5,
  "unread_count": 2
}
```

---

### 4. 更新任务状态

**接口**：`PUT /api/v1/openclaws/{CLAW_ID}/tasks/{TASK_ID}`

**说明**：更新任务执行状态。

**请求体**：
```json
{
  "status": "in_progress",
  "progress": 50
}
```

或完成任务：
```json
{
  "status": "completed",
  "result": "已审查代码，发现 3 个问题"
}
```

---

### 5. 提交工作日报

**接口**：`POST /api/v1/openclaws/{CLAW_ID}/report`

**说明**：向 Hub 提交工作日报。

**请求体**：
```json
{
  "report_date": "2026-03-29",
  "report_time": "15:00",
  "tasks_completed": "1. 完成了用户登录功能\n2. 修复了支付bug",
  "knowledge_recorded": "学习了新的加密算法",
  "experience_shared": "分享了代码审查经验",
  "knowledge_learned": "今天学到了 Rust 异步编程",
  "ai_summary": "AI 自动总结：本周完成了 5 个功能开发..."
}
```

---

### 6. 获取分配的知识库条目

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/knowledge`

**说明**：获取 Hub 分配给本 OpenClaw 的知识库内容。

**响应示例**：
```json
{
  "knowledge": [
    {
      "id": 1,
      "title": "代码审查规范",
      "content": "1. 检查命名规范\n2. 检查安全漏洞...",
      "category": "规范",
      "updated_at": "2026-03-28T10:00:00Z"
    }
  ]
}
```

---

### 7. 提交知识库条目

**接口**：`POST /api/v1/openclaws/{CLAW_ID}/knowledge`

**说明**：向 Hub 提交新的知识库条目。

**请求体**：
```json
{
  "title": "JWT 认证原理",
  "content": "JWT (JSON Web Token) 是...",
  "category": "技术",
  "tags": ["认证", "安全"]
}
```

---

### 8. 获取分配的 Skills

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/assigned-skills`

**说明**：获取 Hub 分配给本 OpenClaw 的 Skills 配置。

**响应示例**：
```json
{
  "skills": [
    {
      "id": 1,
      "name": "代码审查",
      "display_name": "代码审查助手",
      "description": "帮助审查代码质量和安全问题",
      "template_content": "请帮我审查以下代码...",
      "trigger_phrase": "审查代码"
    }
  ]
}
```

---

### 9. 获取分配的行为规范

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/assigned-rules`

**说明**：获取 Hub 分配给本 OpenClaw 的 Rules（行为规范）。

**响应示例**：
```json
{
  "rules": [
    {
      "id": 1,
      "name": "安全规范",
      "display_name": "安全编程规范",
      "description": "代码必须符合安全编程规范",
      "content_template": "1. 不执行危险命令\n2. 验证用户输入..."
    }
  ]
}
```

---

### 10. 获取消息通知

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/messages`

**说明**：获取发送给本 OpenClaw 的消息。

**查询参数**：
- `unread_only=true`：只返回未读消息

**响应示例**：
```json
{
  "messages": [
    {
      "id": 99,
      "type": "system",
      "content": "欢迎使用 OpenClaw 系统",
      "from": "system",
      "created_at": "2026-03-29T09:00:00Z",
      "read": false
    }
  ]
}
```

---

## 完整连接流程

```
1. 启动时
   ├── 调用 GET /config 验证连接，获取配置
   └── 调用 GET /assigned-skills 和 GET /assigned-rules 获取分配的 Skills 和 Rules

2. 运行时（选择一种方式）
   ├── 方式A（SSE长连接）：
   │   └── 调用 GET /events 建立 SSE 连接，实时接收任务和消息
   │       （SSE 端点：/api/openclaws/{CLAW_ID}/events）
   │
   └── 方式B（轮询）：
       ├── 每 30 秒调用 POST /heartbeat 保持在线
       ├── 每 60 秒调用 GET /tasks 查询新任务
       └── 每 5 分钟调用 GET /messages 查询新消息

3. 定时任务
   ├── 按 report_schedule 时间调用 POST /report 提交日报
   └── 每小时调用 GET /assigned-skills 检查 Skill 更新
```

---

## 代码示例

### Python SSE 长连接

```python
import requests
import sseclient

CLAW_ID = "4"
HUB_API_TOKEN = "your_token_here"
HUB_URL = "http://your-hub-host:8088"

headers = {
    "Authorization": f"Bearer {HUB_API_TOKEN}",
    "Accept": "text/event-stream"
}

response = requests.get(
    f"{HUB_URL}/api/openclaws/{CLAW_ID}/events",
    headers=headers,
    stream=True
)

client = sseclient.SSEClient(response)
for event in client.events():
    print(f"收到事件: {event.event}, data: {event.data}")
    
    if event.event == "connected":
        # 连接成功
        data = json.loads(event.data)
        print(f"已连接 OpenClaw: {data.get('name')}")
    
    elif event.event == "task":
        # 处理新任务
        data = json.loads(event.data)
        print(f"新任务: {data}")
    
    elif event.event == "heartbeat":
        # 心跳
        pass
    
    elif event.event == "ping":
        # 保持连接
        pass
```

### Python 轮询方式

```python
import requests
import time

CLAW_ID = "4"
HUB_API_TOKEN = "your_token_here"
HUB_URL = "http://your-hub-host:8088"

headers = {"Authorization": f"Bearer {HUB_API_TOKEN}"}

def heartbeat():
    """保持在线"""
    requests.post(f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/heartbeat", headers=headers)

def fetch_tasks():
    """获取待处理任务"""
    resp = requests.get(
        f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/tasks",
        params={"status": "pending"},
        headers=headers
    )
    return resp.json().get("tasks", [])

def main():
    # 验证连接
    config = requests.get(f"{HUB_URL}/api/v1/openclaws/{CLAW_ID}/config", headers=headers)
    print(f"连接成功: {config.json()}")
    
    while True:
        heartbeat()
        
        tasks = fetch_tasks()
        for task in tasks:
            print(f"处理任务: {task}")
        
        time.sleep(30)  # 每30秒心跳

if __name__ == "__main__":
    main()
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` 配置 |
| 403 | Token 无效 | 从 Hub 重新获取 Token |
| 404 | OpenClaw 未注册 | 联系管理员在 Hub 注册 |
| 429 | 请求过于频繁 | 降低轮询频率 |
| 500 | 服务器错误 | 检查 Hub 服务状态 |
| 503 | 服务不可用 | Hub 不在线，稍后重试 |

---

## 触发词

- "连接 Hub"
- "注册到 Hub"
- "同步 Hub 配置"
- "获取分配的任务"
- "提交日报"
- "查看知识库"
- "心跳"
