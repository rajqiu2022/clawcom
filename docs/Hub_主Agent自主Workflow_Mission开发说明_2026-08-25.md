# Hub 主 Agent 自主 Workflow Mission 开发说明

## 1. 决策结论

采用“主 Agent 自主调度，Hub 最小控制面”的模式：

- 主 Agent 理解目标、查看可用 Flow、决定下一步并发起 Child Run。
- Hub 不解析业务语义，不要求逐节点新增合同或人工 Gate。
- Hub 只校验主 Agent 身份、项目边界、幂等键、Mission 状态和 Child Run 预算。
- 现有 Workflow Definition、Run、Step、Worker/AgentTask 路径保持不变。
- 对外通知、凭据、破坏性动作继续使用已有平台级硬门禁，不扩散到普通内部调度。

## 2. Mission 默认授权

Mission 创建时一次性快照“创建者当时有权执行”的项目内全部 `active` Workflow
Definition；只有显式填写 `allowed_definition_ids` 时才进一步收窄，
`denied_definition_ids` 始终优先排除。后续 dispatch 不再逐 Flow 重新申请授权。

```json
{
  "project_id": 6,
  "main_claw_id": 11,
  "objective": "自主完成代码分析、用例设计和验证",
  "max_child_runs": 20,
  "max_retries_per_flow": 3,
  "expires_in_hours": 8,
  "allow_external_notification": false,
  "allow_destructive_actions": false
}
```

这是一次性、运行级授权。有效期内不再为每个 Flow 或普通节点重复申请人工权限。

## 3. API

### 3.1 创建 Mission

```http
POST /api/v1/workflow-missions
```

用户管理员可指定项目内主 Agent；Claw 只能为自己创建 Mission。

### 3.2 主 Agent 查询 Mission 和可用 Flow

```http
GET /api/v1/workflow-missions/{mission_id}
GET /api/v1/workflow-missions/{mission_id}/definitions
```

第二个接口返回项目内当前可调度的 active Flow 摘要，不要求主 Agent预先记住 Definition ID。

### 3.3 主 Agent自主 Dispatch

```http
POST /api/v1/workflow-missions/{mission_id}/dispatch
```

最小请求：

```json
{
  "workflow_definition_id": 37
}
```

推荐请求：

```json
{
  "workflow_definition_id": 37,
  "start_vars": {"discovery_report_id": 123},
  "reason": "分析已完成，下一步设计候选用例",
  "decision_key": "case-design-stage-1"
}
```

`decision_key` 可省略；Hub 会根据规范化请求生成稳定自动幂等键。相同决策重放返回同一个
Child Run。不同请求复用同一个 key 返回冲突。

Mission dispatch 禁止传入：

- `executor_claw_ids`
- `target_claw_ids`
- `selected_claw_ids`
- `worker_claw_id`
- 其他执行者覆盖字段

执行者继续来自 Definition；物理 Worker 固定为 Mission 主 Agent。

### 3.4 结束 Mission

```http
POST /api/v1/workflow-missions/{mission_id}/complete
POST /api/v1/workflow-missions/{mission_id}/cancel
```

结束 Mission 只禁止创建新的 Child Run，不会隐式取消已经创建的 Run。

## 4. 兼容性

- Mission 是增量能力，不修改原有 `POST /workflow-runs`。
- Child Run 继续使用现有 `controller_run_id`、`correlation_id`、Run/Step 状态机和结果接口。
- 测试用例库快照仍复用原有冻结逻辑。
- Child Run 从历史页面删除后，Mission dispatch 审计和预算仍保留；相同决策重放返回
  `run_deleted=true`，不会悄悄重新创建。
- 现有 Workflow 的严格合同和通知门禁保持原样；Mission 不额外增加合同。

## 5. 主 Agent 上下文

Hub 在 `/sidecar-config` 中为有 active Mission 的 Codex 注入运行时规则：

- 下一步 Flow 由主 Agent自主决定。
- 先查询 `definitions_api`，再调用 `dispatch_api`。
- 普通 Mission dispatch 不要求逐节点人工 Gate。
- 禁止执行者覆盖。
- Child Run 结束后由主 Agent读取结果并决定继续、换 Flow、重试或结束 Mission。

Hub 能力索引版本升级为 v4，增加 Mission API 路由提示。

## 6. Worker 侧剩余适配

Hub API 本身不要求 Mission dispatch 携带逐步骤 `verify.expected`。当前 Worker 的
`hub_proxy` 对未知写路径只要求稳定 `operation_id`，不会强制 `verify`，因此 Mission 新路径
已经满足“授权一次、范围内不反复阻断”。调用示例：

```json
{
  "method": "POST",
  "path": "/api/v1/workflow-missions/91/dispatch",
  "operation_id": "mission-91-case-design-1",
  "body": {"workflow_definition_id": 37, "decision_key": "case-design-1"}
}
```

不要传 `verify`；Hub 服务端负责主 Agent身份、项目、幂等和预算校验。Worker 后续只需增加
Mission 路径的工具说明与专项测试，不应再增加 Definition 白名单或逐节点合同。

## 7. 当前验证

- Mission API 单测覆盖：项目 Flow发现、主 Agent身份、自动幂等、执行者覆盖拒绝、预算、过期、
  完成 Mission、Child Run 删除后审计保留。
- 原 Workflow Run 控制面和 Worker 合同测试保持通过。
- 全量回归：`625 passed`，另有 2 个子测试通过。
- 已完成 MariaDB 迁移并部署 Hub。
- 生产 canary 使用临时离线 Codex Agent：Mission 创建、项目 Flow发现、自主 dispatch、
  幂等重放、sidecar-config Mission 规则注入、Mission 完成均通过；临时 Mission、Run、
  AgentTask、Definition、Agent 与审计测试数据已清理。
