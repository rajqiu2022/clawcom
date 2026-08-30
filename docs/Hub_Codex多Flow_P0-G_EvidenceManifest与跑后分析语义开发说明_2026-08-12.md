# Hub Codex 多 Flow：P0-G Evidence Manifest 与跑后分析语义开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

P0-G 复用现有 Workflow Run、测试报告、`ShiftLeftAnalysisRun`、Finding、幂等记录和 P0-F 实体关系，不另建独立分析系统。新增每个 Workflow Run 唯一的一份 Evidence Manifest，并由服务端执行跑后结论硬校验。

核心规则：缺少 Console、关键 UI 快照或截图等必需证据时，接口拒绝保存 `NO_RISK_FOUND`，只能保存 `ANALYSIS_INCOMPLETE` 或有明确正向证据的风险分类。

本阶段覆盖 AT-10，不给 Flow12 增加前置门禁，也不自动触发跑后分析。

## 2. 数据结构

新增 `workflow_evidence_manifests`：

- `project_id` / `workflow_run_id`
- 可选 `analysis_run_id`
- `coverage_json`
- `artifacts_json`
- `required_evidence_json`
- `completeness_status`
- `missing_required_json`
- `classification`
- `analysis_summary_json`
- `finding_ids_json`
- `revision`
- 分析人、创建人、更新人和时间字段

`workflow_run_id` 唯一，避免同一次 Run 出现互相冲突的多份证据清单。Manifest 更新和结论更新共用 revision，并要求 `expected_version` 乐观锁。

## 3. Evidence Manifest

### 3.1 写入与读取

- `PUT /api/v1/workflow-runs/{run_id}/evidence-manifest`
- `GET /api/v1/workflow-runs/{run_id}/evidence-manifest`

写入示例：

```json
{
  "expected_version": 0,
  "analysis_run_id": 91,
  "coverage": {
    "case_result": "complete",
    "ui_snapshot": "complete",
    "console": "missing",
    "logcat": "not_applicable",
    "screenshots": "partial",
    "performance": "not_requested"
  },
  "artifacts": [
    {
      "type": "screenshot",
      "uri": "hub-artifact://runs/118/screenshots/result.png",
      "sha256": "...64位哈希...",
      "size": 123456,
      "case_id": "33001",
      "step_id": "action_cases_execute"
    }
  ]
}
```

首版默认必需证据：

- `case_result`
- `ui_snapshot`
- `console`
- `screenshots`

调用方可以通过 `required_evidence` 追加平台特有证据，但不能删掉上述 AT-10 核心集合。被列为 required 的证据必须是 `complete`；`partial`、`missing`、`not_requested` 和 `not_applicable` 均不算完整。

coverage 状态支持：

- `complete`
- `partial`
- `missing`
- `not_applicable`
- `not_requested`

### 3.2 制品约束

Manifest 只保存制品链接、SHA-256、大小和小型元数据。API 明确拒绝 artifact 中的 `content`、`data`、`base64`、`blob` 或 `raw` 字段，防止把大日志、截图和性能文件塞入 Workflow JSON 或数据库。

单份 Manifest 最多记录 500 个 artifact；单个 URI 最长 2048 字符；单项 metadata 最大 16 KiB。

## 4. 跑后分析结论

`POST /api/v1/workflow-runs/{run_id}/post-run-analysis`

```json
{
  "expected_version": 1,
  "classification": "ANALYSIS_INCOMPLETE",
  "summary": {
    "reason": "Console 缺失，无法排除运行时异常"
  },
  "finding_ids": []
}
```

允许的分类：

- `CONFIRMED_ANOMALY`
- `PRODUCT_BUG_CANDIDATE`
- `AUTOMATION_RISK`
- `PERFORMANCE_RISK`
- `ANALYSIS_INCOMPLETE`
- `NO_RISK_FOUND`

硬校验：

1. 没有 Manifest 时不能保存跑后结论；
2. Manifest 不完整时，`NO_RISK_FOUND` 返回 409 和 `EVIDENCE_INCOMPLETE_FOR_NO_RISK`；
3. 已保存 `NO_RISK_FOUND` 后，不能直接把证据降级为 incomplete，必须先重新分析；
4. 每次更新要求正确 `expected_version`；
5. 所有写请求要求 `Idempotency-Key`，相同请求重放返回原响应；
6. `finding_ids` 必须存在、未归档且属于同一项目。

## 5. 现有能力复用

### 5.1 测试报告和 Developer AI

现有 `GET /api/v1/test-reports/{report_id}/analysis-context` 增加 `evidence_manifests`。当报告关联的 Analysis Run 绑定 Workflow Run 时，临时鉴权的 Developer AI 可在原报告上下文中读取 Manifest 和结论，无需读取或修改自定义 HTML 文件。

Finding 的备注、评审结论和状态修改继续使用现有 Finding API，不在 Manifest 中复制一套状态机。

### 5.2 实体关系

Manifest 写入和分类时旁路补充：

- `workflow_run -> evidence_manifest`：`has_evidence`
- `workflow_run -> finding`：`produced`
- `evidence_manifest -> finding`：`supports`

关系写入使用 P0-F SAVEPOINT。关系表未迁移或暂时不可用时，Manifest 和跑后结论仍然正常提交。

## 6. 对现有 Flow 的影响

- 不修改 Workflow Definition #12；
- 不修改 Workflow Run 创建、执行、重试、审批或完成状态；
- 不要求 Flow12 等待 Manifest 或跑后分析；
- 只有显式调用新 API 时才创建 Manifest；
- `SHIFT_LEFT_ENABLED=false` 时新接口保持关闭；
- 现有测试报告响应只增加一个兼容字段 `evidence_manifests`。

## 7. 部署顺序

1. 备份数据库；
2. 确认 2026-08-10 测试左移迁移和 P0-F `entity_relations` 已执行；
3. 执行 `ops/migrations/20260812_workflow_evidence_manifests.sql`；
4. 部署 Hub 应用代码；
5. 保持 `SHIFT_LEFT_ENABLED` 原配置，先在测试项目灰度；
6. 对一个已完成的 Workflow Run 写入不完整 Manifest，验证 `NO_RISK_FOUND` 被拒绝；
7. 补齐证据后更新 revision，验证可保存 `NO_RISK_FOUND`；
8. 验证关联报告上下文能读取 Manifest，且 Flow12 原执行入口无变化。

应用代码回退时可保留新表，旧版本不会读取它。若只关闭新能力，设置 `SHIFT_LEFT_ENABLED=false` 即可，不需要删除数据。

## 8. 自动化验证

`tests/test_workflow_evidence_manifests_api.py` 覆盖：

- AT-10：Console 缺失、截图 partial 时拒绝 `NO_RISK_FOUND`；
- 同一证据允许保存 `ANALYSIS_INCOMPLETE`；
- 完整证据允许保存和幂等重放 `NO_RISK_FOUND`；
- 已有无风险结论时拒绝证据降级；
- 自定义 required 列表不能移除 AT-10 核心证据；
- required 证据标为 `not_applicable` 仍不算完整；
- 拒绝内联大制品内容；
- Finding 项目校验、实体关系写入和报告上下文复用；
- 模拟 `entity_relations` 表故障时，Manifest 主事务不受影响。

## 9. 下一阶段建议

P0-H 建议补齐 Capability Gap 的完整生命周期和开发需求关联：列表、幂等 upsert、resolve、候选重新排队以及关联需求审计。当前 P0-B 已能在资格验证失败时创建最小 Gap，但还缺少面向控制器的解决与批量回收接口。
