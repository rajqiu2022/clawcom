# 测试经理临时转正 / 回退（Manager Delegation）

## 1. 为什么不能只改 `primary_manager_claw_id`

在位的**主测试经理**身份被计划监督（Plan Supervision）在两处硬绑定：

| 绑定 | 位置 | 只改 `primary` 的后果 |
|---|---|---|
| 有效主经理判定 | `team_binding_valid()` / `ensure_manager_tenure()` 要求 `team.primary_manager_claw_id == sup.orchestrator_claw_id` | 换 primary 后原 `orchestrator_claw_id` 不再是主经理 → `PLAN_TEAM_NOT_ENABLED`，计划监督整体失效 |
| 监督身份指纹 | `PlanSupervisor.start_hash = sha256({plan_id, team_id, orchestrator_claw_id})`，`bootstrap()` 对不上即 `PLAN_ALREADY_SUPERVISED` | 监督工作项无法重新 bootstrap，也无法换绑 |
| 派工鉴权 | `dispatch_test_task()` 要求 `team.primary == 调用者 == sup.orchestrator_claw_id` | 两个方向都不会自动生效，403 变成永久的 |

结论：**转正是一次"身份换绑"，必须原子完成**，并且要能精确回退。

## 2. 开关

| 层 | 名称 | 说明 |
|---|---|---|
| 能力开关 | `PLAN_MANAGER_DELEGATION_ENABLED` | 缺省继承 `AGENT_TEAMS_ENABLED`，已灰度团队的环境无需新增环境变量；设为 `0` 可冻结该能力（接口返回 `TEAM_DELEGATION_DISABLED` 404） |
| 运行时开关 | `POST/DELETE /api/v1/agent-teams/{team_id}/manager-delegation` | 转正 / 回退，走一次事务 |

## 3. 语义

转正（delegate）一次事务内完成：

1. `team.primary_manager_claw_id` ← 被委派人，`team.backup_manager_claw_id` ← 原主经理
   （因此**在回退前，原主经理就是替补测试经理**）；
2. `team.version += 1`、`manager_epoch += 1`、`manager_session_id` 重建、`active_manager_claw_id` 指向新经理；
3. 该团队下**每一个** `PlanSupervisor`：`orchestrator_claw_id` 换绑、`start_hash` 重算、`fencing_token += 1`、旧租约清空、旧唤醒作废、`status → retryable`、`next_check_at = now`；
4. 对应 `WorkflowMission.main_claw_id` 换绑（否则下次 `ensure_team_mission()` 直接 `PLAN_MISSION_CONFLICT`）；
5. `AgentTeamMission.snapshot_json` 写入新主备 + `manager_delegation` 溯源块；
6. 写 `AuditLog`（`resource_type=agent_team`, `action=manager_delegation`）与每个计划的 `PlanSupervisorEvent(kind='manager_delegation')`。

请求成功返回后，`api_bp.after_request` 的监督 drain 会立即为**新经理**排一条 `plan_supervision` 唤醒，原经理的旧唤醒被置 `failed`——不会留双头。

回退（revoke）是对称操作：恢复 `previous_primary_claw_id` 为主经理，被委派人回替补位；同样重算 `start_hash` 与 fence，唤醒回到原主经理。原记录归档进 `history`（保留最近 5 次），`active=false`。

## 4. 前置条件与错误码

| 约束 | 错误码 |
|---|---|
| 被委派人**必须**是当前 `backup_manager_claw_id` | `TEAM_DELEGATION_TARGET_INVALID` 400 |
| 目标已是主经理 / 非整数 | `TEAM_DELEGATION_TARGET_INVALID` / `TEAM_VALIDATION_FAILED` 400 |
| 已有生效中的转正（须先回退） | `TEAM_DELEGATION_ALREADY_ACTIVE` 409 |
| 无生效中的转正却回退 | `TEAM_DELEGATION_NOT_ACTIVE` 409 |
| 团队主经理已被第三方改动 | `TEAM_DELEGATION_STATE_CONFLICT` 409 |
| `expected_version` 不匹配 | `TEAM_VERSION_CONFLICT` 409 |
| `expires_at` 非法或已过期 | `TEAM_DELEGATION_DEADLINE_INVALID` 400 |
| 能力开关关闭 | `TEAM_DELEGATION_DISABLED` 404 |
| 非项目管理员 | `TEAM_ADMIN_REQUIRED` 403 |

**"必须是当前替补"这条约束是刻意的**：它保证换位是无损的（不会有第三个 Agent 丢掉主备槽位），也让回退成为精确逆操作。

`expires_at` 只是**记录截止时间**，不做后台自动改写；`GET` 会回报 `expired=true`，可用 `--sweep-expired` 显式批量回退。

## 5. 接口

```
GET    /api/v1/agent-teams/{team_id}/manager-delegation          # 任意项目成员可读
POST   /api/v1/agent-teams/{team_id}/manager-delegation          # 项目管理员：转正 → 201
DELETE /api/v1/agent-teams/{team_id}/manager-delegation          # 项目管理员：回退 → 200
POST   /api/v1/agent-teams/{team_id}/manager-delegation/revoke   # 等价于 DELETE（部分客户端不带 body）
```

转正 body：`delegate_claw_id`（必填）、`expected_version`、`reason`、`expires_at`（ISO 8601，可空）。
回退 body：`expected_version`、`reason`。

返回：`{delegation, team, supervisors[]}`，`supervisors[]` 是每个被换绑计划的证据（`from/to/fencing_token/start_hash_changed/next_check_at`）。团队读回（`GET /agent-teams/{id}`）也带 `manager_delegation` 摘要。

## 6. 运维命令（Hub 主机）

`ops/manager_delegation.py`：默认只读预演，写操作必须 `--apply`。

```bash
cd /opt/openclaw-web && SKIP_AUTO_MIGRATE=1 ./venv/bin/python ops/manager_delegation.py --team-id 1 --status
SKIP_AUTO_MIGRATE=1 ./venv/bin/python ops/manager_delegation.py --migrate --apply
SKIP_AUTO_MIGRATE=1 ./venv/bin/python ops/manager_delegation.py --team-id 1 \
    --delegate-claw-id 61 --reason "在位经理 Provider 额度耗尽" --apply
SKIP_AUTO_MIGRATE=1 ./venv/bin/python ops/manager_delegation.py --team-id 1 --revoke --apply
SKIP_AUTO_MIGRATE=1 ./venv/bin/python ops/manager_delegation.py --sweep-expired --apply
```

输出统一以 `MANAGER_DELEGATION {json}` 单行呈现，便于脚本回收。

## 7. 回退本次改动（代码级）

DB 侧只新增一列 `agent_teams.manager_delegation_json`，为纯增量；`AuditLog` / `PlanSupervisorEvent` 的历史记录不受影响。若要停用能力：置 `PLAN_MANAGER_DELEGATION_ENABLED=0`，再执行一次 `--revoke --apply` 把主经理换回原主经理即可。
