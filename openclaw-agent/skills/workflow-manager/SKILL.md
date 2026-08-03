# Workflow 自动化编排 (workflow-manager)

## 简介

本 Skill 用于让 OpenClaw / DeepFlow worker 接入 Hub Workflow，把长流程复杂任务拆成单个独立节点执行。

Workflow 的定位不是完整 n8n，也不是让 Hub 云端执行 Unity/ADB/Shell；它是 Hub 内的轻量流程编排器：

- Hub 负责：把长流程拆为节点、保存节点说明和参考内容、推进节点状态、通知目标 Agent/owner、归档证据、页面展示。
- Agent / worker 负责：执行单个节点任务，自行监控执行情况，并明确回写节点状态。
- 大模型相关节点走 `agent_task`：Hub 通知指定 Agent，由 Agent 使用自身 Skill、Rules、工具、知识库、测试报告、课题讨论、测试用例等上下文执行。
- 外部写操作（推群、触发蓝盾、push 分支等）必须走审批或预授权，不能私自跳过。
- Hub 的 subprocess timeout / AgentTask timeout 只是执行通道信号，不等于业务阻断；是否阻断应由 Agent/worker 或 owner 明确上报。

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar` 或具备轮询 Hub API 的 worker。
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`。
- Hub 地址：

```text
Hub API: http://clawteam.woa.com:18800/api/v1
认证:    Authorization: Bearer {HUB_API_TOKEN}
页面:    http://clawteam.woa.com:18800/workflows
```

所有请求默认：

```http
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

---

## 核心概念

### Workflow Definition

Workflow 模板，定义一组步骤、依赖关系、runner、门禁和审批要求。

### Workflow Run

一次实际执行。每次创建 Run 后，Hub 会生成对应的 Step 实例并推进状态。

### Workflow Step

单个节点实例。对 Agent 和页面优先理解为四态：

| 用户态 | 含义 | 内部兼容状态 |
|---|---|---|
| `todo` / 待开始 | 依赖未满足或等待 Hub 推进 | `pending` |
| `running` / 执行中 | 已派发给 Agent/worker/owner，正在执行或等待明确回写 | `running` / `retrying` / `waiting_approval` |
| `blocked` / 阻断 | 节点遇到业务阻断，需要人工或 Agent 处理后恢复 | `blocked` / `failed` |
| `done` / 结束 | 节点已完成或被明确跳过，Hub 可推进后续节点 | `passed` / `skipped` / `succeeded` |

内部技术状态仍会出现在部分旧 API、日志或数据库中：

| 状态 | 含义 |
|---|---|
| `pending` | 等待依赖满足或等待调度 |
| `running` | 已派发，worker/Agent 正在执行 |
| `retrying` | 重试中 |
| `waiting_approval` | 等待人工审批 |
| `passed` | 通过 |
| `blocked` | 被门禁或 blocker 阻断，可人工处理后重试/恢复 |
| `failed` | 执行失败 |
| `skipped` | 人工跳过并记录 |

> 执行原则：单个节点阻断后，可以由 owner/管理员/目标 Agent 在页面或 API 中改回“执行中”继续运行；节点被标记为“结束”后，Hub 才推进下一个节点。

### Step References

每个节点可以附带参考内容，帮助 Agent 执行单个节点时目标更明确。支持：

| `type` | 含义 |
|---|---|
| `knowledge` | 知识库条目 |
| `test_report` | Hub 测试报告 |
| `topic` | 课题讨论 |
| `testcase` | 测试用例 |
| `skill` | Skill 文档或路径 |
| `work_rule` | 工作规范 / Rule |
| `url` | 外部或站内链接，仅允许 `http://`、`https://` 或非 `//` 开头的站内相对路径 |

示例：

```json
{
  "references": [
    {"type": "knowledge", "id": 12, "title": "登录系统常见坑"},
    {"type": "test_report", "id": 34, "title": "上一轮冒烟报告"},
    {"type": "skill", "path": "openclaw-agent/skills/test-report-manager/SKILL.md"},
    {"type": "work_rule", "title": "测试报告规范"},
    {"type": "url", "url": "/test-reports/34", "title": "报告页面"}
  ]
}
```

### Artifact / Evidence

Step 产生的证据，如 JSON、HTML、截图、日志、Hub 报告、蓝盾构建链接、APK 链接等。Worker 应尽量把证据写入 `evidence` 字段，由 Hub 自动归档。

---

## 节点类型

一期支持以下类型：

| 类型 | 用途 |
|---|---|
| `worker_task` | DeepFlow worker 执行确定性自动化任务，如 Unity 冒烟、ADB 冒烟、蓝盾触发 |
| `agent_task` | Hub 通知指定 Agent，由 Agent 使用 Skill/工具执行分析或操作 |
| `approval` | 纯人工审批节点 |
| `gate` | Hub 确定性门禁判断 |
| `notification` | 通知/推群类节点，通常需要审批 |
| `llm_call` | 预留，后续用于纯文本总结/提取，不作为一期主路径 |

> 原则：涉及真实外部写操作、命令执行、Unity/ADB/Git 的节点，不走 Hub 直接大模型 API，应该走 worker/Agent。

---

## 常用流程

### 1. 查看 Workflow 模板

```http
GET /api/v1/workflow-definitions
```

分页查询：

```http
GET /api/v1/workflow-definitions?page=1&per_page=10
GET /api/v1/workflow-definitions?favorite=1&page=1&per_page=20
GET /api/v1/workflow-runs?page=1&per_page=50
```

规则：`per_page` 只支持 `10/20/50`，默认 `10`；带分页参数时返回 `{items, pagination}`，不带分页参数时兼容旧版数组返回。

返回示例：

```json
[
  {
    "id": 1,
    "workflow_key": "racinggo_qa_auto_test",
    "name": "RacingGO qa_auto_test 全流程",
    "status": "active",
    "version": 1
  }
]
```

### 2. 创建 Workflow Run

```http
POST /api/v1/workflow-runs

{
  "workflow_key": "racinggo_qa_auto_test",
  "run_name": "RacingGO 2026-07-02 qa_auto_test",
  "start_vars": {
    "branch": "qa_auto_test",
    "device_pool": "android_smoke",
    "build_no": "20260702.1"
  },
  "context": {
    "workspace": "F:/DeepFlow",
    "project": "RacingGO"
  }
}
```

Hub 会：

1. 创建 `workflow_run`。
2. 创建所有 step 实例。
3. 找到依赖已满足的第一个 step。
4. 将 step 置为执行中，并通过 Hub 消息/AgentTask 通知目标 Agent；如果节点没有目标 Agent，则回退通知模板 owner 绑定的 Agent。

启动参数变量：

- `start_vars` 必须是 JSON 对象，只属于本次 Run，不修改模板 Definition。
- Hub 会把 `start_vars` 写入 `run.context.start_vars`，并同步到 `run.context.workflow_start.variables`。
- 后续 worker task、AgentTask、gate、branch 都可以读取：推荐 `start_vars.branch`，兼容 `context.start_vars.branch`。
- API 兼容字段：`start_vars`（推荐）、`variables`、`start_parameters`。

权限规则：

- 所有已注册 Agent 都可以创建 Workflow Definition。
- 新建模板默认只有创建者可以启动 Run；创建者可以是 Agent，也可以是 Web 用户。
- 如果要让其他 Agent 或用户执行该模板，必须由创建者或管理员添加执行者。
- 系统内置模板（例如 `racinggo_qa_auto_test`）保留公开可执行，避免影响现有自动化入口。

添加执行者：

```http
POST /api/v1/workflow-definitions/{DEFINITION_ID}/executors

{
  "actor_type": "claw",
  "actor_id": 4
}
```

也支持添加用户：

```json
{
  "actor_type": "user",
  "actor_id": 7
}
```

页面操作：打开 `/workflows` -> 点击模板 -> 如果你是创建者，会看到「添加执行者」，输入 `claw:4` 或 `user:7`。

更新已有模板：

```http
PATCH /api/v1/workflow-definitions/{DEFINITION_ID}

{
  "name": "RacingGO V3",
  "description": "更新节点编排",
  "definition": {
    "steps": [
      {
        "id": "precheck",
        "name": "前置检查",
        "runner": "deepflow.racinggo.precheck"
      }
    ],
    "context": {
      "branch": "qa_auto_test"
    }
  },
  "status": "active"
}
```

规则：

- 只有模板创建者或管理员可以更新已有 Workflow Definition。
- `workflow_key` 不允许通过更新接口修改；Hub 会保留原 key，避免把原地更新变成隐式重命名/新建。
- 可更新 `name`、`description`、`definition.steps`、`definition.context`、`version`、`status`、`visibility_scope`。
- 如果用户要求“不要新建模板”，必须使用这个接口，不要调用集合接口 `POST /api/v1/workflow-definitions`。

### 3. Worker 拉取待执行任务

```http
GET /api/v1/workflow-runs/worker/tasks
```

返回当前 token 可执行的 running/retrying step。Hub 必须同时满足：

- `workflow_runs.status in ("running", "retrying")`
- `workflow_run_steps.status in ("running", "retrying")`
- `target_claw_id` / `executor_claw_ids` 包含当前 `CLAW_ID`；如果节点未指定目标，则只允许模板 owner 绑定的 Agent 处理

如果 run 已经是 `cancelled/succeeded/failed/blocked/waiting_approval`，即使 step 仍残留 `running`，也不应该出现在 worker task 队列。

`waiting_approval` 不进入 worker task 队列。审批节点由 Hub 页面或审批 API 处理，审批通过后 Hub 才会把后续可执行的 worker step 派发为 `running`，worker 再从 `/worker/tasks` 拉取。

示例：

```json
[
  {
    "run_id": 12,
    "step_id": "editor_health",
    "name": "Unity Health",
    "type": "worker_task",
    "runner": "deepflow.unity.health",
    "status": "running",
    "attempt_no": 1,
    "config": {
      "id": "editor_health",
      "depends_on": ["merge_dev"]
    },
    "context": {
      "start_vars": {
        "branch": "qa_auto_test",
        "device_pool": "android_smoke"
      }
    },
    "start_vars": {
      "branch": "qa_auto_test",
      "device_pool": "android_smoke"
    },
    "progress_api": "/api/v1/workflow-runs/12/steps/editor_health/progress",
    "result_api": "/api/v1/workflow-runs/12/steps/editor_health/result"
  }
]
```

> Worker 看到不是自己支持的 `runner`，不要抢执行；应忽略或记录为不支持。

### 4. 回写 Step 结果

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/result
```

通过示例：

```json
{
  "status": "passed",
  "summary": "Unity health green=7 red=0",
  "metrics": {
    "green": 7,
    "red": 0,
    "failed": 0
  },
  "outputs": {
    "editor_ready": true,
    "report_id": 123
  },
  "evidence": {
    "json_report": "F:/DeepFlow/data/reports/unity_health.json",
    "html_report": "http://clawteam.woa.com:18800/api/v1/test-reports/123/html-preview",
    "screenshots": [
      "F:/RacingGoUnity/UnityProj/Library/DeepFlowScreenshots/health_001.png"
    ]
  },
  "logs": {
    "terminal_log": "Health check completed",
    "error_excerpt": ""
  }
}
```

阻断示例：

```json
{
  "status": "blocked",
  "summary": "报告存在 missing screenshot",
  "metrics": {
    "missing_screenshot_count": 41
  },
  "blocker": {
    "type": "missing_screenshots",
    "message": "报告中存在 41 个 missing screenshot card",
    "suggested_action": "回填 before_settle_return/game_over 截图后重试"
  },
  "evidence": {
    "html_report": "http://clawteam.woa.com:18800/test-reports/123"
  }
}
```

Hub 收到结果后会：

1. 保存 summary、metrics、outputs、evidence、logs、blocker。
2. 执行 step 配置中的 gates。
3. 如果 step 配置了 `branches`，用本 step result 和全局 `outputs` 评估 if/then/else，并把未命中的分支节点置为 `skipped`。
4. 根据结果推进 Run：
   - 通过：派发下一步。
   - 门禁失败：进入 `blocked`。
   - 需要审批：进入 `waiting_approval`。

### 4.1 手动变更 Step 四态

owner、管理员或目标 Agent 可以通过页面或 API 修改节点状态：

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/status

{
  "display_state": "running",
  "summary": "环境已恢复，继续执行"
}
```

`display_state` 可选：

| 值 | 作用 |
|---|---|
| `todo` | 回到待开始 |
| `running` | 设为执行中；从阻断恢复时会递增 attempt 并重新通知目标 Agent/owner |
| `blocked` | 标记阻断；可附 `blocker` |
| `done` | 标记结束；Hub 会推进下一个满足依赖的节点 |

约束：

- `cancelled` Run 不能再改状态。
- 无目标节点不允许任意 Agent 抢改，只能 owner fallback 或管理员处理。
- 页面上的“设为执行中 / 标记阻断 / 标记结束”就是调用此接口。

### 4.2 上报 Step 进度

长耗时节点不要把“任务进展”塞进心跳。心跳只表示执行器还活着；下载包体、安装、跑测试、分析日志等阶段性进展应调用 progress API：

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/progress

{
  "worker_id": "racinggo-mobile-worker-1",
  "phase": "download_package",
  "message": "正在下载 Android 包体 380MB / 920MB",
  "percent": 41,
  "progress": {
    "downloaded_mb": 380,
    "total_mb": 920
  },
  "heartbeat": true
}
```

规则：

- `phase/message/percent/progress` 至少传一个；`percent` 会被限制在 `0-100`。
- `heartbeat=true` 时，本次 progress 也会刷新心跳，适合 worker 在关键阶段同时说明“还活着、走到哪了”。
- Hub UI 会在节点卡片和详情面板显示最近 progress；progress 或 heartbeat 长时间不更新只作为“未响应/需提醒”信号，默认不直接把 step 判失败。
- `AgentTask` / subprocess timeout 会记录为执行通道告警，节点保持执行中，等待 Agent 或 owner 明确改为阻断/结束。
- 真正 `blocked/failed` 应由执行方通过 result API 或四态 status API 明确上报，或由 gates/审批规则判定；只有 step 配置显式启用 `auto_block_on_heartbeat_loss`、`heartbeat_auto_block` 或 `auto_block_on_no_response` 时，Hub 才会按未响应自动阻断。
- `agent_task` payload 会包含 `progress_api`，sidecar v2.2+ 会在启动/结束时自动上报基础 progress；Agent 执行长任务时仍应在关键阶段主动调用 progress API。

### 5. 审批节点

外部写操作必须审批，例如推群、push、触发蓝盾构包。

页面审批：

```text
打开 /workflows -> 进入 Run 详情 -> 点击「批准继续」
```

API 审批：

```http
POST /api/v1/workflow-runs/{RUN_ID}/approvals/{APPROVAL_ID}/approve

{
  "comment": "Editor 全绿，允许推群/构包"
}
```

审批后 Hub 会把对应 step 从 `waiting_approval` 释放为可执行状态，并继续推进。

### 6. 重试当前步骤

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/retry

{}
```

适用：

- 临时网络失败。
- 截图/报告已人工修复。
- Unity/ADB 卡住后已恢复环境。

### 7. 从指定步骤恢复

```http
POST /api/v1/workflow-runs/{RUN_ID}/resume

{
  "from_step_id": "editor_report"
}
```

Hub 会从该步骤开始重置后续步骤为 `pending`，再重新推进。

### 8. 终止 Run

```http
POST /api/v1/workflow-runs/{RUN_ID}/cancel

{
  "summary": "用户确认终止，本轮 qa_auto_test 作废"
}
```

终止后 Hub 需要把未完成 step 从 worker 队列清掉：

- run 状态置为 `cancelled`，`current_step_id` 清空，写入 `finished_at`。
- `pending/running/retrying/waiting_approval` 的 step 置为 `skipped`，并写入终止摘要。
- pending approval 置为 `skipped`，避免终止后仍可审批继续。
- worker 拉任务时必须额外过滤 run 状态，只看 step 状态是不够的。
- 如果 worker 已经拉到旧任务，执行中发现 run 已 `cancelled`，必须停止执行，不要继续 push/构包/推群；Hub 会拒绝 cancelled run 的 result/retry/resume/approval。

---

## Step Result 协议

Worker / Agent 回写时请尽量遵守：

| 字段 | 必填 | 说明 |
|---|---|---|
| `status` | 是 | `passed` / `failed` / `blocked` / `skipped` |
| `summary` | 建议 | 一句话说明本步骤结果 |
| `metrics` | 建议 | 结构化指标，供 gates 判断 |
| `evidence` | 建议 | JSON/HTML/截图/构建链接/APK/报告等证据 |
| `logs` | 可选 | terminal 摘要、错误片段 |
| `blocker` | 阻断时必填 | `type/message/suggested_action` |

不要只回写大段自然语言。能结构化的结果必须结构化，方便 Hub 自动门禁和恢复。

建议固定使用这些 `evidence` key，方便 Hub UI 后续做固定展示：

| key | 说明 |
|---|---|
| `json_report` | 结构化 JSON 报告路径或 URL |
| `html_report` | HTML 报告预览 URL |
| `share_url` | 可分享给人的 Hub 页面或外部报告链接 |
| `build_id` | 蓝盾/构建系统 build id |
| `build_url` | 构建详情链接 |
| `apk_path` | APK 本地路径或制品路径 |
| `apk_url` | APK 下载链接 |
| `device_id` | 手机/模拟器设备 ID |
| `screenshots` | 截图路径或 URL 数组 |
| `log_path` | 完整日志路径 |

---

## RacingGO Runner Schema

所有 RacingGO runner 的输入都来自 `task.config.inputs` 和 run `context`。Worker 执行前必须读取 run 详情，确认 run 仍为 `running/retrying`；如果 run 已 `cancelled`，直接停止，不要回写新的业务结果。

通用输入：

```json
{
  "project": "RacingGO",
  "branch": "qa_auto_test",
  "workspace": "F:/DeepFlow",
  "unity_project": "F:/RacingGoUnity/UnityProj",
  "report_dir": "F:/DeepFlow/data/reports/{run_id}",
  "run_id": 12,
  "step_id": "lib20"
}
```

通用输出：

```json
{
  "status": "passed",
  "summary": "一句话结论",
  "metrics": {},
  "evidence": {
    "json_report": "本地 JSON 路径或 Hub URL",
    "html_report": "Hub 报告 URL",
    "share_url": "可分享报告 URL",
    "build_id": "",
    "build_url": "",
    "apk_path": "",
    "apk_url": "",
    "device_id": "",
    "screenshots": [],
    "log_path": ""
  },
  "logs": {
    "terminal_log": "关键日志摘要",
    "error_excerpt": ""
  }
}
```

### `deepflow.racinggo.precheck`

用途：检查工作区、分支、凭证、工具链和报告目录是否可用。

建议 metrics：

```json
{
  "env_ready": true,
  "workspace_clean": true,
  "secrets_ready": true,
  "branch_exists": true,
  "disk_free_gb": 120,
  "python_ready": true,
  "unity_project_exists": true
}
```

门禁样例：

```json
[
  {"expression": "metrics.env_ready == true", "on_fail": "blocked"},
  {"expression": "metrics.workspace_clean == true", "on_fail": "blocked"},
  {"expression": "metrics.secrets_ready == true", "on_fail": "blocked"}
]
```

### `deepflow.racinggo.merge_dev`

用途：合入 `origin/dev` 或指定源分支，产出合并结果。

建议 metrics：

```json
{
  "conflict_count": 0,
  "changed_files": 18,
  "local_head": "abc123",
  "source_head": "def456"
}
```

门禁样例：

```json
[
  {"expression": "status == \"passed\"", "on_fail": "blocked"},
  {"expression": "metrics.conflict_count == 0", "on_fail": "blocked"}
]
```

### `deepflow.racinggo.editor_lib20`

用途：执行库20合线门禁，检查用例失败、报告质量和截图完整性。

建议 metrics：

```json
{
  "total": 120,
  "passed": 120,
  "failed": 0,
  "blocked": 0,
  "missing_screenshot_count": 0,
  "report_error_count": 0
}
```

门禁样例：

```json
[
  {"expression": "metrics.failed == 0", "on_fail": "blocked"},
  {"expression": "metrics.missing_screenshot_count == 0", "on_fail": "blocked"},
  {"expression": "metrics.report_error_count == 0", "on_fail": "blocked"}
]
```

### `deepflow.racinggo.editor_lib21`

用途：执行库21关卡矩阵，重点覆盖关卡链路和截图质量。

建议 metrics：

```json
{
  "total": 80,
  "passed": 80,
  "failed": 0,
  "missing_screenshot_count": 0,
  "level_matrix_failed": 0
}
```

门禁样例：

```json
[
  {"expression": "metrics.failed == 0", "on_fail": "blocked"},
  {"expression": "metrics.level_matrix_failed == 0", "on_fail": "blocked"},
  {"expression": "metrics.missing_screenshot_count == 0", "on_fail": "blocked"}
]
```

### `deepflow.racinggo.endless`

用途：执行无尽专项，重点检查 ghost、结算和异常退出。

建议 metrics：

```json
{
  "total": 30,
  "passed": 30,
  "failed": 0,
  "ghost_bad": 0,
  "crash_count": 0,
  "missing_screenshot_count": 0
}
```

门禁样例：

```json
[
  {"expression": "metrics.ghost_bad == 0", "on_fail": "blocked"},
  {"expression": "metrics.failed == 0", "on_fail": "blocked"},
  {"expression": "metrics.crash_count == 0", "on_fail": "blocked"}
]
```

### `deepflow.hub.editor_report`

用途：汇总 Editor 阶段报告，生成 Hub 可查看报告和最终质量指标。

建议 metrics：

```json
{
  "source_report_count": 3,
  "failed": 0,
  "missing_screenshot_count": 0,
  "report_error_count": 0,
  "html_generated": true
}
```

门禁样例：

```json
[
  {"expression": "metrics.failed == 0", "on_fail": "blocked"},
  {"expression": "metrics.missing_screenshot_count == 0", "on_fail": "blocked"},
  {"expression": "metrics.html_generated == true", "on_fail": "blocked"}
]
```

### `deepflow.bkci.trigger_qa_build`

用途：审批后 push 并触发蓝盾构包。该节点属于外部写操作，必须审批后执行。

建议 metrics：

```json
{
  "local_head": "abc123",
  "remote_head": "abc123",
  "pushed": true,
  "build_triggered": true,
  "build_id": "p-12345",
  "apk_url_ready": false
}
```

门禁样例：

```json
[
  {"expression": "metrics.remote_head == metrics.local_head", "on_fail": "blocked"},
  {"expression": "metrics.build_triggered == true", "on_fail": "blocked"}
]
```

### `deepflow.racinggo.mobile_smoke`

用途：安装手机包并执行移动端冒烟。

建议 metrics：

```json
{
  "install_ok": true,
  "launch_ok": true,
  "failed": 0,
  "crash_count": 0,
  "anr_count": 0,
  "missing_screenshot_count": 0
}
```

### `deepflow.hub.mobile_final_report`

用途：生成手机端最终报告并准备推群文案。推群前仍需要审批。

建议 metrics：

```json
{
  "failed": 0,
  "crash_count": 0,
  "missing_screenshot_count": 0,
  "html_generated": true
}
```

---

## 门禁规则

Hub 一期只支持白名单表达式，不执行任意 Python/JS。

示例：

```json
{
  "gates": [
    {"expression": "metrics.failed == 0", "on_fail": "blocked"},
    {"expression": "metrics.missing_screenshot_count == 0", "on_fail": "blocked"},
    {"expression": "status == \"passed\"", "on_fail": "blocked"}
  ]
}
```

支持比较：

- `==`
- `!=`
- `>`
- `>=`
- `<`
- `<=`

左侧支持点路径，如 `metrics.failed`、`evidence.missing_screenshot_count`、`status`。右侧支持常量，也支持点路径比较，例如：

```json
{"expression": "metrics.remote_head == metrics.local_head", "on_fail": "blocked"}
```

### 输入变量、输出变量和 if/else 分支

每个 step 可以声明固定输入 `inputs` 和变量输入 `input_vars`：

```json
{
  "id": "mobile_smoke",
  "name": "手机包冒烟",
  "runner": "deepflow.racinggo.mobile_smoke",
  "depends_on": ["downloadandinstall"],
  "inputs": {
    "suite": "smoke"
  },
  "input_vars": {
    "apk_url": "outputs.downloadandinstall.apk_url",
    "device_pool": "start_vars.device_pool",
    "branch": "context.start_vars.branch"
  },
  "outputs": ["smoke_passed", "failed", "report_id"]
}
```

Worker 执行完成后通过 `/result` 回写 `outputs`。Hub 会按 step id 汇总为全局变量，例如 `downloadandinstall` 回写：

```json
{
  "status": "passed",
  "outputs": {
    "installed": true,
    "apk_url": "https://bkci.example/apk/racinggo.apk"
  }
}
```

后续 step 的分支可直接判断 `outputs.downloadandinstall.installed`：

```json
{
  "id": "downloadandinstall",
  "name": "蓝盾包下载与覆盖安装",
  "runner": "deepflow.bkci.download_install",
  "branches": [
    {
      "if": "outputs.downloadandinstall.installed == true",
      "then": ["prepare_env"],
      "else": ["download_failed"]
    }
  ]
}
```

规则：

- `branches[].if` 使用和 gates 相同的白名单表达式，不执行任意 Python/JS。
- `then` / `else` 写 step id 数组；命中的分支保持 `pending`，未命中的分支会被 Hub 自动置为 `skipped`。
- 分支目标 step 仍建议把当前 step 写进 `depends_on`，这样状态推进和 UI 依赖关系清晰。
- `outputs.<step_id>.<key>` 是跨节点变量；当前 step 的 gates 也可以用本次 result 的 `outputs.<key>`。
- `start_vars.<key>` 是本次 Run 启动参数变量，整个 Flow 生命周期内只读可用；也可以通过 `context.start_vars.<key>` 读取。

---

## Agent Task 节点怎么处理大模型能力

当 Workflow 里出现类似“分析报告 / 生成结论 / 判断 blocker / 生成推群文案”的大模型节点，一期推荐使用 `agent_task`：

```json
{
  "id": "analyze_editor_report",
  "name": "分析 Editor 报告",
  "type": "agent_task",
  "target_claw_id_var": "agent_id",
  "executor_claw_ids_var": "agent_ids",
  "notify_agent_on_start": true,
  "runner": "agent.skill.test-report-manager",
  "prompt": "请分析本次 Editor 报告，判断是否允许推群，并回写 workflow step result。",
  "references": [
    {"type": "test_report", "id": 123, "title": "Editor 冒烟报告"},
    {"type": "knowledge", "id": 456, "title": "报告评审标准"},
    {"type": "skill", "path": "openclaw-agent/skills/test-report-manager/SKILL.md"}
  ],
  "depends_on": ["editor_report"]
}
```

执行原则：

1. `agent_task` 必须能解析出目标 Agent。推荐运行前指定执行 Agent；模板里可以写 `target_claw_id_var: "agent_id"` 或 `executor_claw_ids_var: "agent_ids"`，Hub 会从本次 Run 的 `start_vars` 读取。也可固定写 `target_claw_id`；`target_agent` 仅作为按 OpenClaw 名称 / safe_name / claw_tag 匹配的兼容字段。
2. 目标优先级：`start_vars` 动态目标 > 启动 API 顶层 `executor_claw_ids/target_claw_ids` 覆盖 > 节点固定 `target_claw_id/target_agent`。
3. Hub 不再依赖聊天消息触发此类节点；Hub 会创建 `AgentTask(task_type="workflow_agent_task")`，sidecar 通过 SSE `task` 事件或轮询任务通道领取。
4. 如果节点配置 `notify_agent_on_start: true`，Hub 在创建 AgentTask 的同时会额外写一条 `ClawMessage(msg_type="task_delegate")` 到目标 Agent 聊天，用于明显提醒；这是可选项，不应作为执行链路唯一入口。
5. sidecar 以该 OpenClaw/Hermes Agent 自己的身份、记忆、Skills、Rules、项目上下文和工具执行节点任务。
6. sidecar 会自动上报基础 progress/heartbeat；Agent 执行长任务时应通过 `progress_api` 阶段性上报进度，例如下载、安装、执行、分析。
7. Agent/sidecar 最终通过 `/api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/result` 回写结构化结果。
8. AgentTask payload 会包含 `display_state` 和 `references`。执行前必须阅读节点 prompt 与 references，避免把复杂长流程重新当成一个大任务处理。
9. 如果目标 Agent 找不到或 sidecar 不支持 `workflow_agent_task`，该 step 应进入 `blocked`，不要静默等待聊天消息。

启动参数示例：

```json
{
  "start_vars": {
    "agent_id": 12,
    "agent_ids": [12, 15],
    "branch": "qa_auto_test"
  }
}
```

sidecar 传给 Agent 的结构化上下文包括：

- `run_id` / `run_name`
- `step_id` / `step_name`
- `runner`
- `prompt`
- `display_state`
- `references`
- `inputs`
- `input_vars`
- 上游 `outputs`
- run `context`
- `progress_api`
- `result_api`

后续可新增 `llm_call`，只用于纯文本总结/提取，不用于执行外部写操作。

---

## RacingGO 首个模板步骤

默认模板 key：

```text
racinggo_qa_auto_test
```

首批步骤：

1. `precheck`：前置检查
2. `merge_dev`：合入 `origin/dev`
3. `editor_health`：Unity Health
4. `lib20`：库20合线门禁
5. `lib21`：库21关卡矩阵
6. `endless`：无尽专项
7. `editor_report`：生成 Hub Editor 总报告
8. `notify_editor`：审批并推送 Editor 报告
9. `push_and_build`：审批 Push 并触发蓝盾构包
10. `mobile_smoke`：手机包冒烟
11. `final_report`：手机端最终报告与推群

---

## Worker 执行建议

### 拉取任务循环

推荐使用本 Skill 附带的独立 worker：

```text
scripts/workflow_worker.py
systemd/openclaw-workflow-worker.service
examples/openclaw-workflow-worker.env.example
examples/openclaw-workflow-runners.json
```

它会循环拉取 Hub 任务、按 runner 白名单过滤、先认领 step 租约，再执行本地命令并回写结果。不要长期靠人工模拟 step result。

伪代码：

```python
while True:
    tasks = GET /api/v1/workflow-runs/worker/tasks
    for task in tasks:
        if task["runner"] not in SUPPORTED_RUNNERS:
            continue
        POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/claim
        start_background_heartbeat_every_30s(task)
        POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/progress  # runner_started
        result = run_supported_runner(task)
        stop_background_heartbeat(task)
        POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/result
    sleep(10)
```

### Step 认领/租约

多个 worker 可能看到同一个 `running` step，因此执行前必须先 claim：

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/claim

{
  "worker_id": "racinggo-mobile-worker-1",
  "lease_seconds": 300
}
```

返回 200 才能执行。返回 409 表示已被其他 worker 认领且租约未过期，应跳过。

### Step 响应/健康灯

claim 成功后，worker 建议在 runner 执行期间持续上报 heartbeat 或 progress。Hub 只把它作为“执行方是否有响应”的显示和提醒信号，不再默认把远端进程当成本机 watchdog 管：

```http
POST /api/v1/workflow-runs/{RUN_ID}/steps/{STEP_ID}/heartbeat

{
  "worker_id": "racinggo-mobile-worker-1"
}
```

默认规则：

- 30 秒内收到 progress/heartbeat：Hub UI 节点显示绿色闪烁灯，表示执行方有响应。
- 超过 30 秒未收到：显示橙色闪烁灯，表示等待响应或响应延迟。
- 连续 3 次未收到（默认约 90 秒）：Hub 默认保持 step 为 `running/retrying`，触发未响应提醒：创建提醒型 `workflow_agent_task`、向模板创建者 Agent 发 `task_delegate` 聊天消息、创建 `P0/interrupt` 待办。
- Hub 不应替执行方判断远端任务是否失败。执行失败、被阻断、跳过或成功必须由 worker/Agent 通过 `/result` 明确回写，或由 gates/审批规则判定。
- 只有节点配置显式启用 `auto_block_on_heartbeat_loss`、`heartbeat_auto_block` 或 `auto_block_on_no_response` 时，连续未响应才会被 Hub 自动标记为 `blocked`；这是例外策略，不是默认策略。

本 Skill 附带的 `scripts/workflow_worker.py` 已内置心跳线程，可通过 `WORKFLOW_HEARTBEAT_SEC` 调整间隔，默认 30 秒；长任务更推荐在关键阶段上报 progress。

worker 执行本地 runner 时会注入这些环境变量：

- `WORKFLOW_TASK_JSON`：完整 task payload 文件路径。
- `WORKFLOW_RUN_ID` / `WORKFLOW_STEP_ID` / `WORKFLOW_RUNNER`：当前执行上下文。
- `WORKFLOW_PROGRESS_API`：完整 progress API URL，runner 脚本可直接 `curl -H "Authorization: Bearer $CLAW_TOKEN"` 上报阶段进度。
- `WORKFLOW_HEARTBEAT_API`：完整 heartbeat API URL；通常不需要 runner 自己调用，worker 已有后台心跳线程。

### systemd 安装建议

在实际执行机器上安装：

```bash
mkdir -p /opt/openclaw-agent/skills/workflow-manager
cp -r workflow-manager/* /opt/openclaw-agent/skills/workflow-manager/
cp /opt/openclaw-agent/skills/workflow-manager/examples/openclaw-workflow-worker.env.example /etc/openclaw-workflow-worker.env
cp /opt/openclaw-agent/skills/workflow-manager/examples/openclaw-workflow-runners.json /etc/openclaw-workflow-runners.json
cp /opt/openclaw-agent/skills/workflow-manager/systemd/openclaw-workflow-worker.service /etc/systemd/system/openclaw-workflow-worker.service
systemctl daemon-reload
systemctl enable --now openclaw-workflow-worker.service
```

必须修改 `/etc/openclaw-workflow-worker.env` 中的 `CLAW_ID/CLAW_TOKEN`，并在 `/etc/openclaw-workflow-runners.json` 中配置本机真实支持的 runner 命令。未配置的 runner 不会被 worker 认领。

### 幂等要求

Worker 必须避免重复执行危险动作：

- 推群前检查审批。
- 触发蓝盾前检查是否已有 build id。
- push 前检查 remote/local HEAD。
- 生成报告前尽量复用同一 run/step 的输出目录。

### 失败处理

遇到失败不要继续下一步。应回写：

```json
{
  "status": "blocked",
  "summary": "蓝盾触发失败",
  "blocker": {
    "type": "bkci_trigger_failed",
    "message": "HTTP 401",
    "suggested_action": "检查 secrets-vault 中蓝盾 token 是否过期"
  },
  "logs": {
    "error_excerpt": "HTTP 401 Unauthorized"
  }
}
```

---

## 安全边界

- 不要让 LLM 自由生成并执行 shell 命令。
- 不要绕过审批执行推群、push、构包等外部写操作。
- 不要把 secrets 写进 `summary/evidence/logs`。
- 不要把本地绝对路径当作公开链接；如果需要外部访问，先上传到 Hub 报告/附件或生成 share link。
- `evidence` 可以保存本地路径用于审计，但对人可点击的证据应优先使用 Hub URL。

---

## 触发词

- "启动 RacingGO QA Workflow"
- "创建 workflow run"
- "查看 workflow 状态"
- "重试 workflow 步骤"
- "从某一步恢复"
- "回写 workflow 结果"
- "审批 workflow 推群/构包"
- "查看 workflow 证据"

---

## 常见问题

### Q: 我执行完 step 但 Hub 没有推进下一步？

检查：

1. 是否 POST 到正确的 `{RUN_ID}/{STEP_ID}`。
2. `status` 是否是 `passed`。
3. gates 是否通过。
4. 下一步是否需要审批。

### Q: 任务重复出现在 `/worker/tasks`？

说明 step 仍是 `running/retrying`。如果已经执行，请回写 result；如果不能执行，请回写 `blocked` 并说明原因。

### Q: sidecar subprocess timeout 后是不是表示节点失败？

不是。timeout 是执行通道信号，Hub 会把它记录为进度/日志告警，节点仍保持“执行中”。Agent 或 owner 需要判断业务实际情况，再通过 result API 或 status API 明确标记为“阻断”或“结束”。

### Q: 能否直接改数据库推进状态？

不要。必须走 API，让 Hub 记录审批、证据、gate 和状态推进过程。

### Q: 什么时候用 `test-report-manager`？

当 step 生成 Hub 测试报告、上传附件、生成分享链接时，结合 `test-report-manager` Skill 使用。Workflow 只记录这份报告作为 evidence / artifact。
