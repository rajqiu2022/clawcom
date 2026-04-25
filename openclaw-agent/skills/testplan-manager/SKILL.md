# 测试计划管理 (testplan-manager)

## 简介

本 Skill 用于管理 Hub 中心的测试计划排期和测试任务，支持完整的测试项目管理流程：

- **测试计划 CRUD**：创建/查询/编辑/删除测试计划，关联项目与 TAPD 迭代
- **测试任务 CRUD**：在计划下创建/管理测试任务，关联用例库和 OpenClaw 执行人
- **用例执行追踪**：实时跟踪每个用例的执行状态（通过/失败/阻塞/跳过）
- **进度上报**：OpenClaw 通过 API 上报任务进度和用例执行结果
- **TAPD 迭代关联**：测试计划支持多选 TAPD 迭代，自动查询迭代列表
- **用例库节点筛选**：创建任务时支持按目录节点和优先级筛选用例

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar`（SSE 通信链路在线）
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub 地址: http://9.134.11.169:8088
API 前缀: /api/v1
```

## 认证方式

```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

---

## 数据结构

### 测试计划 (TestPlan)

```json
{
  "id": 1,
  "name": "v3.2 常规版本测试计划",
  "description": "覆盖 v3.2 版本所有功能测试",
  "version_type": "regular",
  "version_name": "v3.2.1",
  "start_date": "2026-04-15",
  "end_date": "2026-04-30",
  "project_id": 1,
  "project_name": "QQ飞车",
  "tapd_iteration_ids": ["10001", "10002"],
  "tapd_iteration_names": ["Sprint 3.2-1", "Sprint 3.2-2"],
  "tapd_workspace_id": "12345678",
  "status": "active",
  "total_tasks": 5,
  "completed_tasks": 2,
  "total_bugs": 3,
  "resolved_bugs": 1,
  "progress": 40.0,
  "created_by": "龙虾王",
  "created_at": "2026-04-15 10:00:00"
}
```

**版本类型**：`regular`（常规版本）、`resource`（资源版本）、`hotfix`（紧急补丁）
**状态**：`draft`（草稿）、`active`（进行中）、`completed`（已完成）、`archived`（已归档）

### 测试任务 (TestTask)

```json
{
  "id": 1,
  "plan_id": 1,
  "name": "功能测试-主流程",
  "description": "覆盖核心主流程功能测试",
  "task_type": "functional",
  "assignee_claw_id": 3,
  "assignee_name": "小测",
  "start_date": "2026-04-16",
  "end_date": "2026-04-20",
  "priority": "P1",
  "library_id": 2,
  "library_name": "登录模块用例库",
  "case_filter": {
    "module_paths": ["登录模块", "支付模块/退款"],
    "priorities": ["P0", "P1"]
  },
  "status": "in_progress",
  "progress": 60,
  "result_summary": "主流程功能基本正常，发现2个Bug",
  "total_cases": 20,
  "passed_cases": 12,
  "failed_cases": 2,
  "blocked_cases": 1,
  "skipped_cases": 0,
  "tapd_bug_ids": ["12345", "12346"],
  "bug_count": 2,
  "created_by": "龙虾王",
  "created_at": "2026-04-16 09:00:00"
}
```

**任务类型**：`functional`（功能）、`automation`（自动化）、`activity`（活动）、`performance`（性能）、`compatibility`（兼容性）、`security`（安全）、`interface`（接口）、`other`（其他）
**优先级**：`P0`（紧急）、`P1`（高）、`P2`（中）、`P3`（低）
**状态**：`pending`（待开始）、`in_progress`（进行中）、`completed`（已完成）、`blocked`（阻塞）、`skipped`（跳过）

### 用例执行记录 (TestTaskCase)

```json
{
  "id": 1,
  "task_id": 1,
  "case_id": 5,
  "case_title": "验证密码错误提示",
  "case_priority": "P1",
  "status": "passed",
  "executed_at": "2026-04-16 14:30:00",
  "executed_by": "小测",
  "note": "",
  "tapd_bug_id": null
}
```

**执行状态**：`pending`（待执行）、`passed`（通过）、`failed`（失败）、`blocked`（阻塞）、`skipped`（跳过）

### 用例筛选条件 (case_filter)

创建/编辑任务时，如果关联了用例库，可通过 `case_filter` 灵活筛选要导入的用例：

```json
{
  "module_paths": ["登录模块", "支付模块/退款"],
  "priorities": ["P0", "P1"],
  "case_ids": [1, 2, 3],
  "types": ["functional", "interface"]
}
```

| 字段 | 说明 |
|------|------|
| `module_paths` | 目录节点路径列表，支持前缀匹配（包含子目录用例） |
| `priorities` | 优先级列表，如 `["P0", "P1"]` |
| `case_ids` | 指定用例 ID 列表（精确匹配） |
| `types` | 用例类型列表 |

> 不设置 `case_filter` 或留空，则关联用例库的全部用例。

---

## 一、测试计划管理

### 1. 获取测试计划列表

```
GET /api/v1/test-plans

查询参数：
  project_id     按项目ID筛选
  status         按状态筛选（draft/active/completed/archived）
  version_type   按版本类型筛选（regular/resource/hotfix）
  search         搜索计划名称
```

**响应**：返回 TestPlan 数组。

### 2. 创建测试计划

```
POST /api/v1/test-plans

{
  "name": "v3.2 常规版本测试计划",       // 必填
  "description": "覆盖所有功能测试",     // 选填
  "version_type": "regular",             // 选填，默认 regular
  "version_name": "v3.2.1",              // 选填
  "start_date": "2026-04-15",            // 必填，YYYY-MM-DD
  "end_date": "2026-04-30",              // 必填，YYYY-MM-DD
  "project_id": 1,                       // 选填，关联项目ID
  "tapd_iteration_ids": ["10001","10002"], // 选填，TAPD迭代ID列表
  "tapd_iteration_names": ["Sprint 3.2-1","Sprint 3.2-2"], // 选填，冗余展示
  "tapd_workspace_id": "12345678",       // 选填，TAPD workspace ID
  "status": "draft"                      // 选填，默认 draft
}
```

### 3. 获取测试计划详情（含任务列表）

```
GET /api/v1/test-plans/{PLAN_ID}
```

**响应**：返回 TestPlan 对象，包含 `tasks` 数组。

### 4. 更新测试计划

```
PUT /api/v1/test-plans/{PLAN_ID}

{
  "name": "新名称",
  "status": "active",
  "tapd_iteration_ids": ["10001"],
  "tapd_iteration_names": ["Sprint 3.2-1"]
}
```

### 5. 删除测试计划

```
DELETE /api/v1/test-plans/{PLAN_ID}
```

> 删除计划会级联删除其下所有测试任务和用例执行记录。

### 6. 获取测试计划统计

```
GET /api/v1/test-plans/{PLAN_ID}/stats
```

**响应**：
```json
{
  "id": 1,
  "name": "v3.2 测试计划",
  "total_tasks": 5,
  "completed_tasks": 2,
  "progress": 40.0,
  "total_bugs": 3,
  "by_type": {
    "functional": {"total": 3, "completed": 1},
    "performance": {"total": 2, "completed": 1}
  },
  "by_status": {
    "pending": 1,
    "in_progress": 2,
    "completed": 2
  }
}
```

---

## 二、TAPD 迭代关联

### 7. 查询 TAPD 迭代列表

创建计划时，选择项目后可查询该项目对应的 TAPD 迭代，支持多选关联。

```
GET /api/v1/test-plans/tapd-iterations

查询参数：
  project_id   项目ID（不传则返回所有有 workspace_id 的项目迭代）
```

**响应**：
```json
[
  {
    "project_id": 1,
    "project_name": "QQ飞车",
    "workspace_id": "12345678",
    "iterations": [
      {
        "id": "10001",
        "name": "Sprint 3.2-1",
        "status": "open",
        "startdate": "2026-04-14",
        "enddate": "2026-04-25"
      },
      {
        "id": "10002",
        "name": "Sprint 3.2-2",
        "status": "planning",
        "startdate": "2026-04-28",
        "enddate": "2026-05-09"
      }
    ]
  }
]
```

> 使用方式：选择项目 → 调此 API 获取迭代列表 → 用户多选迭代 → 将选中的 `id` 和 `name` 分别存入 `tapd_iteration_ids` 和 `tapd_iteration_names`。

---

## 三、测试任务管理

### 8. 获取计划下的任务列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks

查询参数：
  task_type          按任务类型筛选
  status             按状态筛选
  assignee_claw_id   按指派 OpenClaw 筛选
```

### 9. 创建测试任务

```
POST /api/v1/test-plans/{PLAN_ID}/tasks

{
  "name": "功能测试-主流程",              // 必填
  "description": "覆盖核心流程",          // 选填
  "task_type": "functional",             // 选填，默认 functional
  "priority": "P1",                      // 选填，默认 P2
  "assignee_claw_id": 3,                 // 选填，指派 OpenClaw
  "library_id": 2,                       // 选填，关联用例库
  "case_filter": {                       // 选填，用例筛选条件
    "module_paths": ["登录模块"],
    "priorities": ["P0", "P1"]
  },
  "start_date": "2026-04-16",            // 选填
  "end_date": "2026-04-20",              // 选填
  "status": "pending"                    // 选填，默认 pending
}
```

> 如果指定了 `library_id` 和 `case_filter`，系统会自动根据筛选条件导入用例到任务中，创建 TestTaskCase 记录。

### 10. 获取任务详情

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}
```

**响应**：返回 TestTask 对象，包含 `task_cases` 用例执行列表。

### 11. 更新任务

```
PUT /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}

{
  "name": "新名称",
  "status": "in_progress",
  "progress": 75,
  "result_summary": "已完成大部分测试"
}
```

### 12. 删除任务

```
DELETE /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}
```

---

## 四、用例执行追踪

### 13. 获取任务下的用例执行列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases

查询参数：
  status   按执行状态筛选
```

### 14. 更新单条用例执行状态

```
PUT /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases/{TC_ID}

{
  "status": "failed",              // 必填：pending/passed/failed/blocked/skipped
  "note": "断言失败：期望200，实际500",
  "tapd_bug_id": "12345"
}
```

### 15. 批量更新用例执行状态

```
POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases/batch

{
  "updates": [
    {"tc_id": 1, "status": "passed"},
    {"tc_id": 2, "status": "failed", "note": "超时"},
    {"tc_id": 3, "status": "blocked", "tapd_bug_id": "12346"}
  ]
}
```

---

## 五、OpenClaw 进度上报 API

以下接口专供 OpenClaw（龙虾/Agent）通过 Token 认证上报任务进度。

### 16. 获取指派给 OpenClaw 的测试任务

```
GET /api/v1/openclaws/{CLAW_ID}/test-tasks

查询参数：
  status     按状态筛选
  plan_id    按计划筛选
```

**响应**：
```json
{
  "tasks": [
    {
      "id": 1,
      "plan_id": 1,
      "plan_name": "v3.2 测试计划",
      "name": "功能测试-主流程",
      "status": "in_progress",
      "progress": 60,
      "task_cases": [...]
    }
  ],
  "count": 1
}
```

### 17. 上报任务进度

```
POST /api/v1/openclaws/{CLAW_ID}/test-tasks/{TASK_ID}/report

{
  "status": "in_progress",            // 选填：更新任务状态
  "progress": 75,                     // 选填：更新进度百分比
  "result_summary": "执行摘要...",     // 选填：执行结果摘要
  "case_updates": [                   // 选填：批量更新用例状态
    {"case_id": 5, "status": "passed"},
    {"case_id": 6, "status": "failed", "note": "超时"},
    {"case_id": 7, "status": "blocked", "tapd_bug_id": "12345"}
  ],
  "tapd_bug_ids": ["12345", "12346"]  // 选填：关联 Bug ID 列表（增量追加）
}
```

> `tapd_bug_ids` 是增量追加，不会覆盖已有 Bug 关联。

---

## 六、用例库目录树 & 筛选 API

创建测试任务时，可查看用例库的目录结构和筛选用例。

### 18. 获取用例库目录树

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/tree
```

**响应**：
```json
{
  "library_id": 2,
  "library_name": "登录模块用例库",
  "total_cases": 25,
  "directories": [
    "登录模块",
    "登录模块/手机号登录",
    "登录模块/微信登录",
    "支付模块",
    "支付模块/退款"
  ],
  "priority_counts": {
    "P0": 3,
    "P1": 8,
    "P2": 10,
    "P3": 4
  },
  "tree": [
    {
      "name": "登录模块",
      "path": "登录模块",
      "count": 15,
      "children": [
        {"name": "手机号登录", "path": "登录模块/手机号登录", "count": 8, "children": []},
        {"name": "微信登录", "path": "登录模块/微信登录", "count": 7, "children": []}
      ]
    }
  ]
}
```

### 19. 获取用例库筛选后的用例列表

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases

查询参数：
  module_path   目录路径（前缀匹配，包含子目录）
  priority      优先级筛选（可多个，如 priority=P0&priority=P1）
  type          类型筛选（可多个）
  case_id       用例ID筛选（可多个）
```

> 此 API 用于创建任务前预览将导入的用例范围。

---

## 七、TAPD Bug 关联

### 20. 查询任务关联的 TAPD Bug 详情

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/tapd-bugs
```

**响应**：
```json
{
  "bugs": [
    {
      "id": "12345",
      "title": "登录页面白屏",
      "status": "open",
      "severity": "fatal",
      "priority": "high",
      "current_owner": "开发者A"
    }
  ],
  "count": 1
}
```

---

## 推荐工作流

### 创建测试计划

```
1. 获取项目列表 → GET /projects
2. 查询 TAPD 迭代 → GET /test-plans/tapd-iterations?project_id=X
3. 创建计划 → POST /test-plans（含迭代关联）
4. 查询用例库 → GET /testcase-libraries
5. 查看用例库目录树 → GET /testcase-libraries/{ID}/tree
6. 创建测试任务 → POST /test-plans/{ID}/tasks（含用例库关联和筛选）
7. 指派给 OpenClaw → 设置 assignee_claw_id
```

### OpenClaw 执行测试任务

```
1. 查询自己的任务 → GET /openclaws/{CLAW_ID}/test-tasks
2. 获取任务详情（含用例） → GET /test-plans/{PLAN_ID}/tasks/{TASK_ID}
3. 执行测试，上报进度 → POST /openclaws/{CLAW_ID}/test-tasks/{TASK_ID}/report
4. 批量更新用例状态 → case_updates 数组
5. 任务完成 → report { "status": "completed", "progress": 100 }
```

### API 创建任务（灵活筛选）

```
1. 查询用例库目录树 → GET /testcase-libraries/{ID}/tree
2. 预览筛选结果 → GET /testcase-libraries/{ID}/cases?priority=P0
3. 创建任务并指定筛选 → POST /test-plans/{ID}/tasks
   {
     "library_id": 2,
     "case_filter": {"priorities": ["P0"], "module_paths": ["登录模块"]}
   }
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 201 | 创建成功 | 正常处理 |
| 400 | 参数错误 | 检查必填字段 |
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` |
| 403 | 权限不足 / Token 无效 | 确认角色权限 |
| 404 | 资源不存在 | 检查计划/任务 ID |
| 500 | 服务器错误 | 查看 Hub 日志 |

---

## 触发词

- "创建测试计划"
- "查看测试计划"
- "测试排期"
- "创建测试任务"
- "指派测试任务"
- "查看任务进度"
- "上报测试进度"
- "更新用例状态"
- "测试计划管理"
- "测试任务管理"
- "关联 TAPD 迭代"
- "P0 用例任务"
- "查看我的任务"
