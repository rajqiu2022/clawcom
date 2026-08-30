# Hub × Codex 多 Flow P0-A：Run 控制面开发与迁移说明

## 1. 本阶段范围

本阶段只实现需求文档第 14 节的第一项，不引入 Hub 自动编排，也不修改 Workflow Definition #12：

- Workflow Run 增加可空的幂等键、Controller ID、Correlation ID 和触发来源。
- `POST /api/v1/workflow-runs` 支持按 Workflow Definition 幂等创建。
- Run 列表支持 Definition、多个状态、Controller、Correlation、项目和时间过滤。
- 增加 `GET /api/v1/workflow-runs/latest`。
- Run 摘要直接返回当前步骤状态、结果摘要和产物引用。
- Workflow Definition 完整保留顶层 `start_vars_schema`。
- 模板保存时校验 gate 表达式；声明 `metrics_schema` 后校验精确指标路径。

## 2. 向后兼容策略

- 新数据库字段全部允许为空。
- 旧客户端继续使用 `definition_id`；新客户端也可使用 `workflow_definition_id`。
- 未传 `idempotency_key` 时，每次请求仍创建新的 Run。
- 未传分页参数时，Run 列表继续返回最多 100 条的裸数组。
- 传入 `page`、`page_size` 或旧参数 `per_page` 时，返回 `{items,total,page,page_size,pages}`。
- 未声明 `metrics_schema` 的历史模板只校验 gate 语法，不校验指标名称，避免旧模板无法保存。
- 不增加 Flow #12 的依赖、审批、门禁、候选队列或自动启动逻辑。

## 3. 数据库迁移

部署代码前先执行：

```text
ops/migrations/20260812_workflow_run_control_plane.sql
```

新增字段：

- `idempotency_key`
- `idempotency_request_hash`
- `controller_run_id`
- `correlation_id`
- `trigger_source`

唯一约束为 `(definition_id, idempotency_key)`。MySQL 对空值允许多行，因此旧创建路径不受影响。

## 4. API 约定

### 4.1 幂等创建

```json
{
  "workflow_definition_id": 12,
  "idempotency_key": "scheduler-20260812-flow12-window-2200",
  "controller_run_id": "codex-cycle-20260812-01",
  "correlation_id": "racinggo-dev2-abc123",
  "trigger_source": "codex_controller",
  "start_vars": {}
}
```

- 首次创建返回 `201` 和 `idempotent_replay=false`。
- 相同 Definition、幂等键及请求语义重放返回 `200`、同一 Run ID 和 `idempotent_replay=true`。
- 相同幂等键但请求语义不同返回 `409 IDEMPOTENCY_CONFLICT`。
- `Idempotency-Key` Header 与 Body 均支持；两者同时存在时必须一致。

### 4.2 查询

```text
GET /api/v1/workflow-runs?workflow_definition_id=12&status=running,waiting_approval&created_after=2026-08-12T00:00:00%2B08:00&controller_run_id=codex-cycle-20260812-01&page=1&page_size=50
GET /api/v1/workflow-runs/latest?correlation_id=racinggo-dev2-abc123
```

列表项和 latest 响应新增：

- `workflow_definition_id`
- `current_step_status`
- `controller_run_id`
- `correlation_id`
- `trigger_source`
- `result_summary`
- `output_refs`

## 5. Definition Schema 规则

- `start_vars_schema` 必须是对象，并按原内容写入 `definition_json`。
- `gates` 必须是数组，每个 gate 必须是对象且包含受支持的比较表达式。
- `metrics_schema` 可使用 JSON Schema、简单对象映射或字段数组。
- 声明 `metrics_schema` 后，gate 引用未声明的 `metrics.*` 路径时保存失败，错误包含类似 `steps[0].gates[0].expression` 的精确位置。

## 6. 验证与回滚

自动化测试覆盖：

- AT-01：相同 Definition 和幂等键只创建一个 Run。
- 幂等键请求冲突返回 409。
- 旧创建路径仍可重复创建。
- 组合过滤、分页、latest 和增强字段。
- `start_vars_schema` 保存与 GET 回读一致。
- gate 指标路径校验与精确错误位置。

如需回滚应用代码，应先回滚代码，再保留新增可空字段；无需删除字段。保留字段不会影响旧版 Hub，避免破坏已经写入的 Controller/Correlation 审计数据。

## 7. 下一阶段

下一阶段进入候选用例中心：候选实体、事件历史、状态机、资格验证结果和 `expected_version` 乐观锁。能力缺口会先提供 P0 最小关联对象，以满足 `AUTOMATION_CAPABILITY_GAP` 的验收语义。
