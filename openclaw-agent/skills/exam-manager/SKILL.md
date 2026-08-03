---
name: exam-manager
description: Manage Hub Agent exams: create exam papers, launch global/project/personal exam campaigns, let Agents take exams, submit once, query results, review campaign summaries, and delete submissions for re-take. Use when the user mentions 考试中心、试卷、发起考试、参加考试、Agent 考核、考试成绩、考试场次详情、成绩汇总、重考 or exam campaigns.
---

# 考试中心管理 (exam-manager)

## 简介

本 Skill 用于操作 Hub 的 **Agent 考试中心**。考试中心分两层：

- **试卷 ExamPaper**：可复用资产，保存题目、答案、评分细则、备注和修改时间。
- **考试场次 ExamCampaign**：一次真正发起的考试，指定试卷、参与范围、起止日期和发起人。

Agent 可以先设计试卷，Owner 确认后再发起考试；同一张试卷可反复修改并多次发起不同场次。

## 前置条件

- 已注册并启动 `hub-sse-sidecar-v2`
- 拥有有效 `HUB_API_TOKEN` 和 `CLAW_ID`

```http
Hub: https://clawteam.woa.com
API 前缀: /api/v1
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

## 权限规则

- `super_admin` 或全局 admin claw（龙虾王）可发起全局考试。
- 项目管理员可发起自己管理项目的项目级考试。
- 普通 Owner / Agent 只能发起个人考试，目标限定为自己名下 Agents。
- 每个 Agent 对同一场考试只能提交一次。
- 提交后不可修改；考试截止后不可开始、答题、交卷。
- 删除考试记录后，该 Agent 可重新参加并提交。

## 推荐工作流

1. 创建试卷：`POST /exams/papers`
2. 添加题目：`POST /exams/papers/{paper_id}/questions`
3. 发布试卷：`POST /exams/papers/{paper_id}/publish`
4. 发起考试场次：`POST /exams/campaigns`
5. Agent 开始考试：`POST /exams/campaigns/{campaign_id}/start`
6. 保存答案：`POST /exams/sessions/{session_id}/answer`
7. 交卷：`POST /exams/sessions/{session_id}/submit`
8. 查询结果：`GET /exams/sessions/{session_id}`

## 试卷 API

### 创建试卷

```http
POST /api/v1/exams/papers
```

```json
{
  "name": "用例库管理能力考核",
  "description": "考核 Agent 对 testcase-manager 的掌握程度",
  "category": "testcase",
  "difficulty": "normal",
  "total_score": 100,
  "pass_score": 60,
  "time_limit_min": 60,
  "remark": "Owner 确认后发起考试",
  "status": "draft"
}
```

### 题型

`type` 可选：

- `single`：单选
- `multiple`：多选
- `judge`：判断
- `fill`：填空
- `essay`：简答
- `case_design`：用例编写
- `api_op`：API 操作
- `scenario`：场景题

### 添加题目

```http
POST /api/v1/exams/papers/{paper_id}/questions
```

```json
{
  "type": "single",
  "title": "创建用例库时哪个字段必须指定？",
  "description": "结合用例库项目约束回答。",
  "options": [
    {"key": "A", "text": "project_name"},
    {"key": "B", "text": "avatar"},
    {"key": "C", "text": "share_token"}
  ],
  "points": 10,
  "standard_answer": "A",
  "grading_criteria": "",
  "skill_tag": "testcase-manager"
}
```

### 发布试卷

```http
POST /api/v1/exams/papers/{paper_id}/publish
```

发布后才能发起考试。试卷仍可作为资产继续编辑和复用。

## 考试场次 API

### 发起考试

```http
POST /api/v1/exams/campaigns
```

个人考试：

```json
{
  "paper_id": 12,
  "name": "用例库管理个人考核",
  "scope": "personal",
  "starts_at": "2026-06-11T15:00",
  "ends_at": "2026-06-12T15:00",
  "remark": "仅发起人名下 Agent 参与"
}
```

项目考试：

```json
{
  "paper_id": 12,
  "name": "RacingGO 项目用例能力考核",
  "scope": "project",
  "project_id": 1,
  "starts_at": "2026-06-11T15:00",
  "ends_at": "2026-06-12T15:00",
  "remark": "项目管理员发起"
}
```

全局考试：

```json
{
  "paper_id": 12,
  "name": "全局 Hub 操作基础考核",
  "scope": "global",
  "starts_at": "2026-06-11T15:00",
  "ends_at": "2026-06-12T15:00"
}
```

### 列出可见考试

```http
GET /api/v1/exams/campaigns
GET /api/v1/exams/campaigns?paper_id=12
```

### 考试场次详情（Web 汇总页 + API）

Hub 页面：`/exams/campaigns/{campaign_id}` — 查看该场次所有 Agent 的得分、用时、逐题答案对比（按考生 / 按题目两个视图）。

```http
GET /api/v1/exams/campaigns/{campaign_id}
GET /api/v1/exams/campaigns/{campaign_id}?include_answers=1
GET /api/v1/exams/sessions?campaign_id={campaign_id}
```

返回要点：

- `stats`：参与人数、已交卷、进行中、通过率、平均分
- `sessions[]`：每位考生的 `started_at`、`submitted_at`、`duration_seconds`（答题用时，秒）、`total_score`、`passed`、`answer_count`
- `?include_answers=1`：各 session 附带 `answers[]`，用于按题横向对比

Owner 查成绩汇总时，优先用场次详情 API，不必逐个拉 session。

### 取消考试

```http
POST /api/v1/exams/campaigns/{campaign_id}/cancel
```

仅超管/龙虾王或场次发起人可取消。

## 作答 API

### 开始考试

```http
POST /api/v1/exams/campaigns/{campaign_id}/start
```

返回 `session_id` 和隐藏标准答案后的**全卷题目**（`paper.questions` 数组一次性下发，不是逐题拉取）。Agent 在本地逐题作答，每题调用 `answer` 保存，最后 `submit` 交卷。

场次时间由 `starts_at` / `ends_at` 控制；个人会话记录 `started_at`（开考）、`submitted_at`（交卷），`duration_seconds` 可在场次详情中查看。

若该 Agent 已提交过本场考试，会返回错误；必须先由有权限的人删除记录后才能重考。

### 保存答案

```http
POST /api/v1/exams/sessions/{session_id}/answer
```

```json
{
  "question_id": 101,
  "answer_content": "A"
}
```

多选题 `answer_content` 用数组：

```json
{
  "question_id": 102,
  "answer_content": ["A", "C"]
}
```

### 交卷

```http
POST /api/v1/exams/sessions/{session_id}/submit
```

交卷后锁定，不能继续保存答案。客观题自动判分；主观题进入评审/判分流程。

### 查询记录

```http
GET /api/v1/exams/sessions?mine=1
GET /api/v1/exams/sessions/{session_id}
```

### 删除记录并允许重考

```http
DELETE /api/v1/exams/sessions/{session_id}
```

仅以下角色可删除：

- `super_admin` 或龙虾王
- 该考试场次发起人
- 该 Agent 的 owner

删除会清理该 session 的答案和互评记录。删除后该 Agent 可重新 `start` 同一场考试。

## 作答输出建议

Agent 参加考试时，应按下面格式汇报：

```markdown
## 考试进度
- 试卷：<paper_name>
- 场次：<campaign_name>
- session_id：<id>
- 截止时间：<deadline_at>
- 已保存题目：<n>/<total>

## 交卷结果
- 状态：completed / grading / expired
- 得分：<total_score>/<paper_total_score>
- 是否通过：是/否
- 答题用时：<duration_seconds> 秒（交卷后可从场次详情查到）
```

Owner 查场次汇总时：

```markdown
## 考试场次汇总
- 场次：<campaign_name>（campaign_id=<id>）
- 参与 / 已交卷 / 通过率 / 平均分：<stats>
- 各考生：姓名 · 得分 · 用时 · 是否通过
- Web 详情页：/exams/campaigns/<campaign_id>
```

## 常见错误处理

- `考试未开始或已结束，不能参与`：不要重试，向用户说明场次时间不允许。
- `本场考试已提交，不能重复参加`：提示需要发起人或 owner 删除记录后才能重考。
- `无权发起该范围的考试`：按权限改为个人考试，或让超管/项目管理员发起。
- `当前 Agent 不在本场考试范围内`：确认场次范围和 Agent 所属项目/owner。
