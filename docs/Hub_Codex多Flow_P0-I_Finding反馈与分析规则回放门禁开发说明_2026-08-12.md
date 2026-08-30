# Hub Codex 多 Flow：P0-I Finding 反馈与分析规则回放门禁开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

P0-I 复用现有 ShiftLeft Finding、Developer AI 临时协作、Workflow Run、全局幂等记录和 P0-F 实体关系，补齐两类数据：

1. 可统计、不可变的 Finding 反馈事实；
2. 不可原地覆盖、必须经过历史回放门禁的分析规则版本。

规则创建、编辑和回放均不会自动修改线上分析行为。只有明确执行 activate，且目标版本已有通过门槛的历史回放时，才能成为 active。新版本激活时旧 active 版本原子退役。

## 2. Finding 统一反馈

### 2.1 标签

支持以下固定标签：

- `true_positive`
- `false_positive`
- `duplicate_bug`
- `automation_unsupported`
- `human_confirmed`
- `human_rejected`
- `fixed_before_validation`

反馈保存于新增的 `shift_left_finding_feedback`，每条都是不可变事实，包含：

- Finding 和项目；
- 标签、备注和证据引用；
- 来源类型：`human`、`developer_ai` 或 `system`；
- actor、幂等键、请求 hash 和时间。

同时向原 `shift_left_finding_events` 追加 `feedback` 事件，使现有 Finding 时间线无需改造即可展示。

### 2.2 API

- `POST /api/v1/shift-left/findings/{id}/feedback`
- `GET /api/v1/shift-left/findings/{id}/feedback`

写入示例：

```json
{
  "label": "true_positive",
  "from_revision": 3,
  "note": "Console 与业务状态都确认奖励状态不一致",
  "evidence_refs": [
    "hub-artifact://runs/118/console.log"
  ]
}
```

要求：

- 写请求必须携带 `Idempotency-Key`；
- `from_revision` 必须等于当前 Finding revision；
- note 或 evidence_refs 至少提供一项；
- 相同请求重放不产生重复反馈或事件；
- Developer AI 临时 Token 可在原 `finding:review` scope 和原报告/Finding 边界内提交反馈，不能跨报告或跨项目。

GET 返回反馈明细及按标签聚合的 counts。Finding 详情响应增加兼容字段 `feedback`。

## 3. 分析规则数据结构

新增：

- `analysis_rules`：项目级稳定规则身份；
- `analysis_rule_versions`：不可变规则定义和门槛快照；
- `analysis_rule_replays`：历史回放数据集、指标、影响范围和门禁结果。

### 3.1 Rule

Rule 使用 `(project_id, rule_key)` 唯一标识，保存：

- 名称和描述；
- 当前 active version；
- 最新版本号；
- identity version，用于并发保护；
- `draft`、`active` 或 `retired` 汇总状态。

### 3.2 Rule Version

每个版本保存完整 definition、thresholds、change summary、来源 Finding、based-on version 和 definition hash。definition 与 thresholds 一起参与 hash；相同规则下不允许重复创建完全相同的行为快照。

版本状态严格为：

```text
draft -> replay_passed -> active -> retired
```

版本没有 PATCH 接口，不能原地修改。任何规则或门槛调整都必须创建新版本。

### 3.3 Replay

每次回放保存：

- 可重放的 `dataset_snapshot_ref` 和 SHA-256；
- 可选 Workflow Run；
- 命中数、真阳性、误报、漏报和样本数；
- 服务端计算的 precision、recall、误报率、漏报率和真阴性；
- 变化范围 impact；
- 报告或证据 artifact refs；
- gate 是否通过以及逐项失败原因。

Hub 不在接口内执行规则。历史数据准备、规则执行和结果报告仍由独立 Workflow/Codex 完成；Hub 验证指标一致性、计算门禁并保存审计快照。

## 4. API

### 4.1 创建和查询规则

- `POST /api/v1/analysis-rules`
- `GET /api/v1/analysis-rules?project_id=6&status=active`
- `GET /api/v1/analysis-rules/{id}`

创建规则时同时创建 version 1 draft：

```json
{
  "project_id": 6,
  "rule_key": "reward-state-transition-consistency",
  "name": "奖励状态一致性",
  "definition": {
    "kind": "event_state_consistency",
    "event": "reward_claimed",
    "assertions": ["claimed_once", "balance_delta_matches"]
  },
  "thresholds": {
    "min_sample_count": 20,
    "min_precision": 0.8,
    "min_recall": 0.8,
    "max_false_positive_rate": 0.2,
    "max_false_negative_rate": 0.2
  },
  "source_finding_ids": [9001]
}
```

### 4.2 创建新版本

`POST /api/v1/analysis-rules/{id}/versions`

要求 Rule 的 `expected_version`。可以用 `based_on_version_id` 指明基线；未提供时默认基于当前 active version。创建新 draft 不会改变 active version。

### 4.3 提交历史回放

`POST /api/v1/analysis-rule-versions/{id}/replays`

```json
{
  "workflow_run_id": 201,
  "dataset_snapshot_ref": "hub-artifact://replays/racinggo-20260812.json",
  "dataset_hash": "...64位哈希...",
  "metrics": {
    "sample_count": 100,
    "hit_count": 10,
    "true_positive_count": 9,
    "false_positive_count": 1,
    "false_negative_count": 1
  },
  "impact": {
    "changed_modules": ["lobby.reward"],
    "new_hits": 2,
    "removed_hits": 1
  },
  "artifact_refs": [
    "hub-artifact://replays/racinggo-20260812.html"
  ]
}
```

默认门槛：

- 样本数至少 20；
- precision 至少 0.8；
- recall 至少 0.8；
- 误报率不超过 0.2；
- 漏报率不超过 0.2。

门槛随版本冻结，可在新版本中显式调整。失败回放保存完整结果但版本保持 draft；通过后版本进入 `replay_passed`。

### 4.4 激活和退役

- `POST /api/v1/analysis-rule-versions/{id}/activate`
- `POST /api/v1/analysis-rule-versions/{id}/retire`

activate 要求：

- 正确的 Rule `expected_version`；
- 目标版本状态为 `replay_passed`；
- 至少有一条 `gate_passed=true` 的回放；
- 显式幂等写请求。

激活新版本时，同一 Rule 的旧 active version 在同一事务中进入 retired。不会出现两个 active version。

手动 retire 只允许当前 active version，必须提供 reason。退役后 Rule 没有 active version，线上调用方应停止加载该规则。

## 5. 指标一致性规则

服务端拒绝以下不一致数据：

- 任意计数为负数；
- `hit_count != true_positive_count + false_positive_count`；
- 样本数小于真阳性、误报和漏报之和；
- dataset hash 不是 64 位十六进制；
- impact 为空，无法展示变化范围；
- Workflow Run 不属于规则项目。

precision、recall、误报率、漏报率和真阴性由 Hub 计算，不信任调用方传入的派生值。

## 6. 并发、权限与追溯

- 所有写接口复用 `WorkflowOperationIdempotency`；
- Rule identity 更新要求 `expected_version`；
- 激活时先锁 Rule 再锁 Version，避免多版本并发激活死锁；
- 项目权限沿用 Hub 用户/Agent 项目隔离；
- 临时 Developer AI 只可写 Finding feedback，不能创建或激活规则；
- 来源 Finding 与规则版本建立 `finding -> learned_rule` 的 `learned_into` 关系；
- 规则版本 ID 在关系中使用 `{rule_id}:{version_no}`，支持 P0-F 反向追溯。

## 7. 对现有业务的影响

- 不修改 Flow12、测试计划、报告生成和现有 Finding 状态机；
- 不自动创建、激活或退役规则；
- 不将 active 规则自动注入旧 Workflow；
- Finding 详情只增加 `feedback` 字段；
- 新表不存在时旧接口仍不访问这些表；部署时必须先执行迁移再部署包含 P0-I 的应用代码。

## 8. 部署顺序

1. 备份数据库；
2. 确认测试左移 MVP-A 和 P0-F 迁移已执行；
3. 执行 `ops/migrations/20260812_finding_feedback_analysis_rules.sql`；
4. 部署 Hub 应用代码；
5. 为一个已有 Finding 提交 feedback 并验证幂等重放；
6. 创建一条 draft 规则，验证未回放时 activate 返回 409；
7. 提交一条失败回放，确认保留 draft 和 gate reasons；
8. 提交达标回放并显式 activate；
9. 创建 version 2，确认 version 1 继续 active；
10. version 2 回放通过并激活后，确认 version 1 retired。

回退应用代码时可保留新增表。P0-I 没有自动执行路径，回退不影响 Flow。回退前若已有 active rule，调用方应继续使用部署前固定的规则快照，不要动态读取未受旧版本支持的数据。

## 9. 自动化验证

`tests/test_analysis_rule_learning_api.py` 覆盖：

- 反馈标签校验、revision 校验、幂等和 Finding 时间线；
- 未回放规则不能激活；
- 失败回放保留 draft 和失败原因；
- 达标回放进入 replay_passed；
- activate 幂等且规则进入 active；
- 新 draft 不替换旧 active；
- version 2 独立回放通过后激活并原子退役 version 1；
- active version 手动退役。

现有 `test_shift_left_api.py` 同时验证 Developer AI 临时协作 Token 可在授权 Finding 上写入标准反馈。

## 10. 下一阶段建议

P0-J 建议提供最小“自动化闭环”管理页面和聚合 API，统一展示候选状态、Capability Gap、资源租约、用例库 revision、Flow 快照、Evidence Manifest 分类和规则回放状态；先做可操作列表与跳转，不做复杂图表。
