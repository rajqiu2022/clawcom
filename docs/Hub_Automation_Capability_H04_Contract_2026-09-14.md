# Hub Automation Capability H-04 接口合同

## 目标

Hub 只根据 Worker 实际部署、验收且当前健康的能力解锁 Capability Gap。能力声明、Skill 文本、标题相似度或通用 Shell 权限都不能作为解锁证据。

状态链为：

```text
DeepFlow 不可变 Capability Manifest
→ Worker 校验 Release、合同和健康探针
→ Hub 能力发布
→ 精确 capability/operation/observable/reset token + platform 匹配
→ Gap resolved
→ Candidate READY_FOR_CANARY
→ 真实资格验证
→ 独立 Promotion 才能 ACTIVE
```

## 发布接口

`POST /api/v1/automation-capabilities`

请求必须携带 `Idempotency-Key`，更新还必须携带当前 `expected_version`。示例：

```json
{
  "project_id": 6,
  "key": "racinggo.login.execute",
  "name": "RacingGO 登录大厅",
  "operations": ["login.execute"],
  "observables": ["ui.lobby_ready"],
  "reset_hooks": ["account.restore"],
  "platforms": ["windows_editor"],
  "status": "available",
  "implementation_status": "implemented",
  "verification_status": "verified",
  "implementation_version": "deepflow-v1",
  "producer_claw_id": 11,
  "release_id": "deepflow-<immutable-release-id>",
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "manifest_sha256": "<64 lowercase hex characters>",
  "health_checked_at": "2026-09-14T10:30:00+08:00",
  "health_expires_at": "2026-09-14T11:30:00+08:00",
  "verification": {
    "release_check": {"status": "passed"},
    "contract_check": {"status": "passed"},
    "health_probe": {"status": "passed"},
    "evidence_refs": [
      {"type": "workflow_run", "id": 700}
    ]
  }
}
```

`available` 的健康租约最长 24 小时。Worker 应在到期前用新幂等键和当前 `expected_version` 续报；同一事件重试必须复用原幂等键。

`verification.release_check`、`verification.contract_check` 和 `verification.health_probe` 必须全部通过。`contract_check` 必须覆盖 operation contract 版本与目标平台兼容性；任一失败时 Hub 拒绝 `available`，因此不兼容版本不能解锁 Gap。

非可用状态为 `planned/degraded/unavailable/retired`。这类状态不会解锁 Gap；已经由该能力解锁的 Gap 会重新打开，未晋升候选退回 `WAITING_CAPABILITY`，已晋升候选进入 `QUARANTINED`。旧的资格证据保存在 Candidate Event 中。

## 精确关系与版本规则

- Gap 的 `missing_capabilities`、`required_operations`、`required_observables`、`required_reset_hooks` 是唯一匹配来源。
- 平台来自 Gap `evidence.platform/platforms` 或候选 `case_draft.automation.platform/platforms`。
- Hub 持久化 `automation_capability --satisfies--> capability_gap` 关系，记录匹配 token、平台、Release、Manifest 和健康截止时间。
- 健康续租不会触发重新资格验证。
- implementation version、producer、Release、source commit、manifest 或主能力来源变化会将既有结果重新放回 `READY_FOR_CANARY`。
- 重复幂等事件返回原结果，不增加版本、不重复 resolve/requeue。

## 查询与过期对账

`GET /api/v1/automation-capabilities?project_id=6` 返回：

- 每项的 `eligible_for_gap_resolution` 与 `eligibility_reason`；
- `diagnostics.catalog_total`、`eligible_for_gap_resolution`、`ineligible`；
- 自动识别已过期健康租约，并将其置为 `unavailable` 后撤销相应 Gap 资格。

Worker 启动、恢复或健康扫描后也可显式调用：

`POST /api/v1/automation-capabilities/reconcile`

```json
{"project_id": 6}
```

该接口仅做健康租约与 Gap/Candidate 对账，不执行测试、不启动 Flow，也不直接晋升正式用例。
