# Hub Codex 多 Flow：P0-H Capability Gap 生命周期开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

P0-H 复用 P0-B 已有的 `capability_gaps` 和候选关联，不创建第二套能力缺口系统。原有资格验证在返回 `AUTOMATION_CAPABILITY_GAP` 时仍将候选置为 `WAITING_CAPABILITY`；本阶段补齐缺口登记、开发需求关联、交付状态、解决审计和候选重新排队。

核心约束保持不变：

- 能力不足的候选不能进入正式用例库；
- Gap 标记 resolved 不等于候选验证通过；
- resolved 后仅将候选重新放入 `READY_FOR_CANARY`，必须重新执行资格验证；
- 不修改 Workflow Definition #12，不阻断现有 Flow12。

本阶段延伸覆盖 AT-03。

## 2. 数据结构

扩展现有 `capability_gaps`：

- `required_operations_json`
- `required_observables_json`
- `required_reset_hooks_json`
- `owner`
- `development_requirement_ref`
- `development_requirement_url`
- `development_requirement_status`
- `resolution_json`
- `resolved_by` / `resolved_at`

保留已有字段：

- `missing_capabilities_json`
- `evidence_json`
- `status`
- `candidate_requeue_pending`
- `version`

新增 `capability_gap_events` 追加式审计表，记录事件类型、前后状态、前后版本、actor、request ID、幂等键、请求 hash 和事件负载。

Gap 状态机：

```text
open -> in_progress -> resolved
  ^                       |
  +--------- reopen ------+
```

资格验证再次发现同一个已经 resolved 的 Gap 时，会自动清除旧 resolution 并重新回到 `open`，同时记录 `reopened_by_qualification` 事件。

## 3. API

### 3.1 幂等登记和更新

`POST /api/v1/capability-gaps:upsert`

```json
{
  "project_id": 6,
  "gap_key": "racinggo:reward-observability",
  "title": "奖励状态不可观测",
  "missing_capabilities": ["read_reward_state"],
  "required_operations": ["claim_once"],
  "required_observables": ["reward_state", "currency_balance"],
  "required_reset_hooks": ["restore_test_account"],
  "evidence": {
    "qualification_run_id": 118
  },
  "owner": "automation-platform",
  "development_requirement": {
    "ref": "TAPD-STORY-9001",
    "url": "https://tapd.example/story/9001",
    "status": "planning"
  }
}
```

要求：

- 必须携带 `Idempotency-Key` 或同名 body 字段；
- `gap_key` 未提供时，根据项目和缺失能力集合生成稳定 hash；
- 创建时至少提供 capability、operation、observable 或 reset hook 中的一项；
- 更新已有 Gap 必须提供 `expected_version`；
- 相同幂等键和相同请求返回原响应，不重复加版本或事件；
- 相同幂等键用于不同请求返回 409。

### 3.2 查询

- `GET /api/v1/capability-gaps/{id}`
- `GET /api/v1/capability-gaps?project_id=6&status=resolved&candidate_requeue_pending=true`

列表支持：

- 多状态过滤；
- `candidate_requeue_pending`；
- `development_requirement_ref`；
- `owner`；
- `updated_after`；
- `page` / `page_size`，每页最大 200。

详情返回关联候选 ID、候选状态计数和完整事件时间线。

### 3.3 开始交付

`POST /api/v1/capability-gaps/{id}/start`

```json
{
  "expected_version": 1,
  "owner": "automation-platform",
  "development_requirement": {
    "ref": "TAPD-STORY-9001",
    "url": "https://tapd.example/story/9001",
    "status": "developing"
  }
}
```

只允许 `open -> in_progress`。

### 3.4 标记解决

`POST /api/v1/capability-gaps/{id}/resolve`

```json
{
  "expected_version": 2,
  "resolution": {
    "summary": "奖励读取器和测试账号恢复钩子已交付",
    "verification_run_id": 201
  },
  "development_requirement": {
    "ref": "TAPD-STORY-9001",
    "url": "https://tapd.example/story/9001",
    "status": "done"
  }
}
```

resolve 必须有 resolution 摘要或证据。若仍有关联候选处于 `WAITING_CAPABILITY`，服务端将 `candidate_requeue_pending` 标为 true，供控制器增量查询。

### 3.5 候选重新排队

`POST /api/v1/capability-gaps/{id}/requeue-candidates`

```json
{
  "expected_version": 3,
  "candidate_ids": [1024, 1025]
}
```

规则：

- 只有 resolved Gap 可以重排；
- 不传 `candidate_ids` 时处理该 Gap 下全部候选；
- 仅 `WAITING_CAPABILITY` 候选会变为 `READY_FOR_CANARY`；
- 清除旧 qualification outcome、Run 和证据，避免被误认成已重新验证；
- 保留 `capability_gap_id` 和历史实体关系用于追溯；
- 每个候选独立增加 version 并记录 `capability_gap_requeued` 事件；
- 部分重排后仍有等待候选时，`candidate_requeue_pending` 保持 true；
- 已晋级、已退休或其他状态的候选不会被倒退，响应放入 `skipped_candidates`。

### 3.6 重新打开

`POST /api/v1/capability-gaps/{id}/reopen`

只允许 `resolved -> open`，必须提供 reason。该操作不会强行改变已重排候选的状态；候选后续资格验证仍按正常流程执行。

所有状态写接口都要求 `expected_version` 和幂等键。

## 4. 资格验证接入

原 `/automation-case-candidates/{id}/qualification-result` 保持兼容，并新增可选字段：

- `required_operations`
- `required_observables`
- `required_reset_hooks`
- `development_requirement`

发现同一 `gap_key` 时复用已有 Gap。新的观察证据会更新 Gap；已解决 Gap 再次被验证命中时自动重开。

候选与 Gap 继续记录 `blocked_by`；重排时新增 `unblocked_by`。Gap 与开发需求通过 P0-F 记录 `tracked_by`，需求 ref 可以使用 Hub Requirement ID、TAPD Story ID 或控制器可识别的外部稳定 ID。

## 5. 并发与安全

- Gap 状态变更使用数据库行锁和 version 校验；
- 关联候选重排时同时锁定候选行；
- 任一 version 过期返回 409，不覆盖新数据；
- 选择了不属于该 Gap 的 candidate ID 时整批拒绝；
- 重排事务内要么全部选中候选和 Gap 状态一起提交，要么全部回滚；
- 项目权限沿用候选中心的用户/Agent 项目隔离；
- promotion 的 `QUALIFIED` 前置条件没有放宽。

## 6. 部署顺序

1. 备份数据库；
2. 确认 P0-B `20260812_automation_case_candidates.sql` 已执行；
3. 执行 `ops/migrations/20260812_capability_gap_lifecycle.sql`；
4. 部署 Hub 应用代码；
5. 对已有 open Gap 调用详情，确认旧数据可正常读取；
6. 新建测试 Gap，验证 upsert 重放不增加 version；
7. 验证 `start -> resolve -> requeue-candidates`；
8. 确认候选回到 `READY_FOR_CANARY` 而不是 `QUALIFIED`；
9. 重新执行资格验证后再走 promotion。

迁移只扩展已有表并新增审计表，不改候选表结构。应用回退时可保留新增列；旧代码会忽略它们。回退后不要调用新生命周期 API。

## 7. 自动化验证

`tests/test_capability_gap_lifecycle_api.py` 覆盖：

- 幂等 upsert、版本更新、列表过滤和开发需求关系；
- `open -> in_progress -> resolved`；
- stale expected version 返回 409；
- resolved 后进入待重排查询；
- 原子重排和幂等重放；
- 部分重排保持 pending，全部完成后清除 pending；
- 重排候选回到 `READY_FOR_CANARY` 并清除旧资格结论；
- 手动 reopen 幂等；
- 新资格验证命中已解决 Gap 时自动 reopen。

同时回归：

- AT-03 能力不足候选不产生正式用例；
- P0-C promotion 拒绝 `WAITING_CAPABILITY`；
- P0-F 关系追溯；
- P0-G 跑后证据语义。

## 8. 下一阶段建议

P0-I 建议实现 Finding 统一反馈标签和分析规则版本/历史回放：反馈标签先沉淀 `true_positive`、`false_positive` 等事实，规则必须经过 replay 并达到门槛后才能从 `draft` 进入 `active`，不能直接覆盖线上规则。
