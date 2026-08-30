# Hub Codex 多 Flow：P0-D Flow 用例库快照开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

在 Workflow Run 创建时冻结其使用的正式用例库 revision、content hash 和完整用例内容。后续正式库继续 promotion 或回滚时，本次 Run 的展示、Agent 任务上下文及用例读取接口仍固定指向启动时版本。

本阶段实现需求中的 `workflow_run_library_snapshots` 和 AT-06，同时遵守 AT-07 的稳定性边界：快照能力异常只能产生 warning，不能阻断原有 Flow 创建和执行。

## 2. 兼容性设计

- 未传用例库参数的 Workflow 完全走旧链路。
- 兼容 `test_case_library_id`、`testcase_library_id` 和历史 `library_id` 三种参数名；可位于请求顶层、`start_vars` 或 context。
- 多个参数指向不同库、参数非法、库不存在或项目不匹配时，Run 仍创建成功，并在 `context.warnings` 写入结构化告警。
- 新表尚未迁移、数据库写入失败或正式 revision 内容异常时，使用 SAVEPOINT 回滚快照子操作，Run 和 Step 主事务继续提交。
- 不修改 Definition #12，不修改现有节点状态机、worker claim、心跳、结果回写及报告逻辑。
- Run 快照不对 `library_id` 建外键。即使源用例库以后删除，历史 Run 的冻结内容仍可读取。

## 3. 数据结构

新增 `workflow_run_library_snapshots`：

| 字段 | 说明 |
| --- | --- |
| `workflow_run_id` | Run ID，一条 Run 最多一份用例库快照 |
| `library_id` | 启动时正式库 ID，仅作历史标识 |
| `library_revision` | 启动时正式 revision |
| `snapshot_version` | 与正式 revision 对齐的快照版本 |
| `content_hash` | 正式 revision 的 SHA-256 hash |
| `case_ids_json` | 可执行用例 ID；排除目录占位用例 |
| `case_count` | 可执行用例数量 |
| `snapshot_json` | 启动时完整用例和脑图内容的独立副本 |
| `frozen_by/frozen_at` | 冻结操作人和时间 |

`workflow_run_id` 唯一，删除 Run 时删除对应快照。用例库删除不会删除 Run 快照。

## 4. Run 创建行为

调用：

```json
POST /api/v1/workflow-runs
{
  "workflow_definition_id": 12,
  "idempotency_key": "flow12-20260812-2200",
  "start_vars": {
    "test_case_library_id": 33
  }
}
```

成功响应新增：

```json
{
  "id": 118,
  "testcase_library_snapshot": {
    "workflow_run_id": 118,
    "library_id": 33,
    "revision": 18,
    "snapshot_version": 18,
    "content_hash": "sha256:...",
    "case_ids": [33001, 33002],
    "case_count": 2,
    "frozen_at": "...",
    "cases_api": "/api/v1/workflow-runs/118/testcase-library-snapshot"
  }
}
```

同一份元数据也写入 `context.testcase_library_snapshot`，因此：

- Run 详情和列表可直接展示；
- worker 拉取/claim 的 Step 上下文可直接读取；
- sidecar 的 `workflow_agent_task` payload 可直接读取；
- Agent 应通过 `cases_api` 获取冻结用例，不能再按 `library_id` 查询当前正式库替代。

相同 Run 幂等请求重放返回原 Run 和原快照，不会重复生成。

## 5. 快照读取 API

`GET /api/v1/workflow-runs/{run_id}/testcase-library-snapshot`

默认返回元数据、脑图和冻结的可执行用例。传 `include_cases=false` 时只返回元数据。

返回内容来自 `workflow_run_library_snapshots.snapshot_json`，不会读取用例库当前内容。

## 6. 非阻断告警

快照失败时，Run 响应示例：

```json
{
  "context": {
    "warnings": [{
      "scope": "testcase_library_snapshot",
      "code": "TESTCASE_LIBRARY_SNAPSHOT_FAILED",
      "message": "Failed to freeze testcase library; Run continues with its legacy execution path",
      "details": {
        "library_id": 33,
        "error_type": "OperationalError"
      }
    }]
  }
}
```

可能的 warning code：

- `INVALID_TESTCASE_LIBRARY_ID`
- `TESTCASE_LIBRARY_ID_CONFLICT`
- `TESTCASE_LIBRARY_SNAPSHOT_NOT_FOUND`
- `TESTCASE_LIBRARY_PROJECT_MISMATCH`
- `TESTCASE_LIBRARY_SNAPSHOT_FAILED`

warning 表示此次 Run 没有可靠冻结版本，执行方可继续旧链路，但报告和看板应明确标识该风险。

## 7. 部署顺序

1. 先完成 P0-B、P0-C 迁移；
2. 初始化正式库 #33 revision；
3. 执行：

   ```sql
   ops/migrations/20260812_workflow_run_library_snapshots.sql
   ```

4. 部署应用代码；
5. 使用测试 Definition 创建绑定 #33 的 Run；
6. 确认 Run 响应中的 revision/hash/case count 与 #33 当前正式 revision 一致；
7. promotion #33 后再次创建 Run，确认新 Run 使用新 revision、旧 Run 仍返回旧内容；
8. 再逐步接入 Definition #12 的执行方。

应用代码先于迁移短暂上线也不会阻断 Run：已增加真实“表不存在”的自动化验证。但推荐仍按迁移优先顺序部署，避免产生没有冻结版本的 Run。

回退应用代码时可保留新表；旧代码不会读取它。删除 Run 时数据库外键 `ON DELETE CASCADE` 仍会清理快照。

## 8. 自动化验证

新增 `tests/test_workflow_run_library_snapshots_api.py`，覆盖：

- AT-06：正式库从 revision 0 升级到 1 后，旧 Run 保持 revision 0 的 case/hash，新 Run 使用 revision 1；
- Run、Step/Agent payload 和 cases API 返回同一冻结契约；
- 幂等重放只生成一份快照；
- 快照逻辑抛错不阻断 Run；
- 数据表未迁移的真实 `OperationalError` 不阻断 Run；
- 无用例库参数的旧 Flow 行为不变；
- 非法参数只产生 warning；
- 删除 Run 同步删除快照。

## 9. 下一阶段

P0-E 实现资源租约 `resource_leases`：通用租约管理 Unity、Bridge 和设备；测试账号继续复用 `test-account-manager`，只加固原子领用、TTL、续租和超时回收。对应 AT-08、AT-09，并确保 Flow12 的资源优先级高于外围资格验证。
