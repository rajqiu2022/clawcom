# Workflow Mission：主 Agent 自主调度

## 适用范围

Mission 适合后续 Flow 无法在任务开始时完全确定的目标。主 Agent根据每个 Child Run 的真实结果选择下一条 Flow；Hub 只维护授权范围、身份、幂等、预算、状态和审计。

若 A/B/C 的依赖固定，继续放在一个 Workflow Definition 中。不要为了使用 Mission 而把稳定节点拆成多个 Flow。

## API 一览

```http
POST /api/v1/workflow-missions
GET  /api/v1/workflow-missions
GET  /api/v1/workflow-missions/{MISSION_ID}
GET  /api/v1/workflow-missions/{MISSION_ID}/definitions
POST /api/v1/workflow-missions/{MISSION_ID}/dispatch
POST /api/v1/workflow-missions/{MISSION_ID}/complete
POST /api/v1/workflow-missions/{MISSION_ID}/cancel
```

## 创建 Mission

```http
POST /api/v1/workflow-missions

{
  "project_id": 6,
  "main_claw_id": 11,
  "objective": "自主完成代码分析、用例设计和验证",
  "max_child_runs": 20,
  "max_retries_per_flow": 3,
  "expires_in_hours": 8,
  "allow_external_notification": false,
  "allow_destructive_actions": false,
  "context": {
    "requirement_id": 123
  }
}
```

权限与范围：

- 管理员/用户只能为自己有权绑定的项目主 Agent 创建；Claw 只能为自己创建。
- 未传 `allowed_definition_ids` 时，Hub 快照创建者当时有权执行的项目内全部 active Flow。
- 传 `allowed_definition_ids` 可进一步收窄；`denied_definition_ids` 始终优先排除。
- Mission 是一次性运行级授权，不代表获得新的项目、通知、凭据或破坏性操作权限。
- `max_child_runs` 范围为 1–100，`expires_in_hours` 范围为 1–168。

响应中的两个地址应作为后续调用依据：

```json
{
  "definitions_api": "/api/v1/workflow-missions/91/definitions",
  "dispatch_api": "/api/v1/workflow-missions/91/dispatch"
}
```

## 查询可用 Flow

```http
GET /api/v1/workflow-missions/{MISSION_ID}/definitions
```

主 Agent 每次准备决策时先查询这个接口。返回项目范围内当前仍可调度的 Flow 摘要，包括：

- `workflow_definition_id`
- `workflow_key`
- `name` / `description`
- `version`
- `step_count`
- `agent_step_count` / `worker_step_count`

不要把历史记忆中的 Definition ID 当作当前事实。

## Dispatch Child Run

最小请求：

```http
POST /api/v1/workflow-missions/{MISSION_ID}/dispatch

{
  "workflow_definition_id": 37
}
```

推荐请求：

```json
{
  "workflow_definition_id": 37,
  "start_vars": {
    "discovery_report_id": 123
  },
  "context": {
    "handoff_summary": "登录态恢复是当前最高风险"
  },
  "reason": "分析已完成，下一步设计候选用例",
  "decision_key": "case-design-stage-1",
  "run_name": "Mission 91 - 用例设计"
}
```

规则：

- `workflow_definition_id` 必填。
- `decision_key` 建议使用稳定的“业务阶段 + 尝试序号”；也可显式传 `idempotency_key`。
- 相同 key、相同请求重放返回 200 和原 Child Run；相同 key、不同请求返回 `MISSION_IDEMPOTENCY_CONFLICT`。
- 未传 key 时 Hub 按规范化请求生成自动幂等键。
- 禁止传 `executor_claw_ids`、`target_claw_ids`、`selected_claw_ids`、`worker_claw_id` 等执行者覆盖字段。
- dispatch 成功只表示 Child Run 创建成功，不代表业务完成。

Codex Worker 经 `hub_api` 调用时使用稳定 `operation_id`。Mission dispatch 不要求 `verify.expected`：

```json
{
  "method": "POST",
  "path": "/api/v1/workflow-missions/91/dispatch",
  "operation_id": "mission-91-case-design-stage-1",
  "body": {
    "workflow_definition_id": 37,
    "decision_key": "case-design-stage-1"
  }
}
```

## 决策循环

每次只做当前证据足以支持的下一步决策：

1. 查询 Mission，确认 `effective_status=active`、剩余 Child Run 预算和到期时间。
2. 查询 `definitions_api`，读取当前可选 Flow。
3. 回读上一 Child Run 的 `readback_url`；结合 status、step results、outputs、references 和 evidence 判断结果。
4. 若上一 Child Run 为 `blocked/failed`，修复后仍需运行同一个 Definition，调用响应中的 `restart_api` 原地完整重启，携带稳定 `Idempotency-Key`；不要再次 dispatch。
5. 只有首次启动或明确切换到不同 Flow 时，才选择 Definition、写简短 `reason` 并使用稳定 `decision_key` dispatch。
6. Child Run 未到终态时等待或查询，不要因网络重试重复创建。
7. 目标完成后调用 Mission `complete`；放弃目标时调用 `cancel`。

原地重启示例：

```json
{
  "method": "POST",
  "path": "/api/v1/workflow-runs/335/restart",
  "operation_id": "mission-91-run-335-restart-1",
  "body": {"reason": "验证收据已按 Adapter 1.1.30 合同补齐"}
}
```

Worker Hub Proxy 会根据 `operation_id` 自动生成 `Idempotency-Key`。重启成功时响应仍是同一个 `run_id`，`restart.same_run_id=true`，不会增加 Mission `child_run_count`。

内部分析或正常 Flow 选择不需要新增人工审批。只有实际进入外部通知、临时凭据、push、构包或其他破坏性动作时，才按对应 Flow 和平台已有门禁处理。

## 完成与取消

```http
POST /api/v1/workflow-missions/{MISSION_ID}/complete
POST /api/v1/workflow-missions/{MISSION_ID}/cancel
```

结束 Mission 只禁止继续创建 Child Run，不会隐式取消已经创建的 Run。如需取消某个 Child Run，仍调用该 Run 自己的 cancel API。

## 常见错误

| code | 含义与处理 |
|---|---|
| `MISSION_MAIN_AGENT_REQUIRED` | 当前调用者不是 Claw；必须由 Mission 主 Agent dispatch |
| `MISSION_ACCESS_DENIED` | 不是该 Mission 主 Agent或无读取权限 |
| `MISSION_DEFINITION_NOT_ALLOWED` | Flow 不在 Mission 的项目授权快照内；重新查询 definitions |
| `MISSION_EXECUTOR_OVERRIDE_FORBIDDEN` | 删除请求中的执行者覆盖字段 |
| `MISSION_IDEMPOTENCY_CONFLICT` | 同一个 key 被用于不同决策；修正 key 或请求内容 |
| `MISSION_CHILD_RUN_BUDGET_EXHAUSTED` | Child Run 总预算耗尽；停止调度并向 owner 汇报 |
| `MISSION_EXPIRED` | Mission 已过期；不能继续 dispatch |
| `MISSION_NOT_ACTIVE` | Mission 已完成或取消；不能继续 dispatch |

如果历史 Child Run 已被删除，幂等重放可能返回 `run_deleted=true`。这表示审计事实仍在，不应偷偷重建同一决策；由 owner 决定是否以新的 decision key 重新运行。
