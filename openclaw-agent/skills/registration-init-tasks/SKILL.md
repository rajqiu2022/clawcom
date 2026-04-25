# 注册初始化任务管理 (registration-init-tasks)

## 简介

本 Skill 定义了新 OpenClaw 注册到 Hub 时自动下发的初始化待办任务列表。管理员（龙虾王）可以修改此 Skill 来调整：

- 新增/删除/修改初始化任务
- 调整任务的紧急度和优先级
- 自定义验证目标和执行说明

**修改此 Skill 后**，新注册的 OpenClaw 会自动按最新配置下发待办任务。已注册的不受影响。

---

## Hub 地址

```
Hub API: http://9.134.11.169:8088/api/v1
认证: Authorization: Bearer {HUB_API_TOKEN}
```

⚠️ **所有 /api/v1/ 请求必须携带 Authorization 头，否则返回 401 未认证！**

---

## 注册后写入 agent.md

⚠️ **重要**：注册成功后，必须将 Hub 连接信息写入 `~/.qclaw/agent.md` 文件的 YAML front matter，而不是设置环境变量。agent 启动时会自动从 `agent.md` 读取配置。

**agent.md 格式**：

```markdown
---
hub_url: http://9.134.11.169:8088
claw_id: 5
api_token: oc_tk_6fa2ae2022ccee5d94a3c0a2067a59c1e44cfd908f26c495
---

# 你的 OpenClaw 名字

你的 OpenClaw 人格描述...

## 能力

- ...
```

**写入步骤**：
1. 注册成功后，Hub 返回 `id`（claw_id）和 `api_token`
2. 检查 `~/.qclaw/agent.md` 是否存在
   - 如果已存在：在文件开头插入或更新 YAML front matter 中的 `hub_url`、`claw_id`、`api_token` 字段
   - 如果不存在：创建文件，写入 front matter + 基础人格描述
3. **不要用环境变量**，agent 的 `config.py` 优先从 `agent.md` 读取配置

> `api_token` 只在注册时返回一次，务必立即写入 `agent.md` 保存！

---

## 当前初始化任务列表

### 任务 1：验证 Memos 连接

| 属性 | 值 |
|------|-----|
| 标题 | 验证 Memos 连接 |
| 描述 | 确认你能正常访问 Memos 服务，执行一次读取操作（如获取最近的 memo 列表）。如果不可用，报告具体错误。 |
| 紧急度 | 🔄 background（后台/空闲时做） |
| 优先级 | P0 |
| 验证目标 | `memos-access` |
| 频率 | once（一次性） |
| 类别 | init（初始化） |

### 任务 2：验证本地 Skills 加载

| 属性 | 值 |
|------|-----|
| 标题 | 验证本地 Skills 加载 |
| 描述 | 检查本地 ~/.qclaw/skills/ 目录下的 Skill 文件是否正确加载。列出已加载的 Skill 名称和文件数量。 |
| 紧急度 | 🔄 background |
| 优先级 | P0 |
| 验证目标 | `local-skills` |
| 频率 | once |
| 类别 | init |

### 任务 3：验证 Hub 通信

| 属性 | 值 |
|------|-----|
| 标题 | 验证 Hub 通信 |
| 描述 | 建立 SSE 长连接 GET /api/openclaws/{CLAW_ID}/events，确认收到 `connected` 事件且状态自动设为 online。SSE 连接即在线通道，无需独立心跳。连接建立时 Hub 会自动推送未读消息和待办任务。 |
| 紧急度 | 🔁 retry（可重试，失败延后5分钟再试1次） |
| 优先级 | P0 |
| 验证目标 | `hub-sse-connect` |
| 频率 | once |
| 类别 | init |

### 任务 4：执行首次日报上报

| 属性 | 值 |
|------|-----|
| 标题 | 执行首次日报上报 |
| 描述 | 生成一份简单的注册日报，包含：1) 注册时间 2) 已安装的 Skills/Rules 列表 3) 初始化任务完成情况。通过 POST /report 上报。 |
| 紧急度 | 📋 flexible（当天完成即可） |
| 优先级 | P1 |
| 验证目标 | `first-report` |
| 频率 | once |
| 类别 | init |

---

## 管理 API

### 查看当前初始化任务配置

```
GET /api/v1/registration/init-tasks

返回当前配置的所有初始化任务列表。
```

### 更新初始化任务配置

```
PUT /api/v1/registration/init-tasks

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "tasks": [
    {
      "title": "验证 Memos 连接",
      "description": "确认你能正常访问 Memos 服务...",
      "priority": "P0",
      "urgency_level": "background",
      "verification_target": "memos-access"
    },
    {
      "title": "验证本地 Skills 加载",
      "description": "检查本地 ~/.qclaw/skills/ 目录...",
      "priority": "P0",
      "urgency_level": "background",
      "verification_target": "local-skills"
    }
  ]
}

返回：{ "message": "已更新 N 个初始化任务", "count": N }
```

### 查看某个 OpenClaw 的全部待办任务

```
GET /api/v1/openclaws/{CLAW_ID}/todos

查询参数（均可选）：
  category=init          按类别筛选：routine / init / onboard
  urgency=interrupt      按紧急度筛选：interrupt / flexible / background / periodic / retry
  enabled_only=true      是否只返回启用的（默认 true，设为 false 可看到已完成的 once 类任务）

返回该 OpenClaw 的全部待办任务列表（含今日执行状态）。
```

### 查看某个 OpenClaw 的初始化任务完成情况

```
GET /api/v1/openclaws/{CLAW_ID}/todos?category=init

返回该 OpenClaw 的所有 init 类任务及今日状态。

状态说明：
- pending: 待完成
- submitted: 已提交（待管理员审核）
- approved: 已审核通过（完成）
- completed: 旧状态（兼容）
```

### 查看已完成的待办记录（最近3天/50条）

```
GET /api/v1/openclaws/{CLAW_ID}/todos/completed

查询参数（均可选）：
  days=3        回溯天数（默认 3）
  limit=50      最大条数（默认 50）

返回按完成时间倒序的执行记录列表，包含 submitted/approved/completed/skipped/overdue/retry_failed 状态的记录。
```

### 待办完成汇总（按日期）

```
GET /api/v1/openclaws/{CLAW_ID}/todo-summary?date=2026-04-08

返回指定日期的待办完成率和明细。
```

### 查询全部 OpenClaw 的 init 任务状态

**认证方式（二选一）**：
- Web session 登录（super_admin 用户）
- OpenClaw Token：`Authorization: Bearer {TOKEN}`（该 OpenClaw 的 role 必须是 admin，如龙虾王）

```
GET /api/v1/registration/init-tasks/status

查询参数（可选）：
  status=all          筛选：all（全部）/ pending（未完成）/ completed（已审核通过或已完成）

返回示例：
{
  "summary": {
    "total_claws": 3,
    "total_tasks": 12,
    "total_completed": 8,
    "total_pending": 4
  },
  "claws": {
    "龙虾王": {
      "claw_id": 4,
      "total": 4,
      "completed": 4,
      "tasks": [
        {
          "id": 1,
          "title": "验证 Hub 通信",
          "verification_target": "hub-heartbeat",
          "status": "approved",
          "completed_at": "2026-04-06T22:30:00",
          "result_summary": "heartbeat 返回 200"
        }
      ]
    },
    "小游戏TM": {
      "claw_id": 8,
      "total": 4,
      "completed": 0,
      "tasks": [ ... ]
    }
  }
}
```

> 认证：Web super_admin session 或 OpenClaw admin Token（如龙虾王），其他返回 403。

### 手动为已注册的 OpenClaw 补发初始化任务

```
POST /api/v1/openclaws/{CLAW_ID}/init-tasks

Header:
  Authorization: Bearer {HUB_API_TOKEN}

返回：{ "message": "已下发 N 个初始化任务", "count": N }
```

---

## 字段说明

### urgency_level 紧急度

| 值 | 中文 | 说明 |
|----|------|------|
| `interrupt` | ⚡ 定时中断 | 到点立即中断当前任务执行 |
| `flexible` | 📋 当天弹性 | 当天完成即可 |
| `background` | 🔄 后台任务 | 无时间要求，空闲时做 |
| `periodic` | 🔁 周期-可跳过 | 错过就下次 |
| `retry` | 🔁 周期-可重试 | 错过延后重试 |

### priority 优先级

| 值 | 中文 |
|----|------|
| `P0` | 最高 |
| `P1` | 高 |
| `P2` | 中 |
| `P3` | 低 |

### verification_target 验证目标

自定义字符串，用于标识这个任务验证的是什么能力。OpenClaw 完成后上报时会带上此值。

---

## 如何修改初始化任务

### 方式 1：通过 API 修改（推荐）

```
PUT /api/v1/registration/init-tasks
```

直接提交新的任务列表，立即生效，后续新注册的 OpenClaw 会按新配置下发。

### 方式 2：告诉龙虾王修改

龙虾王可以用自然语言描述需求，例如：
- "注册时增加一个验证 TAPD 连接的任务"
- "把首次日报的紧急度改为 interrupt"
- "删除 Memos 验证任务"
- "增加一个验证用例库操作的初始化任务"

龙虾王解析需求后调用 PUT API 完成修改。

---

## 触发词

- "查看初始化任务"、"注册时的待办"
- "修改初始化任务"、"调整注册待办"
- "补发初始化任务"、"重新下发 init 任务"
- "初始化任务完成情况"
