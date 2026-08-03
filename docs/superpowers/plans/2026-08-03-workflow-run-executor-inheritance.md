# Workflow Run Executor Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让没有显式目标的 `worker_task` 继承本次 Workflow Run 的 `executor_claw_ids`，并允许这些执行者拉取、认领、上报进度、结果及状态。

**Architecture:** Run 级执行者继续以 `run.context_json.workflow_start.executor_claw_ids` 为唯一来源。节点显式 `target_claw_id` 或 `step_config_json.executor_claw_ids` 优先；仅无显式目标的 `worker_task` 动态回退到 Run 执行者，避免把相同权限复制到每个 Step。派发通知和所有节点权限检查统一复用 `_step_allowed_claw_ids`。

**Tech Stack:** Python、Flask、SQLAlchemy、unittest/pytest。

## Global Constraints

- 不改变 `agent_task` 的现有目标解析行为。
- 显式节点目标必须优先于 Run 级执行者。
- 没有 Run 执行者时保留 Workflow owner 回退。
- 不新增数据库字段或迁移。

---

### Task 1: Run 执行者继承与派发

**Files:**
- Modify: `web/app/api/workflows.py`
- Test: `tests/test_workflows_service.py`

**Interfaces:**
- Consumes: `WorkflowRun.context_json["workflow_start"]["executor_claw_ids"]`
- Produces: `_step_allowed_claw_ids(step) -> list[int]`
- Produces: `_dispatch_step_message(step)` 向所有允许执行者创建 `ClawMessage`

- [ ] **Step 1: 写失败测试**

覆盖：

```python
def test_worker_task_inherits_run_executor_claw_ids():
    step = worker_step(
        context={'workflow_start': {'executor_claw_ids': [7, 11]}})
    assert workflow_api._step_allowed_claw_ids(step) == [7, 11]

def test_explicit_worker_target_overrides_run_executors():
    step = worker_step(
        target_claw_id=9,
        context={'workflow_start': {'executor_claw_ids': [7, 11]}})
    assert workflow_api._step_allowed_claw_ids(step) == [9]
```

并验证无目标 `worker_task` 的通知发送给全部 Run 执行者。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_workflows_service.py -q
```

Expected: Run 执行者继承断言失败，当前返回空列表或 owner。

- [ ] **Step 3: 最小实现**

在 `web/app/api/workflows.py`：

```python
def _run_executor_claw_ids(step):
    if not step or step.step_type != 'worker_task' or not step.run:
        return []
    context = step.run.context_json if isinstance(step.run.context_json, dict) else {}
    workflow_start = (
        context.get('workflow_start')
        if isinstance(context.get('workflow_start'), dict)
        else {}
    )
    return normalized_integer_ids(workflow_start.get('executor_claw_ids'))
```

`_step_allowed_claw_ids` 按「显式 target → step executor 列表 → Run executor 列表」返回。`_dispatch_step_message` 对返回的全部执行者逐个创建消息；若为空才保留 target post/owner 回退。

- [ ] **Step 4: 运行测试并确认通过**

Run:

```powershell
python -m pytest tests/test_workflows_service.py -q
```

Expected: PASS。

### Task 2: API 鉴权回归

**Files:**
- Modify: `tests/test_workflows_service.py`
- Modify: `docs/经验记录.md`

**Interfaces:**
- Consumes: `_step_belongs_to_claw(step, claw_id)`
- Consumes: `_can_change_workflow_step_status(run, step)`
- Produces: claim/heartbeat/progress/result/status 使用同一执行者集合

- [ ] **Step 1: 补充权限边界测试**

验证：

```text
Run executor 可以操作无目标 worker_task
非 Run executor 被拒绝
显式 target 存在时 Run executor 不自动获得权限
agent_task 不继承 worker_task 的 Run 回退规则
```

- [ ] **Step 2: 运行 Workflow 相关回归**

Run:

```powershell
python -m pytest tests/test_workflows_service.py tests/test_workflow_worker.py tests/test_task_context.py -q
```

Expected: PASS。

- [ ] **Step 3: 记录经验**

在 `docs/经验记录.md` 记录：Run 创建时已保存执行者，但节点鉴权只读 Step 配置会造成「能启动、不能执行」；派发与 claim/result/status 必须共享同一权限解析函数。
