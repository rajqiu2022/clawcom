# 待办任务管理 (todo-manager)

## 简介

本 Skill 用于管理 OpenClaw 的待办任务系统，支持 5 级紧急度调度策略。OpenClaw 通过此 Skill 实现：

- **任务调度**：根据紧急度自动安排执行顺序
- **心跳感知**：从心跳返回中获取待办统计，决定下一步动作
- **执行上报**：完成/跳过/重试失败均需上报，Hub 记录全部执行日志
- **初始化验证**：注册后的 init 类任务，验证各项能力是否正常

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar`（SSE 通信链路在线）
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub API: http://clawteam.woa.com:18800/api/v1
认证: Authorization: Bearer {HUB_API_TOKEN}
```

---

## 🦞 龙虾王（admin claw）必读 · 审核唯一入口

> 仅当本 OpenClaw 的 `role == admin`（即"龙虾王"）时才适用本节。
> 普通龙虾（test_manager/specialist/...）请直接跳到下面"5 级紧急度"。

**全局待审核队列只走这一个接口，别走别的：**

```
GET /api/v1/todos/submitted          # admin claw 默认全量（days=0, limit=500）
Header: Authorization: Bearer {HUB_API_TOKEN}
```

约束（务必照做，否则会"看不到任务"或"漏审"）：

- ✅ admin claw token 本身就有**全局**可见性，**不要**再加任何项目/claw_id 过滤参数；后端会按 token 的 role 自动放行。
- ✅ **默认窗口对 admin claw = 全量历史（days=0, limit=500）**。后端会自动识别 token 角色：
  - `super_admin` / `admin claw`（龙虾王）：未传 `days` 时默认 `days=0`，返回所有未审核积压；
  - 项目管理员：默认 `days=3`。
  - 想限制时间窗显式传 `days=N`；想看更多条显式传 `limit=2000`。
- ⚠️ **统计口径对照**：
  - `GET /api/v1/dashboard/stats.submitted_todos` 用"每个 todo 的最近一条 log"算法（≈ 全部历史积压），但前端只展示 30 条；
  - `GET /api/v1/todos/submitted` 用"按 log 维度筛 status=submitted"，admin 默认全量，**这才是真实积压**。
  - 两边对不上时**以 `/todos/submitted` 全量结果为准**——不要把"我 API 只返回 N 条"误判成"统计虚假"，那很可能是被 days 窗口截断了。
- ✅ 拿到队列后，**逐条**用返回里的 `openclaw_id` + `id(todo_id)` 调审核接口：
  `POST /api/v1/openclaws/{openclaw_id}/todos/{todo_id}/approve`
  审核历史某天**必须**带 `{"log_date":"YYYY-MM-DD"}`（用返回里的 `log_date` 字段）。
- ❌ **不要**用 `GET /api/v1/openclaws/{自己CLAW_ID}/todos` 看自己——龙虾王自己几乎没有 todo，会得到"0 条"的错觉。
- ❌ **不要**用 `GET /api/v1/dashboard/stats → submitted_todos` 当审核主入口——那只取前 30 条预览，会**漏审历史积压**。它只能用于"瞄一眼今天有没有新提交"。
- ❌ 任何只显示"今日"或"本项目"的列表都不是审核队列。

最简执行模板（伪代码）：

```
queue = GET /api/v1/todos/submitted            # admin 默认全量历史
print(f"待审核共 {len(queue)} 条，跨日期 {min(log_date)} ~ {max(log_date)}")
for item in queue:
    review(item.result_summary)
    POST /api/v1/openclaws/{item.openclaw_id}/todos/{item.id}/approve
        body = {"log_date": item.log_date}     # 必须带，否则跨天找不到记录
# 审核完后再次 GET 应该收敛到 0 / 仅剩刚刚新提交的
```

**自检项（每轮审核前必做）：**

1. `GET /api/v1/todos/submitted` 返回 N 条 → 这就是真实积压总数（admin claw 默认全量）。
2. 如果 `dashboard/stats.submitted_todos` 显示 M 条而 N < M：**不要**写"统计 bug / 虚假统计"结论，先怀疑自己有没有传错 `days` 把窗口缩小了。
3. 按 `log_date` 分组打印一次分布，肉眼确认有没有"前几天的历史积压"被遗漏。

详细参数与字段释义见下文 5.2 节。

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

### 5. 上报完成（提交待办）

**每个任务执行后都必须上报结果，Hub 才会更新状态。提交后状态为 `submitted`（已提交），需管理员审核通过后才算完成（`approved`）。**

```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/complete

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "result_summary": "日报已发送，包含 5 个工作项",     // 执行结果描述（必填）
  "status": "submitted"                                // submitted / skipped / retry_failed（可选，默认 submitted）
}

返回：执行记录详情
{
  "id": 12,
  "todo_id": 1,
  "log_date": "2026-04-04",
  "completed_at": "2026-04-04T21:02:15",
  "result_summary": "日报已发送，包含 5 个工作项",
  "status": "submitted",
  "retry_count": 0
}
```

**注意**：
- 默认状态从 `completed` 改为 `submitted`，表示已提交待审核
- `once` 类型任务提交后会自动 `enabled=false`，不再出现在待办列表中
- `retry_failed` 状态用于重试类任务全部重试失败后的最终上报
- **三阶段流程**：`pending`（待完成）→ `submitted`（已提交，待审核）→ `approved`（已审核，完成）

### 5.1 审核通过待办

**管理员（龙虾王）审核已提交的待办，将 `submitted` 改为 `approved`。**

```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/approve

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体（可选）：
{
  "log_date": "2026-04-13"       // 指定审核哪天的记录，默认今天
}

返回：执行记录详情
{
  "id": 12,
  "todo_id": 1,
  "log_date": "2026-04-13",
  "completed_at": "2026-04-13T21:02:15",
  "result_summary": "日报已发送，包含 5 个工作项",
  "status": "approved",
  "retry_count": 0
}
```

**注意**：
- 只有 `submitted` 状态的记录可以审核，其他状态返回 400 错误
- 审核通过后状态变为 `approved`，此时才算真正完成

### 5.2 管理员待审核队列（龙虾王必用）

> 这是龙虾王审核待办的主入口。  
> 不要只看自己的 `CLAW_ID` 待办，必须先拉全局 `submitted` 队列，再逐条审核。

```
GET /api/v1/todos/submitted?days=3&limit=200

Header:
  Authorization: Bearer {HUB_API_TOKEN}

返回示例：
[
  {
    "id": 11,                          // todo_id
    "title": "发送日报",
    "description": "...",
    "openclaw_name": "小天",            // 提交的龙虾名
    "openclaw_id": 4,                  // 审核时必须用这个 claw_id
    "today_status": "submitted",
    "today_completed_at": "2026-04-21T21:02:15",
    "result_summary": "日报已发送",
    "log_date": "2026-04-21"
  }
]
```

**龙虾王审核标准动作：**
1. `GET /api/v1/todos/submitted` 拉取待审核队列
2. 对每条记录，使用返回的 `openclaw_id` + `id(todo_id)` 调用审核接口：
   - `POST /api/v1/openclaws/{openclaw_id}/todos/{todo_id}/approve`
3. 若需要审核历史某天，携带 `{"log_date":"YYYY-MM-DD"}`。
4. 审核后可再次查询队列，确认该条已从 `submitted` 消失。

**常见误区（必须避免）：**
- ❌ 用龙虾王自己的 `CLAW_ID` 去审核别人提交的 todo
- ❌ 只看“今日待办”而不看“待审核队列”
- ❌ 审核时不传 `log_date` 导致跨天记录找不到

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
  "pending": 2,
  "submitted": 1,
  "approved": 2,
  "rate": 40.0,
  "items": [
    {
      "todo": { ... },
      "status": "submitted",
      "completed_at": "2026-04-04T21:02:15",
      "result_summary": "日报已发送",
      "retry_count": 0
    }
  ]
}
```

### 8. 已完成待办列表

**查询最近的执行记录（默认最近 3 天 / 最多 50 条），包含已完成、已跳过、重试失败的记录。**

```
GET /api/v1/openclaws/{CLAW_ID}/todos/completed

Header:
  Authorization: Bearer {HUB_API_TOKEN}

查询参数（均可选）：
  days=3        回溯天数（默认 3）
  limit=50      最大条数（默认 50）

返回示例：
{
  "since": "2026-04-03",
  "count": 12,
  "items": [
    {
      "log": {
        "id": 45,
        "todo_id": 7,
        "log_date": "2026-04-05",
        "completed_at": "2026-04-05T21:02:15",
        "result_summary": "日报已发送，包含 5 个工作项",
        "status": "completed",
        "retry_count": 0
      },
      "todo": {
        "id": 7,
        "title": "发送日报",
        "urgency_level": "interrupt",
        "task_category": "routine",
        ...
      }
    }
  ]
}
```

**用途**：
- 日报生成时引用近期完成的待办作为工作内容
- 检查 init 任务是否全部完成
- 审计 OpenClaw 的任务执行历史

### 9. 心跳返回中的待办统计

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
  → POST /complete 上报结果（status=submitted，提交待审核）
  → 恢复之前被中断的任务
```

### flexible（当天弹性）

```
心跳返回 todos.pending > 0
  → GET /todos?urgency=flexible 获取弹性任务列表
  → 按 priority 排序
  → 在当前任务间隙或空闲时执行
  → POST /complete 上报结果（status=submitted，提交待审核）
  → 当天 23:59 前未完成的，次日心跳中不再出现
```

### background（后台/空闲时）

```
心跳返回 todos.init_pending > 0
  → GET /todos?category=init 获取初始化任务
  → 空闲时按顺序执行（不中断日常工作）
  → POST /complete 上报结果（status=submitted，提交待审核）
  → once 类型完成后自动 disable
```

### periodic（周期-跳过）

```
心跳返回中发现周期任务未执行
  → 判断是否已过执行时间
  → 已过且无法执行 → POST /skip 上报跳过原因
  → 未过 → 正常执行 → POST /complete 上报（status=submitted）
  → 下一周期继续
```

### retry（周期-重试）

```
心跳返回中发现 retry 任务未执行
  → 第一次尝试执行
  → 失败 → 等待 retry_delay 分钟
  → 第二次尝试（直到 retry_max 次）
  → 全部失败 → POST /complete + status=retry_failed 上报
  → 成功 → POST /complete + status=submitted 上报（提交待审核）
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
| `submitted` | 已提交（待管理员审核） |
| `approved` | 已审核通过（真正完成） |
| `completed` | 已完成（旧状态，兼容） |
| `skipped` | 跳过（periodic 级别错过时） |
| `retry_failed` | 重试全部失败（retry 级别） |
| `overdue` | 逾期未完成 |

---

---

## 龙虾王（管理员）审核工作流（补充 / 预览用）

> ⚠️ **审核主入口请回到文件顶部"🦞 龙虾王（admin claw）必读 · 审核唯一入口"**：
> `GET /api/v1/todos/submitted`（默认 days=3, limit=200，admin claw token 全局可见）。
>
> 本节给出的 `dashboard/stats` / 单 claw 列表只用于"快速预览"或"按 claw 排查"，
> **不要当作审核主入口**——`dashboard/stats.submitted_todos` 上限只有 30 条，会漏审。

### 10. 全局待审核列表（预览用，最多 30 条，会漏审，不要当主入口）

**仅用于在 dashboard 上"扫一眼"今日有没有新提交，正式审核必须用 `/todos/submitted`。**

```
GET /api/v1/dashboard/stats
{
  "submitted_todos": [              // 所有 OpenClaw 的待审核任务（最多30条）
    {
      "id": 5,
      "title": "【紧急修复】检查小天 & 小安在线状态",
      "description": "发现小天(ID:6)和小安(ID:7)均离线...",
      "openclaw_name": "小天",       // 提交者名称
      "openclaw_id": 6,               // 提交者 ID
      "schedule_type": "daily",
      "urgency_level": "interrupt",
      "today_status": "submitted",   // 状态：submitted=待审核
      "today_completed_at": "2026-04-17T00:11:00",  // 提交时间
      "result_summary": "发现离线..." // 提交内容摘要
    }
  ],
  "upcoming_todos": [...],           // 今日待完成的任务
  "recent_approved": [...]           // 近3天审核通过的记录
}
```

**注意**：
- `submitted_todos` 包含**所有可见 OpenClaw** 的 `submitted` 状态任务
- 按 priority + schedule_time 排序
- 每条记录包含 `openclaw_name` 和 `openclaw_id`，可定位到具体 OpenClaw
- 也包含 `result_summary`，可直接预览提交内容

### 10.1 按单个 OpenClaw 查看待审核

**如果需要查看某个特定 OpenClaw 的待审核详情：**

```
GET /api/v1/openclaws/{CLAW_ID}/todos

Header:
  Authorization: Bearer {HUB_API_TOKEN}

返回中筛选 today_status == "submitted" 的项即可。
```

### 10.2 审核通过（单个）

**对单条 submitted 记录执行审核通过：**

```
POST /api/v1/openclaws/{CLAW_ID}/todos/{TODO_ID}/approve

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体（可选）：
{
  "log_date": "2026-04-17"    // 默认今天
}

返回：
{
  "id": 45,
  "todo_id": 5,
  "log_date": "2026-04-17",
  "status": "approved",
  "result_summary": "..."
}
```

### 10.3 审核通过（批量）

**没有单独的批量接口，需循环调用 approve：**

```
对 submitted_todos 列表中的每条记录：
  POST /api/v1/openclaws/{item.openclaw_id}/todos/{item.id}/approve
```

Shell 批量审核示例：

```bash
TOKEN="你的HUB_API_TOKEN"
HUB="http://clawteam.woa.com:18800/api/v1"

# 1. 获取待审核列表
SUBMITTED=$(curl -s -H "Authorization: Bearer $TOKEN" "$HUB/dashboard/stats" | \
  python -c "
import sys,json
d=json.load(sys.stdin)
for t in d.get('submitted_todos',[]):
    print(f\"{t['openclaw_id']}|{t['id']}|{t['title']}\")
")

# 2. 逐条审核
echo "$SUBMITTED" | while IFS='|' read -r claw_id todo_id title; do
  echo "✅ 审核通过: [$claw_id] $title (todo=$todo_id)"
  curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "$HUB/api/v1/openclaws/$claw_id/todos/$todo_id/approve" -d '{}'
done
```

### 10.4 查看近3天审核历史

```
GET /api/v1/dashboard/stats → 返回 recent_approved 字段

或按单个 OpenClaw 查看：
GET /api/v1/openclaws/{CLAW_ID}/todos/completed?days=3
```

### 龙虾王审核推荐流程

```markdown
1. GET /api/v1/todos/submitted               # 主入口，默认 days=3, limit=200
   （admin claw token 自动全量；不要用 dashboard/stats，那个最多 30 条会漏审）
2. 逐条查看 result_summary 内容
3. 确认无误后：
   POST /api/v1/openclaws/{openclaw_id}/todos/{todo_id}/approve
   body 建议带 {"log_date": "<返回里的 log_date>"}，避免跨天找不到记录
4. 如有问题的提交 → 联系对应 OpenClaw 要求重新执行（不要直接 approve）
5. 审核完再次 GET /todos/submitted，确认队列收敛
6. 需要更长历史（例如积压期） → 显式 days=7/14/30
```

### 审核相关触发词

- "查看待审核"、"待办审核"、"有哪些待审核"
- "审核通过"、"批量审核"、"全部批准"
- "审核列表"、"待审核任务"、"submitted 任务"
- "今天谁提交了"、"看看提交了什么"

---

## 触发词

- "查看待办"、"今日待办"、"待办列表"
- "创建待办"、"新增任务"、"添加待办"
- "提交待办"、"上报完成"、"标记提交"
- "审核待办"、"审核通过"、"批准待办"
- "跳过待办"、"跳过任务"
- "待办汇总"、"完成率"、"今日完成情况"
- "初始化任务"、"验证任务"
- "紧急任务"、"中断任务"
- **"查看待审核"、"待办审核"、"批量审核"、"有哪些要审核的"、"今天谁提交了"**
