# Hub × Codex 多 Flow P0 端到端联调与灰度验收

## 1. 阶段结论

P0-A～P0-J 的 AT-01～AT-12 已整理成可重复执行的验收套件。当前开发环境的内存数据库验收结果为 **13 passed**；其中 AT-03 同时验证“产生 Gap”和“禁止 promotion”，因此总用例数比 AT 编号多一条。

新增的 AT-07 跨阶段场景同时制造以下外围异常：

- 代码分析 Run 失败；
- 候选停留在 `WAITING_CAPABILITY`；
- Capability Gap 未解决；
- 分析规则 replay gate 未通过。

在这些对象同时存在时，Flow12 仍能通过原有 `POST /api/v1/workflow-runs` 创建，Definition 未改变，外围对象也未被 Flow12 反向修改。

这证明当前实现满足“外围闭环可增强，但不能成为原 Flow 的强依赖”。

## 2. AT-01～AT-12 覆盖矩阵

| 验收项 | 自动化测试 | 结果 |
| --- | --- | --- |
| AT-01 Run 创建幂等 | `test_workflow_run_control_plane_api.py::...::test_at01_create_run_is_idempotent_per_definition` | 通过 |
| AT-02 候选乐观锁 | `test_automation_case_candidates_api.py::...::test_at02_stale_expected_version_is_rejected` | 通过 |
| AT-03 能力不足和禁止入库 | 候选 API 与 promotion API 两条 `test_at03_*` | 通过 |
| AT-04 promotion 重放只发布一次 | `test_at04_idempotent_replay_publishes_once` | 通过 |
| AT-05 两个发布者 revision 冲突 | `test_at05_stale_publisher_loses_and_can_dry_run_again` | 通过 |
| AT-06 Flow12 冻结 revision | `test_at06_run_keeps_frozen_revision_after_library_upgrade` | 通过 |
| AT-07 外围失败不阻断 Flow12 | `test_at07_peripheral_failures_do_not_block_or_mutate_flow12` | 通过 |
| AT-08 资源冲突不改变 Flow12 | `test_at08_flow12_lease_blocks_qualification_without_changing_run` | 通过 |
| AT-09 TTL 回收和过期审计 | `test_at09_expired_group_is_reclaimed_and_history_is_preserved` | 通过 |
| AT-10 证据不足不能写无风险 | `test_at10_incomplete_evidence_rejects_no_risk_and_accepts_incomplete` | 通过 |
| AT-11 正反向完整链路 | `test_at11_full_lineage_from_production_case_and_reverse` | 通过 |
| AT-12 回滚产生新 revision | `test_at12_rollback_creates_new_revision_and_preserves_history` | 通过 |

统一执行入口：

```powershell
ops\run_multiflow_acceptance.ps1
```

同时执行全量回归：

```powershell
ops\run_multiflow_acceptance.ps1 -FullRegression
```

## 3. 只读部署前检查

新增：

```powershell
python ops/verify_multiflow_readiness.py `
  --project-id <灰度项目ID> `
  --library-id 33 `
  --workflow-definition-id 12
```

机器读取建议使用 JSON：

```powershell
python ops/verify_multiflow_readiness.py `
  --project-id <灰度项目ID> `
  --library-id 33 `
  --workflow-definition-id 12 `
  --json
```

检查器不导入 Flask 应用、不执行自动迁移、不更新 revision、不回收租约。它只执行以下读取：

- 11 个迁移文件是否存在；
- 20 张闭环相关表及关键列是否齐全；
- `SHIFT_LEFT_ENABLED` 是否开启；
- 灰度项目、#33、Flow12 是否存在且项目归属一致；
- #33 当前 revision/hash 是否有完全一致的正式快照；
- 是否存在过期但尚未惰性回收的 active 租约。

返回规则：

- 有 `fail`：退出码 1，不允许进入灰度切流；
- 只有 `warn`：退出码 0，可继续，但必须人工确认警告；
- 全部 `pass`：数据库和种子对象满足切流前置条件。

最终启用 Evidence/Finding/Developer AI 协作前增加严格参数：

```powershell
python ops/verify_multiflow_readiness.py `
  --project-id <灰度项目ID> `
  --library-id 33 `
  --workflow-definition-id 12 `
  --require-shift-left
```

此时 `SHIFT_LEFT_ENABLED=0` 会从警告升级为失败并返回退出码 1。

## 4. 当前本机检查结果

2026-08-12 对当前开发机执行检查：

- 迁移文件：PASS，11/11 齐全；
- 数据库 schema：FAIL；
- `SHIFT_LEFT_ENABLED`：WARN，当前关闭。

本机 `web/config.py` 没有读取到 MySQL 配置，因此检查对象是 2026-03-25 的旧本地 SQLite 数据库。该库没有当前 Hub 主表和 P0 新表，结果为 `NOT READY` 符合预期；这不是线上环境检查结论。

生产/灰度服务器必须在其实际 `.env` 和 Python 环境下重新运行检查，不能复用本机结果。

## 5. 灰度部署顺序

### 5.1 部署前

1. 冻结本次发布文件清单和 Git commit；
2. 备份生产数据库；
3. 记录 #33 当前用例数、revision/hash；
4. 记录 Flow12 Definition JSON/hash，确认本次发布不修改 Definition #12；
5. 先执行 preflight，保存预期的缺表报告；
6. 执行一键 AT-01～AT-12 和全量回归。

### 5.2 数据库迁移

按以下顺序执行：

1. `20260804_workflow_operation_idempotency.sql`
2. `20260810_shift_left_mvp_a.sql`
3. `20260812_workflow_run_control_plane.sql`
4. `20260812_automation_case_candidates.sql`
5. `20260812_testcase_library_promotions.sql`
6. `20260812_workflow_run_library_snapshots.sql`
7. `20260812_resource_leases_and_test_account_ttl.sql`
8. `20260812_entity_relations.sql`
9. `20260812_workflow_evidence_manifests.sql`
10. `20260812_capability_gap_lifecycle.sql`
11. `20260812_finding_feedback_analysis_rules.sql`

迁移全部为加字段或加新表，不要求删除旧数据。

### 5.3 初始化 #33

迁移完成后、开放 promotion 前执行：

```powershell
python ops/initialize_testcase_library_revision.py --library-id 33
```

记录输出的 `revision`、`case_count` 和 `content_hash`，再运行 preflight。必须确认 `seed_objects=PASS`。

### 5.4 代码灰度

1. 先保持 `SHIFT_LEFT_ENABLED=0` 部署代码；
2. 通过旧调用方式启动一次隔离 Flow12，确认创建、派发和完成均正常；
3. 验证带幂等键的新 Run 创建，但不切换所有控制器；
4. 验证候选、租约、promotion、快照和实体追溯；
5. 在灰度实例开启 `SHIFT_LEFT_ENABLED=1`；该开关当前是实例级而不是项目级，优先使用独立灰度实例。若只能使用共享实例，开启后所有有项目权限的用户都会看到相关 API，必须依靠现有项目权限和调用方白名单控制范围；
6. 验证 Evidence Manifest、Finding 反馈、Developer AI 临时协作和规则回放；
7. 打开 `/automation-closed-loop`，逐项与源 API/数据库核对；
8. 观察至少一个完整 Flow12 周期后再扩大调用方范围。

## 6. 灰度停止条件

出现任一情况立即停止扩大流量：

- Flow12 旧创建接口返回率或派发成功率下降；
- Definition #12 JSON/hash 发生非预期变化；
- #33 revision 增长但没有对应 promotion/rollback 审计；
- 同一幂等键产生多个 Run、testcase 或 replay；
- 无项目权限用户能读取其他项目对象；
- 证据不完整却出现 `NO_RISK_FOUND`；
- 资源冲突修改或终止了持有租约的 Flow12；
- preflight 出现新的 `fail`。

## 7. 回滚方案

1. 将 `SHIFT_LEFT_ENABLED` 设回 `0`，先关闭临时协作和跑后分析入口；
2. 停止 Codex/DeepFlow 使用新控制 API，回退到旧 Run 创建调用；
3. 回滚 Hub 应用代码；
4. 保留新增表和可空字段，不执行破坏性数据库回滚；
5. 不删除已产生的 revision、snapshot、relation、lease event、Finding feedback 或 replay；
6. 如 #33 内容需要业务回滚，调用正式 rollback API 产生新 revision，禁止直接删除历史 revision；
7. 用旧调用方式启动隔离 Flow12，确认恢复后再解除事故状态。

## 8. 尚未执行的外部动作

- 尚未在生产/灰度服务器执行迁移；
- 尚未初始化线上 #33 revision；
- 尚未修改线上 `SHIFT_LEFT_ENABLED`；
- 尚未部署或切换 DeepFlow/Codex 调用方；
- 尚未提交当前工作区代码。

这些动作需要进入正式部署阶段后，使用服务器实际环境逐步执行。
