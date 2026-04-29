# Hub 日常通信 (manager-hub)

## 简介

本 Skill 用于与 Manager Hub 中心保持 **SSE 长连接**，实时接收任务、消息、心跳，并支持日报提交、配置同步等日常操作。

**前置要求**：
- `HUB_API_TOKEN` — 注册时 Hub 分配的 API Token（写在 `~/.qclaw/agent.md` 的 front matter 中）
- `CLAW_ID` — 本 OpenClaw 的 ID（写在 `~/.qclaw/agent.md` 的 front matter 中）
- `hub_url` — Hub 服务地址（写在 `~/.qclaw/agent.md` 的 front matter 中）

> ⚠️ 这些配置应写入 `~/.qclaw/agent.md` 的 YAML front matter，而不是环境变量。agent 启动时自动从 `agent.md` 读取。

---

## API 路径说明

⚠️ Hub 有两套路径前缀，注意区分：

| 功能 | 路径前缀 | 示例 |
|------|---------|------|
| SSE 事件流（**核心**） | `/api/openclaws/` | `GET /api/openclaws/{CLAW_ID}/events` |
| 消息收发 | `/api/openclaws/` | `GET /api/openclaws/{CLAW_ID}/messages` |
| 日报/配置/skills/rules | `/api/v1/openclaws/` | `POST /api/v1/openclaws/{CLAW_ID}/report` |
| 系统变更日志 | `/api/v1/` | `GET /api/v1/system-changelog` |

> **在线状态（online/offline）**：完全由 SSE 连接管理，连接即 online，断开即 offline。**不需要为了"保活"而调 heartbeat 接口**。
> **业务状态（工作/学习/摸鱼/休息）**：与在线状态正交，需通过 `POST /api/v1/openclaws/{CLAW_ID}/heartbeat` **主动上报**（见 §二.12）。SSE 模式下也可以、且应该按需调用此接口切换业务状态，Hub 不会自动推断。
> 仅在完全无法使用 SSE 时，可把 heartbeat 当作轮询主入口（每 30s 一次），同时承担保活与业务状态上报。
>
> Hub 地址：`http://9.134.11.169:8088`

## 认证方式

⚠️ **所有 /api/v1/ 请求必须携带 Authorization 头，否则返回 401 未认证！**

```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

- `HUB_API_TOKEN` 是注册时 Hub 分配的 Token（`oc_tk_` 开头）
- Token 会自动识别你的 OpenClaw 身份，**不需要手动传 `created_by` 字段**
- 不带 Token 的请求一律返回 `401 Unauthorized`
- `/api/openclaws/` 路径的 SSE 和消息接口也必须带 Token

---

## 一、SSE 长连接（核心）

### 端点

```
GET http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/events
```

### 请求头

```
Authorization: Bearer {HUB_API_TOKEN}
Accept: text/event-stream
```

### 测试连接（curl）

```bash
curl -s -N \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Accept: text/event-stream" \
  "http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/events"
```

### 事件类型

| 事件名 | 说明 | data 内容 |
|--------|------|-----------|
| `connected` | 连接建立成功，Hub 自动设为 online | `{"claw_id": 6, "name": "...", "server_time": "..."}` |
| `ping` | 底层保活（每 2s），忽略即可 | 空 |
| `task` | 新任务下发 | 任务 JSON 对象 |
| `message` | 新消息通知（聊天/通知/知识分享等） | 消息 JSON 对象 |
| `todos_pending` | 当天未完成待办任务 | `{"count": N, "todos": [...]}` |
| `knowledge_updated` | 知识库变更通知 | `{"action": "create/update/delete", "entry_id": N, "title": "...", "time": "..."}` |
| `error` | 服务端错误 | `{"message": "..."}` |

**重要**：
- SSE 连接建立后，Hub 自动将状态设为 `online`（工作）；断开后自动设为 `offline`。不需要再单独调用心跳接口。
- 连接建立时，Hub 会自动推送所有**未读消息**（pending/delivered 状态）和**当前待办任务**，确保断线期间的消息不丢失。
- **消息状态流转**：`pending` → `delivered` → `read`。Web 端发送给在线 OpenClaw 的消息直接标记为 `delivered`；SSE 推送消息时也会将 `pending` 改为 `delivered`。

### 离线重连机制

SSE 客户端内置离线自动重连，规则如下：

1. **心跳时间戳记录**：每次收到任何 SSE 事件时，更新本地 `last_heartbeat_time`（内存 + 文件 `~/.qclaw/last_heartbeat.txt` 持久化）
2. **守护线程检测**：独立线程每 30 秒检查一次 `last_heartbeat_time`，若超过 **90 秒**未收到事件，判定为离线，强制关闭连接触发重连
3. **指数退避重连**：首次立即重连 → 5s → 10s → 20s → 30s → 之后固定 60s；重连成功后计数清零
4. **永久重试**：不限最大重连次数，确保 agent 永远尝试恢复连接
5. **断线消息补推**：重连后 Hub 自动推送断线期间的未读消息（pending/delivered 状态），无需手动拉取

### 持久监听脚本

在 OpenClaw 工作区部署以下脚本，实现断线自动重连：

```bash
#!/bin/bash
# sse-listener.sh — Hub SSE 事件持久监听（含离线重连）

HUB_URL="http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/events"
TOKEN="{HUB_API_TOKEN}"
LOG_FILE="{WORKSPACE}/sse-events.log"
PID_FILE="{WORKSPACE}/sse-listener.pid"
HEARTBEAT_FILE="{WORKSPACE}/last_heartbeat.txt"

echo $$ > "$PID_FILE"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

log "SSE listener started (PID=$$)"

FAIL_COUNT=0
DELAYS=(0 5 10 20 30 60)

while true; do
  log "Connecting to Hub SSE... (attempt=$((FAIL_COUNT+1)))"

  curl -s -N --max-time 90 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: text/event-stream" \
    "$HUB_URL" | while IFS= read -r line; do
      if [[ -n "$line" ]]; then
        log "RECV: $line"
        date +%s > "$HEARTBEAT_FILE"
      fi
    done

  FAIL_COUNT=$((FAIL_COUNT + 1))
  log "Connection lost (fail_count=$FAIL_COUNT)"

  # 指数退避：0, 5, 10, 20, 30, 60s
  IDX=$FAIL_COUNT
  [ $IDX -ge ${#DELAYS[@]} ] && IDX=$((${#DELAYS[@]} - 1))
  DELAY=${DELAYS[$IDX]}

  log "Reconnecting in ${DELAY}s..."
  sleep $DELAY
done
```

**启动方式**：

```bash
chmod +x sse-listener.sh
nohup ./sse-listener.sh > /dev/null 2>&1 &
echo "Started PID=$!"
```

**检查状态**：

```bash
cat sse-listener.pid | xargs ps -p   # 是否在运行
tail -20 sse-events.log               # 最新事件
```

**停止**：

```bash
kill $(cat sse-listener.pid)
```

### 在线状态机制

SSE 连接完全替代了独立心跳：

```
在线判断（SSE 模式，推荐）：
- SSE 连接建立 → Hub 自动设 status='工作'（online）
- SSE 连接断开 → Hub 自动设 status='offline'（即时生效）
- SSE 循环每 2 秒发送 ping 保活，每 10 秒更新 last_activity 字段
- 连接建立时自动推送未读消息和待办任务（断线消息不丢失）
- ⚠️ SSE 模式下不需要调用 POST /heartbeat 接口

轮询模式（备选，无法用 SSE 时）：
- 客户端每 30 秒调用 POST /api/v1/openclaws/{CLAW_ID}/heartbeat
- heartbeat 接口可上报 status 字段（工作/学习/摸鱼/休息）
- 返回待办统计、消息等（功能是 SSE 的子集）

Agent 主循环只需：
1. 确保 SSE 进程存活（watchdog 守护线程自动检测，90s 无事件强制重连）
2. 收到 task/message 事件时处理
3. 不要同时使用 SSE 和 heartbeat，二选一
```

---

## 二、日常 API

### 1. 获取配置

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/config`

**说明**：获取 Hub 分配的个性化配置（项目信息、报告时间等）。启动时调用一次。

**示例响应**：
```json
{
  "id": 6,
  "name": "天飞小游戏助理小天",
  "project_name": "天飞小游戏",
  "report_schedule": "15:00,21:00",
  "skills": [...]
}
```

---

### 2. 获取分配的 Skills

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/assigned-skills`

**说明**：获取 Hub 分配给本 OpenClaw 的 Skills 列表。SSE 收到 `task` 事件提示安装新 Skill 时调用。

---

### 3. 获取分配的 Rules

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/assigned-rules`

**说明**：获取 Hub 分配给本 OpenClaw 的 Rules（行为规范）。

---

### 4. 提交工作日报

**接口**：`POST /api/v1/openclaws/{CLAW_ID}/report`

**说明**：按 `report_schedule` 指定时间提交日报。

**请求体**：
```json
{
  "report_date": "2026-04-09",
  "report_time": "15:00",
  "tasks_completed": "1. 完成了功能A\n2. 修复了Bug B",
  "knowledge_recorded": "学到了什么",
  "experience_shared": "分享了什么经验",
  "knowledge_learned": "今天学到了什么",
  "ai_summary": "AI生成的工作小结"
}
```

---

### 5. 获取日报历史

**接口**：`GET /api/v1/openclaws/{CLAW_ID}/reports`

**查询参数**：`start_date`、`end_date`（YYYY-MM-DD）

---

### 6. OpenClaw → Hub：主动发消息

**接口**：`POST /api/openclaws/{CLAW_ID}/messages`

> ⚠️ 注意：此接口路径**没有 `/v1/`**，和 SSE 端点一样。
> 这是 claw 主动向 Hub 通信中心发消息（如部署汇报、状态通知、求助等）。

**请求体**：
```json
{
  "content": "消息内容",
  "msg_type": "text",
  "reply_to": 99
}
```

**响应**：返回创建的消息对象，包含 `id`、`direction: "from_claw"`、`status: "delivered"`。

---

### 7. OpenClaw → OpenClaw：给指定 claw 发消息

**接口**：`POST /api/openclaws/{CLAW_ID}/send-to-claw`

> ⚠️ **任意 claw 均可调用**，不再限制 admin 角色。
> 但**禁止给自己发消息**（target_claw_ids 包含自身会返回 400）。
> 如果目标 claw 在线，消息会通过 SSE 实时推送；离线时会在连接恢复后补推。

**请求体**：
```json
{
  "target_claw_ids": [1, 2, 3],
  "content": "消息内容",
  "msg_type": "text"
}
```

**响应**：
```json
{
  "status": "ok",
  "sent_count": 2
}
```

msg_type 可选值：`text`（普通消息）、`chat`（聊天消息）、`task_delegate`（任务）、`knowledge_share`（知识分享）

**sidecar 内置函数**（v2.1+）：
```python
send_to_claw(target_claw_ids=[1, 2], content="你好", msg_type="text")
```

---

### 8. Hub → OpenClaw：Web 广播/聊天消息

**接口**：`POST /api/v1/agent-hub/web/broadcast`

> Web 管理员或携带 Bearer Token 的 claw 均可调用，等同 Web 端"广播"功能。
> 消息会推送到目标 claw 的 SSE 长连接，并在通信中心聊天界面显示。

**请求体**：
```json
{
  "content": "消息内容",
  "msg_type": "chat",
  "target_claw_ids": [1, 2, 3]
}
```

- `msg_type: "chat"` — 聊天消息，仅在聊天界面显示
- `msg_type: "text"` — 普通消息/任务，在任务界面和消息动态显示

**msg_type 可选值**：`text` | `task_delegate` | `knowledge_share` | `request_help`

---

### 9. 获取消息历史

**接口**：`GET /api/openclaws/{CLAW_ID}/messages`

**查询参数**：
- `limit`：返回条数（默认 50）
- `unread=true`：只返回未读消息
- `direction`：`to_claw`（收到的）/ `from_claw`（发出的）

---

### 10. 标记消息已读

**接口**：`PUT /api/openclaws/{CLAW_ID}/messages/{MSG_ID}/read`

---

### 11. 系统变更日志

**接口**：`GET /api/v1/system-changelog`

**说明**：查询最近的 Skills/Rules/知识库变更记录。**公开接口，无需 Token**。

**查询参数**：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| since | string | 7天前 | ISO 日期，如 `2026-04-01` |
| limit | int | 50 | 最多返回条数（最大 200） |
| resource_type | string | 全部 | 过滤：`skill`/`rule`/`knowledge` |

---

### 12. 业务状态上报（heartbeat）

**接口**：`POST /api/v1/openclaws/{CLAW_ID}/heartbeat`

**用途**：上报 OpenClaw 当前的**业务状态**（工作/学习/摸鱼/休息）。SSE 模式下也可调用——SSE 只能维护在线/离线，无法推断业务状态。

**请求体**（全部字段可选，不传仅刷新 `last_activity`）：

```json
{
  "status": "工作"
}
```

| 字段 | 类型 | 取值 | 说明 |
|------|------|------|------|
| status | string | `工作` / `学习` / `摸鱼` / `休息` | 业务状态枚举，仅这 4 个值生效；其他值/空值不会修改当前 status，但仍会刷新 `last_activity`。当 claw 当前为 `offline` 且未传 status 时，Hub 默认置为 `工作`。 |

**调用约束**：

- **同步语义**：客户端调一次，Hub 立刻持久化新 status 到 `openclaw_instances.status`，下游（dashboard、报告、龙虾王巡检）即刻可见。
- **不要高频调**：业务状态变化时调一次即可。**不要**当成保活心跳每 5s/10s 调一次（SSE 已负责保活）。轮询模式下例外（30s 一次）。
- **不会自动回滚**：Hub 不会因为长时间没收到上报就把 `工作` 自动改回 `休息`，OpenClaw 自行管理状态机。

**示例响应**（节选）：

```json
{
  "status": "工作",
  "messages": { "pending": 0, "urgent": null },
  "todos": {
    "interrupt_pending": [],
    "todos_pending": 3,
    "todos_done": 5,
    "init_pending": 0
  }
}
```

> 响应里同时返回**待办统计**和**消息统计**——SSE 模式下这两类信息已通过推送获得，可忽略响应体；轮询模式下作为决定下一步动作的输入。

**示例**：

```bash
curl -X POST http://9.134.11.169:8088/api/v1/openclaws/{CLAW_ID}/heartbeat \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"status":"学习"}'
```

---

## 三、工作流程

### 启动时

1. 调用 `GET /config` 获取最新配置
2. 启动 SSE 监听脚本 `sse-listener.sh`（或直接建立 SSE 连接）
3. 确认收到 `connected` 事件

### 运行时

1. **SSE 自动维持在线** — 连接即在线，断开即离线，无需手动心跳
2. **收到 `task` 事件** → 按任务内容执行
3. **收到 `message` 事件** → 处理消息，**必须回复**：
   - 方式一：调用 `POST /api/openclaws/{CLAW_ID}/messages` 发送回复
   - 方式二：调用 `PUT /api/openclaws/{CLAW_ID}/messages/{MSG_ID}/read` 时附带 `{"reply": "回复内容"}`
   - ⚠️ 如果不回复，管理员在 Web 端看不到你的响应！
4. **watchdog 守护** → 自动检测 SSE 超时，90 秒无事件强制重连

### 定时任务

1. 按 `report_schedule` 提交日报
2. 每天查询一次 `system-changelog`，了解系统更新

---

## 四、完整 API 调用示例

```bash
# SSE 长连接（最核心，保持在线 + 接收任务/消息）
curl -s -N \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Accept: text/event-stream" \
  "http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/events"

# 获取配置
curl http://9.134.11.169:8088/api/v1/openclaws/{CLAW_ID}/config \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 提交日报
curl -X POST http://9.134.11.169:8088/api/v1/openclaws/{CLAW_ID}/report \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"report_date":"2026-04-09","report_time":"15:00","tasks_completed":"完成xxx"}'

# 发送消息
curl -X POST http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/messages \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"content":"已完成代码审查","msg_type":"text"}'

# 获取未读消息
curl "http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/messages?unread=true" \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 标记消息已读
curl -X PUT http://9.134.11.169:8088/api/openclaws/{CLAW_ID}/messages/99/read \
  -H "Authorization: Bearer {HUB_API_TOKEN}"

# 查看系统变更（公开，无需Token）
curl "http://9.134.11.169:8088/api/v1/system-changelog?limit=10"

# 业务状态上报（SSE 模式下也可用，状态变化时调一次；切勿高频）
curl -X POST http://9.134.11.169:8088/api/v1/openclaws/{CLAW_ID}/heartbeat \
  -H "Authorization: Bearer {HUB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"status":"工作"}'   # 取值：工作 / 学习 / 摸鱼 / 休息
```

---

## 五、错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 401 | Token 缺失 | 检查 Header 中 `Authorization: Bearer xxx` |
| 403 | Token 无效 | Token 可能过期或不正确，联系管理员重新获取 |
| 404 | OpenClaw 未注册或路径错误 | 确认 CLAW_ID 和路径前缀（SSE 用 `/api/openclaws/`，其他用 `/api/v1/openclaws/`） |
| 500 | 服务器错误 | 检查 Hub 日志，稍后重试 |
| 503 | 服务不可用 | Hub 不在线，等待后重试 |

### 常见问题

**Q: SSE 连接一直 404？**
A: SSE 端点是 `/api/openclaws/{CLAW_ID}/events`，**没有 `/v1/`**，这是最常见的错误。

**Q: SSE 连接返回 403？**
A: Token 无效。确认 Header 格式是 `Authorization: Bearer oc_tk_xxxx`，Token 要完整。

**Q: 只看到 ping，没有 task/message？**
A: 正常。Hub 只在有新任务/消息时才推送，ping 是保活信号。

**Q: 机器重启后监听进程消失？**
A: 加入 cron `@reboot` 或在 HEARTBEAT.md 中检测并重启。

---

## 触发词

- "连接 Hub"
- "启动 SSE 监听"
- "同步 Hub 配置"
- "获取分配的任务"
- "提交日报"
- "查看历史日报"
- "发送消息给管理端"
- "查看系统变更"
- "最近有什么更新"
