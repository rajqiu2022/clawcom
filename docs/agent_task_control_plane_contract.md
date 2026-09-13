# 普通 AgentTask 控制面合同

本文描述 Hub 管理类任务（`task_type != workflow_agent_task`）的领取、心跳、回执和部署身份合同。Workflow AgentTask 继续使用 Workflow Run/Step 自身的 claim、heartbeat、result 接口，两套状态机不得混用。

## 生命周期

1. Hub 创建任务时校验 payload；部署类任务校验失败直接返回 HTTP 422，不创建任务。
2. Worker 从 SSE 或 `GET /api/openclaws/{claw_id}/pending-tasks` 领取任务。领取是条件更新，只有一个调用方能得到同一任务。
3. Hub 返回 `claim_token`、`attempt_no`、`fencing_token` 和 `lease_expires_at`。`claim_token` 仅在领取响应下发，不在管理查询 API 返回。
4. Worker 在租约到期前调用 heartbeat 续租。任务完成后，使用同一组领取凭据提交结构化结果。
5. 租约过期后，旧凭据的 heartbeat/result 返回 HTTP 409；后台扫描器按 `retry_max` 有限重试，预算耗尽后进入 `failed / agent_task_lease_expired`。
6. 管理员取消或重试会递增 fencing token。旧执行者的迟到结果只能产生审计记录，不能覆盖当前状态。

## Worker 请求

Heartbeat：

```http
POST /api/openclaws/{claw_id}/tasks/{task_id}/heartbeat
Authorization: Bearer {claw_token}
Content-Type: application/json

{
  "claim_token": "仅使用领取响应中的值",
  "attempt_no": 1,
  "fencing_token": 1,
  "lease_seconds": 300,
  "progress": {
    "phase": "validating",
    "summary": "正在校验部署目标"
  }
}
```

Result（`/report` 与 `/poll` 同合同）：

```json
{
  "task_id": "task_...",
  "claim_token": "仅使用领取响应中的值",
  "attempt_no": 1,
  "fencing_token": 1,
  "status": "blocked",
  "error_code": "EXTERNAL_GATE",
  "reason": "等待负责人确认",
  "retryable": false,
  "evidence": {
    "ticket": 7
  }
}
```

终态只接受 `completed`、`failed`、`blocked`。`failed + retryable=true` 仅在未耗尽 `retry_max` 时重新排队。完全相同的 progress 只续租，不重复写入进度事实。

## 部署任务

部署形态 payload 必须显式包含 `deployment_kind`：

- `hub_application`：必须绑定允许的 Hub 仓库、合法分支和完整 40 位 Hub commit；不得携带 Worker Release ID。
- `worker_release`：`release_id` 必须等于 `worker-{source_commit}`，平台、manifest SHA、artifact SHA 必须与 Hub 中已批准的不可变 Worker Release 记录完全一致。
- `agent_instance`：只表示目标 Agent 实例操作，不得混入 Hub 源码或 Worker Release 字段，`target_claw_id` 必须与派发 URL 一致。

Hub commit 不能冒充 Worker Release。旧部署任务若没有 `deployment_kind`，领取或后台扫描时会 fail-closed 为 `invalid_deployment_contract`。

## 管理与观测

管理员可在 Claw 详情页的“Agent 任务”页签查看任务年龄、心跳年龄、租约、attempt、fencing、重试次数、终态原因和去重后的最新进度，并执行取消或人工重试。

对应 API：

- `GET /api/openclaws/{claw_id}/tasks`
- `GET /api/openclaws/{claw_id}/tasks/{task_id}`
- `POST /api/openclaws/{claw_id}/tasks/{task_id}/cancel`
- `POST /api/openclaws/{claw_id}/tasks/{task_id}/retry`

取消、重试、租约过期、非法部署合同和迟到结果均保留审计记录。
