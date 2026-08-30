# Hub × Codex 多 Flow P0-B：候选用例中心开发与迁移说明

## 1. 本阶段范围

本阶段新增独立候选用例生命周期，不修改正式用例表的现有 CRUD，也不会自动向用例库 #33 写入数据：

- 候选用例、追加式事件历史和服务端状态机。
- 幂等 upsert、条件查询、详情和 `expected_version` 乐观锁更新。
- 编辑器资格验证结果回写。
- `AUTOMATION_CAPABILITY_GAP` 对应的最小 Capability Gap 对象和关联。
- 来源引用、资格验证 Run、能力缺口、正式库和正式用例的结构化字段。

正式入库仍由下一阶段的 promotion 事务接口完成。普通候选 API 无权把状态改成 `ACTIVE`。

## 2. 数据库迁移

执行：

```text
ops/migrations/20260812_automation_case_candidates.sql
```

新增表：

- `automation_case_candidates`
- `automation_case_candidate_events`
- `capability_gaps`：P0 最小对象，P1 再补充能力目录和开发需求工作流。

关键约束：

- `(project_id, dedupe_key)` 唯一，防止重复候选。
- 候选 `version` 从 1 开始，每次修改递增。
- 事件表追加写，保存前后状态、前后版本、Actor、Request ID 和差异摘要。
- 能力缺口按 `(project_id, gap_key)` 去重。

## 3. API

### 3.1 幂等创建

```text
POST /api/v1/automation-case-candidates:upsert
```

请求必须包含 `project_id`、`title`，并提供稳定 `dedupe_key` 或 `idempotency_key`。完全重放返回同一个候选且不重复创建事件。

新候选只允许从以下状态开始：

- `DISCOVERED`
- `DESIGNED`

`DESIGNED` 必须至少包含可执行步骤和业务状态校验，单纯“页面可打开”不能作为完整设计。

### 3.2 查询与详情

```text
GET /api/v1/automation-case-candidates?project_id=6&state=READY_FOR_CANARY&page=1&page_size=50
GET /api/v1/automation-case-candidates/{id}
```

列表统一返回 `{items,total,page,page_size,pages}`。详情同时返回事件时间线、来源链路和下游关联。

### 3.3 更新

```text
PATCH /api/v1/automation-case-candidates/{id}
```

必须传 `expected_version`。数据库更新条件同时包含 ID 和当前版本，因此两个客户端使用同一旧版本并发更新时只有一个成功，另一个返回：

```text
409 CANDIDATE_VERSION_CONFLICT
```

### 3.4 资格验证

```text
POST /api/v1/automation-case-candidates/{id}/qualification-result
```

只接受处于 `READY_FOR_CANARY` 的候选，并且结果只能是：

- `QUALIFIED` → `QUALIFIED`
- `AUTOMATION_CAPABILITY_GAP` → `WAITING_CAPABILITY`
- `CASE_DESIGN_ERROR` → `CASE_DESIGN_ERROR`
- `PRODUCT_BUG_CANDIDATE` → `PRODUCT_BUG_CANDIDATE`

能力不足时必须提供 `missing_capabilities`，Hub 创建或复用最小 Gap，保存证据并关联候选。此操作不会创建正式 testcase。

资格结果支持 `idempotency_key`；重放返回同一版本且不重复创建事件或 Gap。

## 4. 状态机边界

主要正常路径：

```text
DISCOVERED → DESIGNED → READY_FOR_CANARY → QUALIFIED → PENDING_PUBLISH
```

异常和恢复路径包括：

- `WAITING_CAPABILITY → READY_FOR_CANARY`
- `ENV_BLOCKED → READY_FOR_CANARY`
- `CASE_DESIGN_ERROR → DESIGNED`
- `PRODUCT_BUG_CANDIDATE → DESIGNED`
- `QUARANTINED → READY_FOR_CANARY`

`ACTIVE` 只能由后续 promotion 服务在正式用例成功写入同一事务后设置。候选 PATCH 直接跳到 `ACTIVE` 会被拒绝。

## 5. 验证

自动化测试覆盖：

- upsert 重放不产生重复候选和事件。
- AT-02：相同 `expected_version` 的两个写入只有一个成功。
- 非法 `DESIGNED → ACTIVE` 跳转被服务端拒绝。
- AT-03 前半段：能力不足创建 Gap、候选等待能力、正式用例数不变。
- 资格结果幂等重放。
- 状态过滤、分页和详情事件时间线。

AT-03 的“调用 promotion 必须失败且库 revision 不变”将在 P0-C promotion 接口实现后补齐完整集成测试。

## 6. 回滚与兼容

新接口、新模型与现有测试计划、用例库和 Flow #12 没有调用依赖。回滚应用代码时可保留新表，不会影响旧业务；不要删除已经产生的候选审计数据。

## 7. 下一阶段

P0-C 实现正式用例库 `revision/content_hash`、候选 promotion 原子事务、dry-run、并发冲突和不可变发布记录，对应 AT-04、AT-05，并补齐 AT-03 的入库拒绝验证。
