# Hub Workflow 自动化编排能力需求案

## 背景

当前 RacingGO 自动化全流程包含合入 `dev`、Unity Editor 冒烟、Hub 报告、企微推送、蓝盾构包、制品下载、真机安装、手机冒烟、最终报告等多个阶段。现在主要依赖小安/Agent 临场串联命令，流程长、状态复杂、外部依赖多，容易出现：

- 重复踩坑：Unity 卡 PlayMode、截图丢失、Hub 图片附件化、PowerShell 命令解析失败等。
- 过程不可恢复：中途失败后很难从某个阶段继续。
- 门禁靠人判断：是否可推第二群、是否可构包、是否需要人工确认不够结构化。
- 证据分散：日志、JSON、截图、报告、构建号、APK、账号使用记录散落在多个地方。

希望 Hub 增加类似 n8n 的轻量工作流能力，用于把这类自动化流程固化成可复跑、可审计、可恢复的标准 Runbook。

## 目标

在 Hub 内提供“自动化工作流 / Workflow Runbook”能力，让小安或 DeepFlow worker 按固定节点执行流程，并将每步结果、证据、审批、通知统一沉淀到 Hub。

核心目标：

1. 固定 RacingGO QA 全流程，减少 Agent 临场决策。
2. 每个步骤输出结构化结果，Hub 根据门禁自动决定继续、阻断、重试或人工确认。
3. 支持中断后从指定节点恢复。
4. 所有证据自动归档到同一个 workflow run。
5. 外部写操作，如推群、触发蓝盾，必须有明确审批或预授权记录。

## MVP 范围

第一版不需要做成完整 n8n，只做面向 QA 自动化的 Runbook 编排。

### 1. Workflow 定义

支持用 JSON/YAML 定义有向流程：

```yaml
id: racinggo_qa_auto_test
name: RacingGO qa_auto_test 全流程
steps:
  - id: precheck
    name: 前置检查
    runner: deepflow.racinggo.precheck

  - id: merge_dev
    name: 合入 origin/dev
    runner: deepflow.racinggo.merge_dev
    depends_on: [precheck]

  - id: editor_health
    name: Unity Health
    runner: deepflow.unity.health
    depends_on: [merge_dev]

  - id: lib20
    name: 库20合线门禁
    runner: deepflow.racinggo.editor_lib20
    depends_on: [editor_health]

  - id: lib21
    name: 库21关卡矩阵
    runner: deepflow.racinggo.editor_lib21
    depends_on: [lib20]

  - id: endless
    name: 无尽专项
    runner: deepflow.racinggo.endless
    depends_on: [lib21]

  - id: editor_report
    name: 生成 Hub Editor 总报告
    runner: deepflow.hub.editor_report
    depends_on: [endless]

  - id: notify_editor
    name: 推送 Editor 报告
    runner: deepflow.wecom.release_group
    approval_required: true
    depends_on: [editor_report]

  - id: push_and_build
    name: Push 并触发蓝盾构包
    runner: deepflow.bkci.trigger_qa_build
    approval_required: true
    depends_on: [notify_editor]

  - id: mobile_smoke
    name: 手机包冒烟
    runner: deepflow.racinggo.mobile_smoke
    depends_on: [push_and_build]

  - id: final_report
    name: 手机端最终报告与推群
    runner: deepflow.hub.mobile_final_report
    depends_on: [mobile_smoke]
```

### 2. Workflow Run 状态机

每次执行生成一个 `workflow_run`。

Workflow Run 状态：

- `pending`
- `running`
- `waiting_approval`
- `blocked`
- `failed`
- `succeeded`
- `cancelled`

Step 状态：

- `pending`
- `running`
- `retrying`
- `passed`
- `blocked`
- `failed`
- `skipped`
- `waiting_approval`

### 3. Step 输出协议

DeepFlow worker 每步必须回传标准 JSON：

```json
{
  "step_id": "lib21",
  "status": "passed",
  "summary": "19/19 PASS",
  "metrics": {
    "case_count": 19,
    "passed": 19,
    "failed": 0
  },
  "evidence": {
    "json_report": "F:/DeepFlow/data/reports/editor_lib14_suite_report.json",
    "html_report": "F:/DeepFlow/data/reports/editor_lib14_suite_report.html",
    "screenshots": [
      "F:/RacingGoUnity/UnityProj/Library/DeepFlowScreenshots/..."
    ]
  },
  "logs": {
    "terminal_log": "...",
    "error_excerpt": ""
  },
  "next_action": "continue"
}
```

失败或阻断时：

```json
{
  "step_id": "editor_report",
  "status": "blocked",
  "blocker": {
    "type": "missing_screenshots",
    "message": "报告中存在 41 个 missing screenshot card",
    "suggested_action": "fallback to before_settle_return/game_over screenshots and update report"
  },
  "next_action": "manual_review"
}
```

### 4. 门禁规则

Hub 支持每个 step 配置 gates：

```yaml
gates:
  - expression: "metrics.failed == 0"
    on_fail: blocked
  - expression: "report.missing_screenshot_count == 0"
    on_fail: blocked
  - expression: "git.remote_head == git.local_head"
    on_fail: blocked
```

RacingGO 首批内置门禁：

- Unity health 必须 `green=7 red=0`。
- 库20 必须 `6/6 PASS`。
- 库21 必须 `19/19 PASS`。
- 无尽必须满足：
  - 买油 `3/3`
  - `ghost_bad=0`
  - 有结算截图
  - 有返回大厅证据
- Hub 报告必须：
  - 无 missing screenshot
  - 链接可分享
  - 回读成功
- push 后必须：
  - `origin/qa_auto_test == local HEAD`
- 外部通知/构包必须：
  - 有审批记录或预授权记录。

### 5. 人工审批节点

Hub 页面提供按钮：

- 继续
- 重试当前步骤
- 从某步骤恢复
- 跳过并记录原因
- 终止流程
- 批准推群
- 批准触发蓝盾构包

审批记录需要保存：

```json
{
  "approval_id": 123,
  "workflow_run_id": 456,
  "step_id": "push_and_build",
  "action": "approve",
  "approver": "rajqiu",
  "approved_at": "2026-07-01T23:40:00+08:00",
  "comment": "Editor 全绿，允许构包"
}
```

### 6. Worker 接入

Hub 不直接跑 Unity/ADB。Hub 只派发任务，DeepFlow 本地 worker 执行。

建议 API：

- `POST /api/v1/workflow-runs`
  - 创建运行。
- `POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/dispatch`
  - 派发节点给 worker。
- `POST /api/v1/workflow-runs/{run_id}/steps/{step_id}/result`
  - worker 回写结果。
- `POST /api/v1/workflow-runs/{run_id}/approvals/{approval_id}/approve`
  - 审批通过。
- `POST /api/v1/workflow-runs/{run_id}/resume`
  - 从失败或暂停节点恢复。

## UI 需求

Workflow Run 页面展示：

- 当前进度条：例如 `merge_dev -> health -> lib20 -> lib21 -> endless -> report`
- 每步状态、耗时、执行机器、runner 名称
- 每步输出摘要
- 证据链接：JSON、HTML、截图、日志、Hub 报告、蓝盾构建、APK
- 当前阻断原因
- 可执行操作按钮：重试、继续、审批、终止
- 最终一键生成总结报告

## 首个落地场景

优先实现 `RacingGO qa_auto_test 全流程`：

1. 合入 `origin/dev`
2. Unity health
3. 库20
4. 库21
5. 无尽专项
6. Hub Editor 总报告
7. 推第二群
8. push `qa_auto_test`
9. 蓝盾构包
10. APK 制品获取
11. 真机手机包冒烟
12. 手机最终报告与推群

## 验收标准

MVP 验收：

- 能在 Hub 创建一次 workflow run。
- DeepFlow worker 能执行至少 `precheck/health/lib20/lib21/endless/report` 六个节点。
- 任一步失败后 Hub 能显示 blocker，并支持从该步重试。
- Hub 报告截图缺失时能自动阻断，不允许推群或构包。
- 推群和蓝盾构包必须经过审批节点。
- workflow run 最终能汇总所有证据，生成可分享报告。

## 非目标

第一版不做：

- 通用 n8n 级可视化拖拽。
- 任意第三方插件市场。
- 云端直接执行 Unity/ADB。
- 让 LLM 自由生成 shell 命令执行完整流程。

第一版目标是把高频 QA 自动化流程产品化，稳定跑通 RacingGO 这条链路。
