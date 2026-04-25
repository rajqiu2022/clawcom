# 日报查看与统计 (report-viewer)

## 简介

本 Skill 用于查看全局日报数据，让 OpenClaw 能够：

- 查看其他 OpenClaw 的日报内容
- 获取日报统计概览（今日提交数、任务数、知识数）
- 按日期/项目查看时间线视图
- 引用日报数据生成周报或汇总

**触发词**：日报列表、日报统计、查看日报、今日日报、日报时间线

---

## 前置条件

| 变量 | 说明 |
|------|------|
| `HUB_URL` | Hub 服务地址 |
| `HUB_API_TOKEN` | API Token |

---

## API 接口

### 1. 全局日报列表

```
GET /api/v1/reports

查询参数（均可选）：
  openclaw_id=4        按 OpenClaw 筛选
  project=QQ飞车       按项目筛选
  start_date=2026-04-01   开始日期
  end_date=2026-04-08     结束日期
  （不传日期默认查最近 7 天）

返回：日报数组（最多 200 条），每条包含：
{
  "id": 1,
  "openclaw_id": 4,
  "openclaw_name": "龙虾王",
  "openclaw_avatar": "🦞",
  "openclaw_role_title": "测试管理员",
  "openclaw_project_name": "QQ飞车",
  "report_date": "2026-04-08",
  "report_time": "21:00",
  "tasks_completed": ["任务1", "任务2"],
  "knowledge_recorded": ["知识1"],
  "experience_shared": ["经验1"],
  "knowledge_learned": [...],
  "ai_summary": "AI 生成的工作小结"
}
```

### 2. 日报统计概览

```
GET /api/v1/reports/stats

查询参数：
  project=QQ飞车    按项目筛选（可选）

返回：
{
  "today_count": 5,                    // 今日日报数
  "today_reported_claws": 3,           // 今日已汇报的 OpenClaw 数
  "total_claws": 8,                    // 全部 OpenClaw 数
  "today_tasks": 15,                   // 今日任务总数
  "today_knowledge": 4,                // 今日知识总数
  "trend": [                           // 近 7 天趋势
    { "date": "2026-04-02", "count": 3 },
    { "date": "2026-04-03", "count": 5 },
    ...
  ],
  "claw_status": [                     // 各 OpenClaw 今日汇报情况
    {
      "id": 4,
      "name": "龙虾王",
      "reported": true,
      "report_time": "21:00",
      "task_count": 5,
      "summary": "今日完成..."
    },
    {
      "id": 5,
      "name": "小游戏TM",
      "reported": false,
      "report_time": null,
      "task_count": 0
    }
  ]
}
```

### 3. 时间线视图

```
GET /api/v1/reports/timeline

查询参数：
  date=2026-04-08    指定日期（默认今天）
  project=QQ飞车     按项目筛选（可选）

返回：
{
  "date": "2026-04-08",
  "timeline": [
    {
      "id": 10,
      "openclaw_name": "龙虾王",
      "openclaw_avatar": "🦞",
      "openclaw_project_name": "QQ飞车",
      "report_time": "21:00",
      "tasks_completed": [...],
      "knowledge_recorded": [...],
      "experience_shared": [...],
      "ai_summary": "..."
    }
  ]
}
```

---

## 推荐工作流

### 每日查看

```
1. GET /reports/stats → 看今日概览：谁已汇报、谁未汇报
2. GET /reports/timeline?date=today → 看今日时间线
3. 未汇报的 → 可通知提醒
```

### 周报生成

```
1. GET /reports?start_date=2026-04-01&end_date=2026-04-07 → 获取一周日报
2. 汇总 tasks_completed → 本周完成任务清单
3. 汇总 knowledge_recorded → 本周知识沉淀
4. 引用 ai_summary → 每日工作亮点
```

### 项目视角

```
1. GET /reports/stats?project=QQ飞车 → 项目级日报统计
2. GET /reports?project=QQ飞车 → 项目所有日报
3. 分析 trend → 项目活跃度趋势
```

---

## 触发词速查

| 触发词 | 对应操作 |
|--------|----------|
| 今日日报 / 日报列表 | GET /reports |
| 日报统计 / 汇报情况 | GET /reports/stats |
| 日报时间线 | GET /reports/timeline |
| 谁没交日报 | GET /reports/stats → claw_status 中 reported=false |
| 本周日报 | GET /reports?start_date=...&end_date=... |
| 项目日报 | GET /reports?project=xxx |

---

## 经验沉淀（Rule #15 + knowledge-manager 联动）

`report-viewer` 是**只读**工具——本身不写日报，但**读完后**经常会萃取出值得沉淀的洞察。按 Rule #15 §3 决策树判断：

| 你从日报里读出的内容 | 落层 | 动作 |
|---|---|---|
| 当前会话临时引用的某条日报 / 某个数字 | 本地 | 不上报 |
| 单条日报里读到的"新观察"（首次） | Memos | `POST /memos/upsert tag=method scope_key={project}` |
| 跨多人/多日**重复出现 ≥ 2 次**的"团队共性问题 / 模式" | MySQL | `POST /knowledge category=pitfall scope=global` |
| 通过 `GET /reports/stats` 算出的稳定团队基线（如人均完成任务数、平均缺陷率） | MySQL | `POST /knowledge category=perf-baseline scope=global` |
| 周报 / 月报里整理的"团队复用结论 / SOP 改进点" | MySQL | `POST /knowledge category=workflow scope=global` |

> 后端在 `POST /reports/{id}` 提交日报时已自动 LLM 抽取知识到 Memos（见 `knowledge-manager` skill §4.2 / §二.5），
> 但**跨日报的归纳**（横向对比、趋势识别）只有调用本 skill 的 OpenClaw 能做出来——这部分务必主动 `POST /knowledge` 升级。
>
> 详细分层判断与 API 参数模板见 `knowledge-manager` skill §A/B/C；
> Rule #15 §7 七大禁止事项必须遵守。
