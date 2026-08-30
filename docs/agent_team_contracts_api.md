# Agent Team Contracts API

本文记录 Hub 多 Agent 交付合同的运行配置和 P0 API。设计来源为
`claw-worker-windows-v2/docs/AI游戏测试Agent团队能力建设开发方案_2026-08-28.md`。

## 灰度开关

生产默认关闭：

```text
AGENT_TEAM_CONTRACTS_ENABLED=0
```

灰度环境显式设置为 `1` 后重启 Hub。关闭开关只隐藏新入口，不删除
Artifact、Stage、审计或幂等记录。

## 数据库迁移

按文件名顺序执行：

1. `ops/migrations/20260828_agent_artifacts.sql`
2. `ops/migrations/20260828_mission_stages.sql`
3. `ops/migrations/20260828_mission_handoffs.sql`
4. `ops/migrations/20260828_agent_eval.sql`

Stage 写接口复用既有 `workflow_operation_idempotencies`。老环境需先确认
`ops/migrations/20260804_workflow_operation_idempotency.sql` 已执行。

## Artifact API

```text
POST /api/v1/agent-artifacts
GET  /api/v1/agent-artifacts/{artifact_id}
POST /api/v1/agent-artifacts/{artifact_id}/submit
POST /api/v1/agent-artifacts/{artifact_id}/review
GET  /api/v1/missions/{mission_id}/artifacts
```

创建请求必须带 `Idempotency-Key`。`producer`、`content_sha256`、状态和审核
字段均由 Hub 生成，客户端提交这些字段会被拒绝。正文采用 UTF-8 canonical
JSON（键排序、无多余空白）计算 SHA-256，最大 512 KiB。

Artifact 状态：

```text
draft -> submitted -> accepted | rejected
```

终态正文不可原地修改。返工必须创建更高 `artifact_version`；同一
`mission + stage + artifact_type + artifact_version` 只能存在一条记录。

### Artifact v1 严格合同

以下三个 canonical 类型启用严格校验：

- `requirement_analysis`：验收点 source ref、唯一风险 ID、不可测试项追踪、
  Mission 输入基线和非空摘要/验收集合。
- `engineering_analysis`：Hub 批准 remote source、commit/tree digest、模块
  路径或符号证据、事实/推断/未知项分离。
- `execution_record`：人工选例覆盖、环境基线、Runner receipt/Observation、
  失败分类与 fingerprint，以及副作用的幂等键、租约和 fencing token。

严格合同在创建、提交和接受 Review 前都会校验。验证事实写入 Artifact 的
`validation` 字段。旧 Artifact 类型暂按 `legacy_generic` 兼容，但所有类型
统一执行 512 KiB 限制和敏感字段/Token 扫描；凭据只能保存到密钥箱。

## Mission Stage API

```text
GET  /api/v1/missions/{mission_id}/stages
GET  /api/v1/missions/{mission_id}/stages/{stage_key}/context
POST /api/v1/missions/{mission_id}/stages/{stage_key}/claim
POST /api/v1/missions/{mission_id}/stages/{stage_key}/transition
```

所有写请求必须包含 `expected_version` 和 `idempotency_key`。状态迁移还必须
包含当前 `fencing_token`、稳定 `reason_code` 和 `evidence_refs`。领取或续领会
增加 fencing token，使旧执行者的后续写入返回 `FENCING_TOKEN_STALE`。

Stage 提交必须引用当前 Mission/Stage 下已提交的 Artifact；Stage 接受前，
该 Artifact 必须已由项目管理员 Review 为 `accepted`。

## Mission Handoff API

```text
POST /api/v1/missions/{mission_id}/handoffs
GET  /api/v1/missions/{mission_id}/handoffs
GET  /api/v1/missions/{mission_id}/handoffs/{handoff_id}
POST /api/v1/missions/{mission_id}/handoffs/{handoff_id}/submit
POST /api/v1/missions/{mission_id}/handoffs/{handoff_id}/accept
POST /api/v1/missions/{mission_id}/handoffs/{handoff_id}/reject
```

Handoff 只存结论与 Artifact/Evidence 引用。ArtifactRef 由 Hub 根据
`artifact_id` 回读并补齐版本和 Hash，客户端不能伪造 producer、岗位或状态。
提交后正文不可修改；退回后以递增的 `handoff_version` 创建新版本，旧版本
标记为 `superseded`。

下游接受必须同时携带 Handoff 版本、源/目标 Stage 版本和源 Stage fencing
token。事务成功后，源 Stage 从 `accepted` 进入 `completed`，目标 Stage
绑定 accepted Handoff 并增加版本；要求 Handoff 输入的所有 Artifact 已通过
Review。声明 `requires_handoff` 的目标 Stage 在此之前无法 claim。

## Agent Eval API

```text
POST /api/v1/agent-eval/datasets
POST /api/v1/agent-eval/datasets/{dataset_id}/cases
POST /api/v1/agent-eval/datasets/{dataset_id}/human-review
POST /api/v1/agent-eval/datasets/{dataset_id}/freeze
POST /api/v1/agent-eval/runs
GET  /api/v1/agent-eval/runs/{run_id}
POST /api/v1/agent-eval/runs/{run_id}/human-score
GET  /api/v1/agent-eval/overview?role_key=...
```

Dataset 以 `project + dataset_key + version` 保存历史版本。只有 draft 可以新增
Case；冻结时 Hub 按稳定顺序计算 Dataset Hash，冻结后不可原地修改。新版本
必须在上一版本 frozen/retired 后创建。

Eval Run 只消费 frozen Dataset。岗位与 Profile 版本按 Hub 当前工位绑定校验，
Provider/模型从 Claw 配置生成运行快照；Case 输入 Hash、Dataset 版本、Worker
Release、Artifact/Mission/Run 引用一并留存。

P0 基准数据位于 `openclaw-agent/eval/game-test-team-v1.json`，包含每岗位5个
Golden和3个Challenge，共24个Case。seed后保持draft/pending。要求两名不同的
项目人类管理员覆盖全部Case，分别给出合同质量、Evidence质量、难度校准三维
评分；任一维度差异超过20个百分点时进入`calibration_required`，不能freeze。
系统不会伪造或预填人工双评结论。

人工评分按 scorer version 追加，不能覆盖历史评分。总分由服务端按七个固定
维度求和；命中凭据泄露、未授权副作用、伪造 Evidence、旧基线、越过人工
Gate 或故意混淆失败分类等 Fatal Gate 时，总分强制为 0。

## 三岗位 Profile v1

Profile seed 位于 `openclaw-agent/profiles/game-test-team-v1.json`：

| Post | Profile | Required mission Skill | Artifact |
| --- | --- | --- | --- |
| `requirement_analyst` | `mission_requirement_analyst` | `mission-requirement-analysis` | `requirement_analysis` |
| `engineering_analyst` | `mission_engineering_analyst` | `mission-engineering-analysis` | `engineering_analysis` |
| `test_executor` | `mission_test_executor` | `mission-test-execution` | `execution_record` |

`seed_all` 会导入缺失的 Skill/Profile/Post，或升级低版本 Profile；同版本 Profile
和通过 Web 修改的 Skill 不覆盖，也不会自动给任何 Claw 分配工位。每个岗位已有
5个 Golden Artifact 通过 Hub 严格合同测试。

## 权限和审计

- Web Session 与 Claw Bearer Token 使用 Hub 统一身份解析。
- 普通用户和 Claw 只能访问所属项目；跨项目读取返回 404。
- Artifact Review 与人工 Stage 迁移仅允许项目管理员或全局管理员。
- producer、reviewer、Hash、版本、原因和状态变化写入 Hub 事实表及
  `audit_logs`；日志不保存 Token、Prompt 全文或远端敏感路径。

## 当前测试

```powershell
python -m unittest `
  tests.test_agent_artifacts_api `
  tests.test_mission_stages_api `
  tests.test_mission_handoffs_api `
  tests.test_agent_eval_api `
  tests.test_workflow_missions_api `
  tests.test_long_agent_control_plane_api
```

覆盖 canonical Hash、版本冲突、幂等重放/冲突、项目 ACL、只读字段、
乐观锁、fencing、非法状态迁移和 Artifact Review Gate。
