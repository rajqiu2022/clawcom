# 待办任务管理 (todo-manager)

## 简介

本 Skill 用于管理 OpenClaw 的待办任务系统，支持 5 级紧急度调度策略。OpenClaw 通过此 Skill 实现：

- **任务调度**：根据紧急度自动安排执行顺序
- **心跳感知**：从心跳返回中获取待办统计，决定下一步动作
- **执行上报**：完成/跳过/重试失败均需上报，Hub 记录全部执行日志
- **初始化验证**：注册后的 init 类任务，验证各项能力是否正常

---

## 前置条件

- 已通过 `hub-connect` Skill 连接到 Hub
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub API: http://9.134.11.169:8088/api/v1
认证: Authorization: Bearer {HUB_API_TOKEN}
```

---

## 5 级紧急度

| 级别 | urgency_level | 调度行为 | 典型场景 |
|------|---------------|----------|----------|
| ⚡ 中断 | `interrupt` | 到点必须中断当前任务立即执行 | 21:00 发日报 |
| 📋 弹性 | `flexible` | 有时间要求，当天完成即可 | 查看 KM 文章 |
| 🔄 后台 | `background` | 无时间要求，空闲时做 | 初始化验证 Memos |
| 🔁 跳过 | `periodic` | 错过就下一周期，但上报 skipped 记录 | 每周五周报 |
| 🔁 重试 | `retry` | 错过延后 retry_delay 分钟重试 retry_max 次，仍失败上报 | Hub 通信验证 |

### 调度决策逻辑

```
收到心跳返回后：
1. 检查 todos.interrupt 列表
   → 有未执行的 → 立即中断当前任务，执行 interrupt 任务
2. 检查 has_urgent
   → true → 拉取消息同步配置
3. 检查 todos.init_pending
   → > 0 → 空闲时处理初始化任务
4. 检查 todos.pending
   → > 0 → 按 priority 排序执行 flexible 任务
5. 所有任务完成 → 继续日常工作
```

---

## API 接口

### 1. 获取待办列表

```
GET /api/v1/openclaws/{CLAW_ID}/todos

Header:
  Authorization: Bearer {HUB_API_TOKEN}

查询参数（均可选）：
  category=init          按类别筛选：routine / init / onboard
  urgency=interrupt      按紧急度筛选：interrupt / flexible / background / periodic / retry
  enabled_only=true      是否只返回启用的（默认 true）

返回示例：
[
  {
    "id": 1,
    "title": "发送日报",
    "description": "汇总今日工作...",
    "schedule_type": "daily",
    "schedule_time": "21:00",
    "urgency_level": "interrupt",
    "retry_delay": 5,
    "retry_max": 1,
    "priority": "P0",
    "task_category": "routine",
    "verification_target": null,
    "enabled": true,
    "today_status": "pending",
    "today_completed_at": null
  }
]
```

### 2. 创建待办任务

```
POST /api/v1/openclaws/{CLAW_ID}/todos

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "title": "发送日报",                    // 必填
  "description": "汇总今日工作进展...",     // 可选
  "schedule_type": "daily",               // daily / weekly / monthly / once
  "schedule_time": "21:00",               // HH:MM 格式，interrupt/flexible 建议必填
  "schedule_day": null,                    // 周几(1-7) 或 几号(1-31)，weekly/monthly 时用
  "urgency_level": "interrupt",           // interrupt / flexible / background / periodic / retry
  "retry_delay": 5,                        // 重试延迟（分钟），仅 retry 级别
  "retry_max": 1,                          // 最大重试次数，仅 retry 级别
  "priority": "P0",                        // P0 / P1 / P2
  "task_category": "routine",             // routine / init / onboard
  "verification_target": null,             // init 任务的验证目标标识
  "enabled": true
}

返回：201 + 任务详情
```

### 3. 更新待办任务

```
PUT /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体（只传需要修改的字段）：
{
  "title": "新标题",
  "urgency_level": "flexible",
  "enabled": false
}

返回：200 + 任务详情
```

### 4. 删除待办任务

```
DELETE /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}

Header:
  Authorization: Bearer {HUB_API_TOKEN}

返回：
{ "message": "待办已删除" }
```

### 5. 上报完成

**每个任务执行后都必须上报结果，Hub 才会更新状态。**

```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/complete

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "result_summary": "日报已发送，包含 5 个工作项",     // 执行结果描述（必填）
  "status": "completed"                                // completed / retry_failed（可选，默认 completed）
}

返回：执行记录详情
{
  "id": 12,
  "todo_id": 1,
  "log_date": "2026-04-04",
  "completed_at": "2026-04-04T21:02:15",
  "result_summary": "日报已发送，包含 5 个工作项",
  "status": "completed",
  "retry_count": 0
}
```

**注意**：
- `once` 类型任务完成后会自动 `enabled=false`，不再出现在待办列表中
- `retry_failed` 状态用于重试类任务全部重试失败后的最终上报

### 6. 上报跳过

**`periodic` 级别错过执行时间时调用。**

```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/skip

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "result_summary": "当前正在执行紧急任务，跳过本次周报"   // 跳过原因
}

返回：执行记录详情（status=skipped）
```

### 7. 待办完成汇总

```
GET /api/v1/openclaws/{CLAW_ID}/todo-summary

Header:
  Authorization: Bearer {HUB_API_TOKEN}

查询参数：
  date=2026-04-04       指定日期（默认今天）

返回示例：
{
  "date": "2026-04-04",
  "total": 5,
  "completed": 3,
  "pending": 2,
  "rate": 60.0,
  "items": [
    {
      "todo": { ... },
      "status": "completed",
      "completed_at": "2026-04-04T21:02:15",
      "result_summary": "日报已发送",
      "retry_count": 0
    }
  ]
}
```

### 8. 心跳返回中的待办统计

**心跳不是本 Skill 的接口，但待办统计在心跳中返回，是调度决策的核心数据源。**

```
POST /api/v1/openclaws/{CLAW_ID}/heartbeat

返回：
{
  "status": "ok",
  "pending_messages": 3,
  "has_urgent": true,
  "todos": {
    "pending": 5,               // 今日待完成总数
    "done": 2,                  // 今日已完成
    "init_pending": 1,          // 初始化任务未完成数
    "interrupt": [               // 需要立即中断执行的任务列表
      {
        "id": 7,
        "title": "发送日报",
        "time": "21:00"
      }
    ]
  },
  "server_time": "2026-04-04T23:11:00"
}
```

---

## 各级别任务的处理流程

### interrupt（中断执行）

```
心跳返回 todos.interrupt 非空
  → 检查当前时间是否到达 schedule_time
  → 中断当前正在执行的任务（保存进度）
  → 执行 interrupt 任务
  → POST /complete 上报结果
  → 恢复之前被中断的任务
```

### flexible（当天弹性）

```
心跳返回 todos.pending > 0
  → GET /todos?urgency=flexible 获取弹性任务列表
  → 按 priority 排序
  → 在当前任务间隙或空闲时执行
  → POST /complete 上报结果
  → 当天 23:59 前未完成的，次日心跳中不再出现
```

### background（后台/空闲时）

```
心跳返回 todos.init_pending > 0
  → GET /todos?category=init 获取初始化任务
  → 空闲时按顺序执行（不中断日常工作）
  → POST /complete 上报结果
  → once 类型完成后自动 disable
```

### periodic（周期-跳过）

```
心跳返回中发现周期任务未执行
  → 判断是否已过执行时间
  → 已过且无法执行 → POST /skip 上报跳过原因
  → 未过 → 正常执行 → POST /complete 上报
  → 下一周期继续
```

### retry（周期-重试）

```
心跳返回中发现 retry 任务未执行
  → 第一次尝试执行
  → 失败 → 等待 retry_delay 分钟
  → 第二次尝试（直到 retry_max 次）
  → 全部失败 → POST /complete + status=retry_failed 上报
  → 成功 → POST /complete + status=completed 上报
```

---

## 推荐工作流

### 每日启动

```
1. POST /heartbeat 获取今日状态
2. 检查 todos.interrupt → 设置定时器
3. GET /todos?category=init → 有未完成的初始化任务则优先处理
4. GET /todos → 按 urgency_level + priority 排序今日任务队列
5. 开始执行...
```

### 执行中

```
每 30 秒:
  POST /heartbeat
  → 检查 interrupt 列表是否有新的中断任务
  → 检查 pending_messages 是否有新消息

每个任务完成后:
  POST /complete 立即上报

遇到无法完成的 periodic 任务:
  POST /skip 上报跳过

遇到 retry 任务失败:
  等待 retry_delay 分钟后重试
  超过 retry_max 次 → POST /complete + status=retry_failed
```

### 每日收尾

```
1. GET /todo-summary 检查今日完成率
2. 未完成的 flexible 任务 → 纳入日报的"未完成项"
3. 汇总当日所有 result_summary → 生成日报内容
4. POST /report 上报日报
```

---

## 任务参数速查

| 参数 | 类型 | 说明 |
|------|------|------|
| `title` | string | 任务标题（必填） |
| `description` | string | 详细描述/执行要求 |
| `schedule_type` | string | `daily` / `weekly` / `monthly` / `once` |
| `schedule_time` | string | `HH:MM` 格式的执行时间 |
| `schedule_day` | int | 周几(1-7) 或 几号(1-31) |
| `urgency_level` | string | `interrupt` / `flexible` / `background` / `periodic` / `retry` |
| `retry_delay` | int | 重试延迟分钟数（仅 retry） |
| `retry_max` | int | 最大重试次数（仅 retry） |
| `priority` | string | `P0` / `P1` / `P2` |
| `task_category` | string | `routine` / `init` / `onboard` |
| `verification_target` | string | 验证目标标识（init 任务用） |
| `enabled` | bool | 是否启用 |

## 执行记录状态

| status | 说明 |
|--------|------|
| `completed` | 正常完成 |
| `skipped` | 跳过（periodic 级别错过时） |
| `retry_failed` | 重试全部失败（retry 级别） |
| `overdue` | 逾期未完成 |

---

## 触发词

- "查看待办"、"今日待办"、"待办列表"
- "创建待办"、"新增任务"、"添加待办"
- "完成待办"、"上报完成"、"标记完成"
- "跳过待办"、"跳过任务"
- "待办汇总"、"完成率"、"今日完成情况"
- "初始化任务"、"验证任务"
- "紧急任务"、"中断任务"
