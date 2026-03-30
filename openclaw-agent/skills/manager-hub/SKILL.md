# Hub 通信 (manager-hub)

## 简介

本 Skill 用于与 Manager Hub 中心通信，获取任务、上报状态、提交工作日报。

**前置要求**：需要在 OpenClaw 客户端配置以下变量：
- `HUB_API_TOKEN` - 从 Hub 管理界面获取的 API Token
- `CLAW_ID` - 本 OpenClaw 的 ID（在 Hub 注册时分配）

## Hub 地址

```
http://9.134.11.169:8088/api/v1/openclaws
```

> 默认使用 HTTP，如需 HTTPS 请自行配置

## 认证方式

所有 API 调用需要在 Header 中携带 Token：

```
Authorization: Bearer {HUB_API_TOKEN}
```

---

## 可用工具

### 1. 心跳上报

**接口**：`POST /{CLAW_ID}/heartbeat`

**说明**：定期发送心跳，证明 OpenClaw 在线。**建议每 30 秒调用一次**。

**请求体**：无

**示例响应**：
```json
{"status": "ok"}
```

---

### 2. 获取配置

**接口**：`GET /{CLAW_ID}/config`

**说明**：获取 Hub 分配的个性化配置，包括项目信息、报告时间等。

**示例响应**：
```json
{
  "id": 4,
  "name": "龙虾王",
  "claw_tag": "claw-lobster",
  "project_name": "智能助手",
  "module_name": "nlp",
  "report_schedule": "15:00,21:00",
  "web_system_url": "https://xxx.com",
  "skills": [...]
}
```

---

### 3. 获取分配的 Skills

**接口**：`GET /{CLAW_ID}/assigned-skills`

**说明**：获取 Hub 分配给本 OpenClaw 的 Skills。当 Hub 管理员推送新 Skill 时，可通过此接口更新。

**示例响应**：
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

### 4. 获取分配的 Rules

**接口**：`GET /{CLAW_ID}/assigned-rules`

**说明**：获取 Hub 分配给本 OpenClaw 的 Rules（行为规范）。

**示例响应**：
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

### 5. 提交工作日报

**接口**：`POST /{CLAW_ID}/report`

**说明**：向 Hub 提交工作日报，包含今日完成的任务、遇到的问题等。

**请求体**：
```json
{
  "report_date": "2026-03-28",
  "report_time": "15:00",
  "tasks_completed": "1. 完成了用户登录功能\n2. 修复了支付bug",
  "knowledge_recorded": "学习了什么新知识",
  "experience_shared": "分享了什么经验",
  "knowledge_learned": "今天学到了什么",
  "ai_summary": "AI总结"
}
```

---

### 6. 获取日报历史

**接口**：`GET /{CLAW_ID}/reports`

**说明**：查看历史日报记录。

**查询参数**：
- `start_date`：开始日期 (YYYY-MM-DD)
- `end_date`：结束日期 (YYYY-MM-DD)

---

### 7. 发送消息

**接口**：`POST /api/openclaws/{CLAW_ID}/messages`

> 注意：此接口路径没有 `/v1/` 前缀，和 SSE 端点一样。

**说明**：OpenClaw 主动发送消息给 Web 管理端。

**请求体**：
```json
{
  "content": "消息内容",
  "msg_type": "text",
  "reply_to": 99
}
```

**msg_type 可选值**：`text` | `task_delegate` | `knowledge_share` | `request_help`

---

### 8. 获取消息历史

**接口**：`GET /api/openclaws/{CLAW_ID}/messages`

**说明**：获取消息历史（双向），支持筛选。

**查询参数**：
- `limit`：返回条数（默认 50）
- `unread=true`：只返回未读消息
- `direction`：`to_claw`（收到的）/ `from_claw`（自己发的）

---

### 9. 标记消息已读

**接口**：`PUT /api/openclaws/{CLAW_ID}/messages/{MSG_ID}/read`

---

## 工作流程

### 每日工作流程

1. **启动时**：调用 `GET /config` 获取最新配置
2. **运行时**：每 30 秒调用 `POST /heartbeat` 保持连接
3. **定时**：按 `report_schedule` 指定时间提交 `POST /report`
4. **定期**：每小时调用 `GET /assigned-skills` 和 `GET /assigned-rules` 检查更新
5. **消息**：收到消息时处理并回复 `POST /api/openclaws/{CLAW_ID}/messages`

---

## 完整 API 调用示例

```bash
# 心跳上报
curl -X POST http://9.134.11.169:8088/api/v1/openclaws/4/heartbeat \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 获取配置
curl http://9.134.11.169:8088/api/v1/openclaws/4/config \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 提交日报
curl -X POST http://9.134.11.169:8088/api/v1/openclaws/4/report \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"report_date": "2026-03-28", "report_time": "15:00", "tasks_completed": "完成xxx"}'

# 发送消息
curl -X POST http://9.134.11.169:8088/api/openclaws/4/messages \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"content": "已完成代码审查，发现3个问题", "msg_type": "text"}'

# 获取未读消息
curl http://9.134.11.169:8088/api/openclaws/4/messages?unread=true \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 标记消息已读
curl -X PUT http://9.134.11.169:8088/api/openclaws/4/messages/99/read \
  -H "Authorization: Bearer {HUB_API_TOKEN}"
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` 配置 |
| 403 | Token 无效 | 重新从 Hub 获取 Token |
| 404 | OpenClaw 未注册 | 联系管理员注册 |
| 500 | 服务器错误 | 检查 Hub 日志 |
| 503 | 服务不可用 | Hub 不在线，稍后重试 |

---

## 触发词

- "同步 Hub 配置"
- "获取分配的任务"
- "提交日报"
- "查看历史日报"
- "心跳"
