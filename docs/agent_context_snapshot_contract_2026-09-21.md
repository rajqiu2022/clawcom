# Hub Agent 上下文快照合同（第一阶段）

## 目标

Hub 是 Agent 身份、团队岗位、Profile、Rule、Skill 与运行时身份的唯一可信来源。
每个新 Workflow Run 创建时冻结一次上下文；现有 Run 永不因团队或配置变化被重写。

## 测试经理合同

测试经理是团队级管理者，不是默认执行者，职责包括：

1. 理解目标、创建计划并拆分可验收任务。
2. 调度团队内已有代码分析员和测试执行员。
3. 跟踪任务进度、阻断与恢复。
4. 检查分析结论、执行证据和报告完整性；不合格结果退回。
5. 汇总结果、给出结论并完成闭环。

同一交付物中，测试经理不得伪造执行证据；执行者不得同时作为独立评审者。

## Hub 数据合同

- `agent_context_snapshots`：每个 Run 唯一、不可变的安全上下文文档。
- `workflow_runs.context_snapshot_id/context_snapshot_sha256`：Run 绑定。
- `agent_tasks.context_snapshot_id/context_snapshot_sha256`：任务继承同一绑定。
- `GET /api/v1/workflow-runs/{run_id}/agent-context-snapshot`：按 Run 可见性回读。

快照包含团队版本、角色名单、经理任期、manager/executor/reviewer 分配、无密钥
Runtime 摘要、Profile 内容与版本、Rule 内容与 SHA、Skill manifest 与 SHA。
不保存 Token、认证信息或 Secret。

## 创建与校验

- 普通 Run：如果发起 Claw 作为唯一团队经理且该团队选择了此 Flow，绑定团队快照。
- Team Mission Child Run：显式绑定 Mission 团队与派发经理。
- 外部评审 Child Run：生成非团队快照，保留独立评审链路现有语义。
- 团队 Run 中 manager=executor 或 executor=reviewer 时返回
  `POLICY_ROLE_OVERLAP`，不创建 Run。

## Worker 下一阶段

Worker 领取任务后必须：

1. 从任务读取 `context_snapshot_id` 与 `context_snapshot_sha256`。
2. 回读快照并校验 SHA，按快照注入身份、岗位合同、Profile、Rules 与 Skills。
3. claim/progress/heartbeat/result 始终回传同一 ID/SHA。
4. 缺失、SHA 不符、角色冲突或能力不符时不得调用模型，返回
   `POLICY_CONTEXT_INCOMPLETE`。

兼容切换建议：先让 Worker 上报回执并观测，再由 Hub 对已带快照的新任务开启强制校验；
历史 Run 不补写快照，也不改变原执行语义。
