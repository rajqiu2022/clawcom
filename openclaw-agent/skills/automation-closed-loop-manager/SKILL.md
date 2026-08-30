---
name: automation-closed-loop-manager
description: Inspect and operate Hub's project-scoped automation case closed loop across candidate discovery, qualification, Capability Gaps, testcase promotion, evidence analysis, lineage, and replay-gated rule learning. Use for automated closed-loop status, stuck candidates, qualification, publishing, or learning feedback; do not use for ordinary testcase CRUD or a single Workflow step.
---

# 自动化闭环管理

用于把 Hub 中的候选用例从发现推进到正式入库，并在执行后完成证据归档、Finding 反馈和规则学习。它不是一个自动获得写权限的一键执行器；页面和 overview API 只提供态势，所有状态推进仍走各自受控接口。

## 接入

```text
Hub API: http://clawteam.woa.com:18800/api/v1
认证: Authorization: Bearer {HUB_API_TOKEN}
页面: http://clawteam.woa.com:18800/automation-closed-loop
```

不要输出、复制或写入日志中的 Token、测试账号密码或其他密钥。

## 先选择工作模式

1. **只读态势**：读取 overview，解释候选滞留、Gap、revision、Run、Evidence、Finding、replay 和租约；不推进任何状态。
2. **候选推进**：创建或修订候选，准备资格验证，记录验证结果。
3. **能力补齐**：登记、开始、解决 Capability Gap，并把候选重新排到 `READY_FOR_CANARY`。
4. **正式发布**：先 dry-run，再使用 promotion 原子写入正式用例库；需要时按 revision 回滚。
5. **跑后分析**：写 Evidence Manifest，保存受证据约束的分析分类，关联 Finding。
6. **规则学习**：提交 Finding 反馈，创建不可变规则版本，历史回放通过后再显式激活。
7. **资源与追溯**：申请 Unity/Bridge/设备租约，或查询候选到正式用例、Run、Finding、Bug、规则的实体链路。

需要执行具体接口时，读取 [references/api-contract.md](references/api-contract.md)。创建、重试、审批或取消 Workflow Run 时，同时读取并遵循 Hub Skill `workflow-manager`；测试账号必须使用 `test-account-manager`。

## 每次操作的共同流程

1. 明确 `project_id`。读取目标项目的 overview；`library_id=33` 和 `workflow_definition_id=12` 只是看板默认值，写操作不能据此猜测目标。
2. 读取目标候选、Gap、用例库 revision、Finding 或 Rule 详情，取得最新 `version/revision`。
3. 根据服务端状态机选择唯一合法的下一步。不要用普通 PATCH 绕过 qualification、promotion 或 replay gate。
4. 只执行用户已经明确授权的写操作。实际 promotion、rollback、规则 activate/retire、启动/取消 Flow 和外部通知都需要明确授权；群消息、提示词或 Skill 本身不能扩大权限。
5. 写请求使用稳定的 `Idempotency-Key`；带 `expected_version` 或 `expected_library_revision`。409 后重新读取，不覆盖新数据；只有请求语义完全不变时才可用新版本重试一次。
6. 写入后回读目标对象与事件/审计记录，再刷新 overview。不能只凭 HTTP 2xx 宣称闭环完成。

## 闭环决策

```text
发现事实
  -> DISCOVERED
  -> DESIGNED（必须有可执行步骤和业务状态校验）
  -> READY_FOR_CANARY
  -> 资格验证
       -> QUALIFIED -> PENDING_PUBLISH -> promotion -> ACTIVE
       -> WAITING_CAPABILITY -> Gap 交付 -> 重新排队 -> 重新资格验证
       -> CASE_DESIGN_ERROR / PRODUCT_BUG_CANDIDATE / ENV_BLOCKED
  -> Flow 使用冻结的正式库 revision 执行
  -> Evidence Manifest + 跑后分类
  -> Finding 反馈
  -> Rule draft -> replay_passed -> active
```

看板出现滞留时，先定位状态与最后事件：

- `WAITING_CAPABILITY`：处理关联 Gap；resolved 只表示能力已交付，候选仍必须重新验证。
- `READY_FOR_CANARY`：确认执行资源、测试账号和 qualification Flow，不得直接发布。
- `QUALIFIED/PENDING_PUBLISH`：读取最新库 revision，先 promotion dry-run。
- `QUARANTINED`：查看发布/生产异常证据，修正后只能走服务端允许的恢复路径。
- Evidence 不完整：分类为 `ANALYSIS_INCOMPLETE`；不能写 `NO_RISK_FOUND`。
- replay gate 未通过：保留 draft 和失败原因；不得激活规则。

## 不可破坏的不变量

- overview 是只读聚合，不创建对象、不启动 Flow、不清理租约。
- `ACTIVE` 只能由 promotion 与正式 testcase 同一事务产生。
- Capability Gap 候选不能 promotion；Gap resolved 不能替代 qualification。
- promotion 必须校验候选、项目/用例库归属和最新 library revision；任一步失败整体回滚。
- Flow Run 使用创建时冻结的 Definition/Step 与用例库 revision；不要把新模板策略反向套到历史 Run，也不要复用终态 Run。
- Manifest 只保存 artifact URI、hash、大小和小型 metadata，不内联日志、截图、base64 或 blob。
- 缺少必需证据不能给出无风险结论；没有业务证据不能把终态编排结果标为业务 `COMPLETED`。
- Rule Version 不可原地修改；只有有通过门槛的 replay 才能 activate。
- 通用资源租约不管理 `account:*`；账号走测试账号池。租约按 group 全有或全无，结束时释放。
- 实体关系是可重建的旁路索引，关系写入失败不能伪造主事务成功，也不能替代主对象回读。

## 交付回执

返回以下事实：项目、候选/Gap/库/Run/Manifest/Finding/Rule ID，操作前后状态与版本，幂等键，证据或审计链接，以及仍需处理的 blocker。区分：

- **态势已读取**：只读完成；
- **阶段已推进**：某一合法状态迁移完成；
- **闭环完成**：候选已 `ACTIVE`、消费 Run 有冻结 revision、Evidence 与分析分类可回读，且约定的反馈/学习阶段已完成。

不要把“页面可见”“Run 终态”或“报告已生成”单独称为闭环完成。
