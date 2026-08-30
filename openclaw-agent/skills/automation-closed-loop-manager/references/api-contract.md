# Hub 自动化闭环 API 合同

只在执行对应模式时读取本文件。所有示例都使用：

```http
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

写接口优先同时提供 `Idempotency-Key` Header 和 body `idempotency_key`，两者保持一致。ID、版本、revision 和项目归属必须从当次 GET 回读取得。

## 1. 态势与追溯

### 项目态势

```http
GET /api/v1/automation-closed-loop/overview
  ?project_id={project_id}
  &library_id={library_id}
  &workflow_definition_id={definition_id}
  &stale_hours=24
  &limit=12
```

`project_id` 必填；`library_id` 默认 33、Definition 默认 12，但默认值只用于只读展示。响应包含 `candidates`、`capability_gaps`、`resource_leases`、`testcase_library`、`workflow_runs`、`evidence`、`findings`、`analysis_rules`，并带 `Cache-Control: no-store`。

### 实体链路

```http
GET /api/v1/entity-relations/trace?project_id={project_id}&entity_type=automation_case_candidate&entity_id={id}&direction=both&max_depth=5
```

`direction` 为 `upstream/downstream/both`，`max_depth` 为 1～5。`truncated=true` 时缩小方向或实体范围，不把不完整图当成完整追溯。

## 2. 候选用例

### 幂等创建

```http
POST /api/v1/automation-case-candidates:upsert
```

```json
{
  "project_id": 6,
  "title": "奖励领取后状态与余额一致",
  "dedupe_key": "racinggo:reward:claim-state-balance:v1",
  "idempotency_key": "candidate-reward-v1",
  "state": "DESIGNED",
  "module_key": "lobby.reward",
  "source_type": "commit",
  "source_refs": [{"type": "commit", "value": "8b94c62"}],
  "required_capabilities": ["read_reward_state", "restore_test_account"],
  "production_library_id": 33,
  "case_draft": {
    "preconditions": ["奖励可领取，测试账号可恢复"],
    "steps": ["记录余额和奖励状态", "领取一次奖励", "再次读取状态和余额"],
    "expected_results": ["领取仅成功一次", "奖励状态已变更", "余额增量与奖励一致"]
  }
}
```

新候选只能从 `DISCOVERED` 或 `DESIGNED` 开始。`DESIGNED` 必须有可执行步骤和业务状态校验。

### 查询与更新

```http
GET   /api/v1/automation-case-candidates?project_id=6&state=READY_FOR_CANARY&page=1&page_size=50
GET   /api/v1/automation-case-candidates/{candidate_id}
PATCH /api/v1/automation-case-candidates/{candidate_id}
```

PATCH 必须带最新 `expected_version`。正常路径：

```text
DISCOVERED -> DESIGNED -> READY_FOR_CANARY -> QUALIFIED -> PENDING_PUBLISH
```

恢复路径由服务端校验：

```text
WAITING_CAPABILITY / ENV_BLOCKED / QUARANTINED -> READY_FOR_CANARY
CASE_DESIGN_ERROR / PRODUCT_BUG_CANDIDATE -> DESIGNED
```

不能 PATCH 到 `ACTIVE`。

### 记录资格验证结果

```http
POST /api/v1/automation-case-candidates/{candidate_id}/qualification-result
```

```json
{
  "expected_version": 3,
  "idempotency_key": "qualification-candidate-1024-run-201",
  "qualification_run_id": 201,
  "qualification_outcome": "QUALIFIED",
  "evidence": {
    "result": "hub-artifact://runs/201/qualification.json",
    "screenshots": ["hub-artifact://runs/201/result.png"]
  }
}
```

只接受 `READY_FOR_CANARY`。outcome 为：

- `QUALIFIED` → `QUALIFIED`
- `AUTOMATION_CAPABILITY_GAP` → `WAITING_CAPABILITY`
- `CASE_DESIGN_ERROR` → `CASE_DESIGN_ERROR`
- `PRODUCT_BUG_CANDIDATE` → `PRODUCT_BUG_CANDIDATE`

能力不足时还要提供 `missing_capabilities`，可选 `capability_gap_key/title`、`required_operations`、`required_observables`、`required_reset_hooks` 和 `development_requirement`。Hub 会创建/复用 Gap，不会创建正式 testcase。

## 3. Capability Gap

```http
POST /api/v1/capability-gaps:upsert
GET  /api/v1/capability-gaps?project_id=6&status=resolved&candidate_requeue_pending=true
GET  /api/v1/capability-gaps/{gap_id}
POST /api/v1/capability-gaps/{gap_id}/start
POST /api/v1/capability-gaps/{gap_id}/resolve
POST /api/v1/capability-gaps/{gap_id}/requeue-candidates
POST /api/v1/capability-gaps/{gap_id}/reopen
```

所有写接口要求幂等键；更新/状态迁移要求最新 `expected_version`。

```text
open -> in_progress -> resolved
 ^                       |
 +-------- reopen -------+
```

resolve 必须有 `resolution.summary` 或证据。resolved 后：

```json
{
  "expected_version": 3,
  "idempotency_key": "gap-88-requeue-v3",
  "candidate_ids": [1024, 1025]
}
```

requeue 只把 `WAITING_CAPABILITY` 候选放回 `READY_FOR_CANARY`，清除旧资格结论并保留 Gap 追溯；之后必须重新验证。

## 4. 正式用例晋级与 revision

### 先 dry-run

```http
POST /api/v1/testcase-libraries/{library_id}/promotions
```

```json
{
  "candidate_ids": [1024, 1025],
  "expected_library_revision": 17,
  "idempotency_key": "closed-loop-20260812-batch-004",
  "dry_run": true,
  "message": "资格验证通过，准备发布",
  "source_run_ids": [201, 202]
}
```

dry-run 不占用幂等键、不修改候选和正式库。确认 create/update 差异后，重新读取 library revision，再以新的正式发布幂等键提交 `dry_run=false`。

只有状态为 `QUALIFIED/PENDING_PUBLISH`、资格结论为 `QUALIFIED`、项目和目标库一致的候选可发布。成功后正式 testcase、候选 `ACTIVE`、revision 和 promotion 审计在同一事务提交。

冲突处理：

- `PROMOTION_CONFLICT`：重新检查候选状态和归属；
- `LIBRARY_REVISION_CONFLICT`：读取最新 revision，重新 dry-run；
- `IDEMPOTENCY_KEY_CONFLICT`：同一键被不同请求占用，停止并核查。

### 查询与回滚

```http
GET  /api/v1/testcase-libraries/{library_id}/revisions
GET  /api/v1/testcase-libraries/{library_id}/revisions/{revision}
GET  /api/v1/testcase-libraries/{library_id}/promotions
POST /api/v1/testcase-libraries/{library_id}/revisions/{target_revision}/rollback
```

回滚也要求 `expected_library_revision` 和幂等键。它创建新 revision，不删除历史；被移除 promotion 对应的候选回到 `PENDING_PUBLISH`。

## 5. Workflow、资源与测试账号

Workflow Run 的创建、claim、progress、result、retry、审批和取消合同由 `workflow-manager` 管理。每次资格验证或正式消费都创建新 Run，不复用 `blocked/failed/succeeded/cancelled` Run；保存 qualification Run ID 和正式消费 Run 的冻结 library revision/hash/case list。

通用资源：

```http
POST /api/v1/resource-leases/acquire
POST /api/v1/resource-leases/{lease_id}/renew
POST /api/v1/resource-leases/{lease_id}/release
GET  /api/v1/resource-leases?resource_key={key}&status=active
```

```json
{
  "resource_keys": [
    "unity:racinggo:dev2",
    "bridge:racinggo:dev2:ui",
    "device:android-001"
  ],
  "owner_type": "workflow_run",
  "owner_id": "201",
  "controller_run_id": "codex-cycle-20260812-01",
  "priority": 100,
  "ttl_seconds": 900,
  "idempotency_key": "run-201-qualification-resources"
}
```

资源键只用于 `unity:*`、`bridge:*`、`device:*`。同一 group 全有或全无；TTL 为 30～3600 秒；priority 不代表抢占。`account:*` 会被拒绝，测试账号必须使用 `test-account-manager` 的 acquire/keepalive/release。

## 6. Evidence Manifest 与跑后分类

```http
PUT  /api/v1/workflow-runs/{run_id}/evidence-manifest
GET  /api/v1/workflow-runs/{run_id}/evidence-manifest
POST /api/v1/workflow-runs/{run_id}/post-run-analysis
```

Manifest 首次 `expected_version=0`，以后使用回读 revision。默认必需证据：`case_result`、`ui_snapshot`、`console`、`screenshots`，调用方只能追加，不能删除。artifact 只提交 `type/uri/sha256/size/case_id/step_id/metadata`，不得提交 `content/data/base64/blob/raw`。

分类支持：

```text
CONFIRMED_ANOMALY
PRODUCT_BUG_CANDIDATE
AUTOMATION_RISK
PERFORMANCE_RISK
ANALYSIS_INCOMPLETE
NO_RISK_FOUND
```

没有 Manifest 不能分类；Manifest 不完整不能写 `NO_RISK_FOUND`。写接口要求幂等键和最新版本，`finding_ids` 必须属于同一项目且未归档。

## 7. Finding 反馈与规则学习

### Finding 反馈

```http
POST /api/v1/shift-left/findings/{finding_id}/feedback
GET  /api/v1/shift-left/findings/{finding_id}/feedback
```

写入要求幂等键、当前 Finding `from_revision`，以及 note 或 evidence_refs。标签固定为：

```text
true_positive / false_positive / duplicate_bug / automation_unsupported
human_confirmed / human_rejected / fixed_before_validation
```

反馈是不可变事实；错误反馈不要覆盖，按业务规则补充新事实并保留时间线。

### Rule Version 与 replay gate

```http
POST /api/v1/analysis-rules
GET  /api/v1/analysis-rules?project_id={project_id}
GET  /api/v1/analysis-rules/{rule_id}
POST /api/v1/analysis-rules/{rule_id}/versions
POST /api/v1/analysis-rule-versions/{version_id}/replays
POST /api/v1/analysis-rule-versions/{version_id}/activate
POST /api/v1/analysis-rule-versions/{version_id}/retire
```

版本状态：

```text
draft -> replay_passed -> active -> retired
```

规则和版本不可原地覆盖；调整行为或阈值必须新建版本。Replay 必须提供可重放 dataset ref、64 位 SHA-256、原始计数、非空 impact 和 artifact refs。Hub 自行计算 precision/recall/误报率/漏报率。activate 要求 Rule 最新 `expected_version`、目标版本 `replay_passed` 且至少一个 `gate_passed=true` replay。

## 8. 完成核验

闭环完成前至少回读：

1. candidate 为 `ACTIVE` 且绑定正式 testcase/library；
2. promotion 审计和新 library revision 存在；
3. 消费 Run 绑定冻结 revision 与用例数；
4. Evidence Manifest 和跑后 classification 可读；
5. Finding/feedback 与实体关系可追溯；
6. 若本次范围含学习，Rule Version 有 replay 证据，激活状态与用户授权一致；
7. overview 与源对象一致，没有把遗留 blocker 隐藏成完成。
