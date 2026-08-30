# Hub Codex 多 Flow：P0-C 用例库晋级与版本控制开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

在不修改现有用例 CRUD、旧快照 API 和 Flow #12 Definition 的前提下，增加候选用例进入正式库的唯一原子发布入口，并为正式库建立可精确回读、不可变和可回滚的 revision 链。

本阶段覆盖需求文档中的：

- `test_case_libraries.revision/content_hash`；
- `testcase_library_promotions`；
- 完整 revision 快照；
- promotion 幂等、乐观锁、数据库写锁和 dry-run；
- AT-03、AT-04、AT-05、AT-12。

## 2. 兼容性边界

- 不修改现有用例创建、编辑、删除和旧 `testcase_snapshots` 接口。
- 不修改 Workflow Definition #12，也不改变任何 Flow Run 的创建或执行逻辑。
- 新表和新字段都是增量结构；未调用新接口时没有业务行为变化。
- 新服务发现旧 CRUD 在最后一个正式 revision 后修改了内容时，会先登记一个 `legacy_sync` revision，再要求调用方按最新 revision 重新 dry-run，避免旧入口导致静默覆盖。
- 正式 revision 保存用例库脑图和全部 testcase 持久化内容字段；库名称、负责人、评审状态等管理字段不参与内容 hash，也不会被内容回滚覆盖。

## 3. 新数据结构

### 3.1 `test_case_libraries`

- `revision`：当前正式内容版本，初始为 0；
- `content_hash`：当前完整内容的 `sha256:` hash。

### 3.2 `testcase_library_revisions`

不可变全量快照，唯一键为 `(library_id, revision)`。记录：

- 完整用例内容和脑图；
- content hash、case count；
- 来源类型：`baseline`、`legacy_sync`、`promotion`、`rollback`；
- 操作人、来源操作和回滚目标 revision。

### 3.3 `testcase_library_promotions`

同时承担原子变更审计和幂等结果存储。唯一键为 `(library_id, idempotency_key)`，记录 promotion/rollback 的前后 revision、候选、创建/更新用例、来源 Run、响应结果和操作人。

## 4. API

### 4.1 候选原子晋级

`POST /api/v1/testcase-libraries/{library_id}/promotions`

```json
{
  "candidate_ids": [1001, 1002],
  "expected_library_revision": 17,
  "idempotency_key": "closed-loop-20260812-batch-004",
  "dry_run": false,
  "message": "资格验证通过，发布外围冒烟用例",
  "source_run_ids": [201, 202]
}
```

行为：

1. 对库记录和候选记录加数据库写锁；
2. 校验候选属于同项目/同库、状态为 `QUALIFIED` 或 `PENDING_PUBLISH`，且资格结果为 `QUALIFIED`；
3. 校验 `expected_library_revision`；
4. 同一事务内创建或更新 testcase、绑定候选、候选转为 `ACTIVE`、增加事件、生成完整 revision 和 promotion 审计；
5. 任意一步失败整体回滚；
6. 相同幂等键和相同请求返回第一次结果，幂等键对应不同请求返回 409；
7. `dry_run=true` 只返回 create/update 差异，不创建用例、不修改候选、不占用幂等键。

主要冲突码：

- `PROMOTION_CONFLICT`：候选不满足发布条件；
- `LIBRARY_REVISION_CONFLICT`：库版本已变化；
- `IDEMPOTENCY_KEY_CONFLICT`：幂等键被不同请求占用。

### 4.2 revision 查询

- `GET /api/v1/testcase-libraries/{library_id}/revisions`
- `GET /api/v1/testcase-libraries/{library_id}/revisions?content_hash=sha256:...`
- `GET /api/v1/testcase-libraries/{library_id}/revisions/{revision}`
- `GET /api/v1/testcase-libraries/{library_id}/revisions/{revision}/cases/{case_pk}`

单 revision 默认返回完整快照；可传 `include_snapshot=false` 只读元数据。

### 4.3 promotion/rollback 审计查询

`GET /api/v1/testcase-libraries/{library_id}/promotions`

支持 `operation_type=promotion|rollback`、`page` 和 `per_page`。

### 4.4 回滚

`POST /api/v1/testcase-libraries/{library_id}/revisions/{target_revision}/rollback`

```json
{
  "expected_library_revision": 19,
  "idempotency_key": "rollback-20260812-001",
  "message": "回退错误发布"
}
```

回滚不会删除或修改历史 revision，而是把目标内容恢复到正式库并创建新的 revision。例如 19 回滚到 18 后得到 revision 20，revision 18 和 19 均保持不变。

若回滚移除了后续 promotion 创建的 testcase，对应候选会从 `ACTIVE` 回到 `PENDING_PUBLISH`、解除正式用例绑定并记录 `promotion_rolled_back` 事件，之后可修订并重新发布。

## 5. 迁移和启用

建议顺序：

1. 备份数据库；
2. 执行 `ops/migrations/20260812_automation_case_candidates.sql`（若 P0-B 尚未迁移）；
3. 执行 `ops/migrations/20260812_testcase_library_promotions.sql`；
4. 在应用运行环境执行：

   ```bash
   python ops/initialize_testcase_library_revision.py --library-id 33
   ```

5. 确认输出的 `revision=0`、`case_count` 和 `content_hash`；
6. 部署应用代码并先调用 dry-run；
7. 观察无误后再开放实际 promotion。

初始化脚本幂等，只登记当前内容，不修改用例和旧快照。即使未主动运行，第一次 revision API 调用也会惰性完成 baseline 登记。

回退应用代码前不必删除新增表/字段；旧代码不会读取它们。已完成的正式 promotion 会真实写入用例库，因此业务内容回退应使用 revision rollback，而不是直接回退数据库结构。

## 6. 自动化验证

新增 `tests/test_testcase_library_promotions_api.py`，覆盖：

- AT-03：能力缺口候选不能进入正式库，revision 不变；
- dry-run 不修改正式内容和候选；
- AT-04：重复 promotion 只产生一个 testcase、一条 promotion 和一个发布 revision；
- AT-05：相同 revision 的后写者返回 409，并可基于最新版本重新 dry-run；
- 按 revision、case ID 和 hash 精确回读；
- AT-12：回滚产生新 revision，旧快照保持不变。

本阶段相关回归测试同时覆盖 P0-A Run 控制面、P0-B 候选中心以及用例库删除依赖清理。

## 7. 下一阶段

P0-D 建议实现 `workflow_run_library_snapshots`：Flow #12 启动时冻结用例库 revision/hash/用例清单，执行中即使用例库继续晋级，本次 Run 仍严格使用启动时版本，从而完成 AT-06。
