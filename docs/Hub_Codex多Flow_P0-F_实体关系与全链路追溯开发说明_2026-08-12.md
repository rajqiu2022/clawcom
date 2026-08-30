# Hub Codex 多 Flow：P0-F 实体关系与全链路追溯开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 本阶段目标

P0-F 在不改变现有候选、资格验证、用例晋级和 Flow 启动契约的前提下，增加项目级通用实体关系，使正式用例可以追溯到来源和后续结果，并支持反向查询。

目标链路：

```text
commit / requirement / panorama_node / module / finding
  -> automation_case_candidate
  -> qualification workflow_run
  -> testcase
  -> testcase_library_revision
  -> consuming workflow_run
  -> finding / bug
  -> learned_rule
```

关系记录属于旁路增强：自动接入点使用独立 SAVEPOINT。关系表未迁移、临时不可用或关系写入失败时，原业务事务继续提交，不能阻断候选创建、资格验证、用例晋级或 Workflow Run 创建。

本阶段覆盖 AT-11。

## 2. 数据结构

新增 `entity_relations` 表，每行表示一条有方向的边：

- `project_id`
- `from_type` / `from_id`
- `relation_type`
- `to_type` / `to_id`
- `metadata_json`
- `created_by` / `updated_by`
- `created_at` / `updated_at`
- `relation_key`

自然键由项目、起点、关系类型和终点组成。服务端将自然键规范化后计算 SHA-256，写入唯一 `relation_key`，因此重复投递不会产生重复边；相同自然键携带新 metadata 时更新原记录。

当前支持的实体类型：

- `commit`
- `requirement`
- `panorama_node`
- `module`
- `finding`
- `automation_case_candidate`
- `workflow_run`
- `testcase`
- `testcase_library_revision`
- `capability_gap`
- `bug`
- `learned_rule`
- `test_report`

实体 ID 统一保存为字符串，兼容数据库整数 ID、commit SHA、TAPD Bug ID 和复合 revision ID。

## 3. API

### 3.1 幂等批量写入

`POST /api/v1/entity-relations:batch-upsert`

```json
{
  "project_id": 6,
  "relations": [
    {
      "from_type": "commit",
      "from_id": "8b94c62",
      "relation_type": "source_of",
      "to_type": "automation_case_candidate",
      "to_id": "1024",
      "metadata": {
        "branch": "feature/racinggo-sign-in"
      }
    }
  ]
}
```

单批最多 500 条。服务端先校验整批数据再写入，任一条非法时整批不落库。响应分别返回 `created_count`、`updated_count` 和 `unchanged_count`；并发写入同一自然键时会回滚并重试一次。

### 3.2 单跳双向查询

- `GET /api/v1/entity-relations?project_id=6&from_type=testcase&from_id=301`
- `GET /api/v1/entity-relations?project_id=6&to_type=testcase&to_id=301`

支持组合过滤：

- `relation_type`
- `updated_after`
- `page` / `page_size`，其中 `page_size` 最大 200

为避免无条件扫描全项目关系，至少要提供一组 from/to、`relation_type` 或 `updated_after`。

### 3.3 多跳追溯

`GET /api/v1/entity-relations/trace?project_id=6&entity_type=testcase&entity_id=301&direction=both&max_depth=5`

参数：

- `direction`：`upstream`、`downstream` 或 `both`；
- `max_depth`：1～5，默认 5；
- `max_nodes`：2～500，默认 300。

响应包含根节点、带深度的节点列表、关系边列表和 `truncated` 标记。达到节点上限时停止扩展，调用方可提示用户缩小方向或查询范围。

所有接口都按 `project_id` 做权限校验，用户和自定义 Agent 不能跨项目读取或写入关系。

## 4. 已接入的自动关系

### 4.1 候选来源

候选创建时，以及更新 `source_refs` 或 `module_key` 时，自动补充：

- `commit/requirement/panorama_node/finding -> candidate`：`source_of`
- `module -> candidate`：`source_of`

### 4.2 资格验证

保存资格验证结果时自动补充：

- `candidate -> qualification workflow_run`：`qualified_by`
- `candidate -> capability_gap`：`blocked_by`

### 4.3 正式用例晋级

promotion 原子提交时自动补充：

- `candidate -> testcase`：`promoted_to`
- `testcase -> testcase_library_revision`：`included_in`

metadata 记录用例库、revision、promotion 和 content hash，便于审计晋级批次。

### 4.4 Flow 用例快照

Workflow Run 冻结用例库快照时自动补充：

- `testcase -> workflow_run`：`consumed_by`
- `testcase_library_revision -> workflow_run`：`consumed_by`

metadata 记录冻结的 revision、content hash 和 snapshot ID。用例库后续升级不会改写历史 Run 的关系和快照。

### 4.5 跑后分析、Bug 和规则

P0-F 提供通用批量 API，由报告/跑后分析环节按业务事实写入，例如：

- `workflow_run -> finding`：`produced`
- `finding -> bug`：`reported_as`
- `bug -> learned_rule`：`learned_into`

这些业务对象的创建流程无需直接依赖关系表；关系可以在同一流程中写入，也可以由控制器幂等补写。

## 5. 部署顺序

1. 备份数据库；
2. 执行 `ops/migrations/20260812_entity_relations.sql`；
3. 确认 `entity_relations` 表、唯一键和 from/to 查询索引存在；
4. 部署 Hub 应用代码；
5. 用测试项目调用 batch-upsert 两次，确认第二次为 `unchanged_count=1`；
6. 新建候选并晋级，确认从正式 testcase 可追溯到 candidate 和来源；
7. 启动带用例库的 Workflow Run，确认存在 `consumed_by` 关系；
8. 最后让报告、Bug 和学习规则流程接入通用批量 API。

迁移只新增表，不修改现有表和 Workflow Definition #12。部署应用前先建表可避免自动关系首次写入告警；即便建表延迟，旁路保护也会让原业务继续运行。

## 6. 回退策略

- 回退应用代码时可保留 `entity_relations` 表，旧代码不会读取该表；
- 关系数据是派生索引，不是候选、用例、Run、Bug 或规则的唯一事实源；
- 自动接入失败后，可以根据原业务记录通过 batch-upsert 幂等回填；
- 不建议回退时删除关系表，避免丢失已经形成的审计链路；
- 如果必须删除，先导出 `entity_relations`，再停用所有关系写入入口。

## 7. 自动化验证

`tests/test_entity_relations_api.py` 覆盖：

- 批内去重、重复请求幂等和 metadata 更新；
- 整批校验失败时零写入；
- from/to 双向分页查询；
- AT-11：从正式用例五跳追溯来源 commit、模块、资格验证 Run、消费该用例的 Flow Run、跑后 Finding、Bug 和规则，并从规则反向回到正式用例；
- 模拟关系表缺失，验证候选创建不被旁路关系写入阻断。

## 8. 下一阶段建议

P0-G 可接入跑后证据完整性和 Finding 状态语义，落实 AT-10：当 Console、关键截图等证据不完整时，只能写入 `ANALYSIS_INCOMPLETE`，不能误标为 `NO_RISK_FOUND`。同时由报告环节使用本阶段 API 建立 `workflow_run -> finding -> bug -> learned_rule` 的真实业务关系。
