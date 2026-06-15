# 用例库与功能全景关联设计

日期：2026-06-15

## 背景

用例库需要和功能全景形成双向关联：

- 功能全景节点可以展示覆盖它的用例库、用例目录和用例数量。
- 用例库可以展示关联的功能全景模块、风险、测试重点和代码路径。
- 关联不是必填项，未关联时保持为空。
- 用例删除、用例库删除、功能全景模块删除后，另一侧不能继续展示无效关联。
- 功能全景需要新增一个测试覆盖/bug 风险视图，节点大小由关联用例数决定，颜色由 bug 风险系数决定。
- Agent 需要能按时间段快速查询某个用例库最近变更的用例，用于评审或测试。

现状：

- `GameModulePanorama.related_case_libraries` 已有 JSON 字段，但只适合轻量展示，不适合作为正式双向关系。
- `TestCase` 已有 `created_at`、`updated_at`、`updated_by`，但缺少可审计的增量变更日志。
- `TestCaseSnapshot` 已支持整库快照、diff、checkout，适合版本回滚，不适合快速查“最近某时间段修改了哪些用例”。

## 目标

1. 新增正式关系表，支持功能全景模块与用例库/目录/用例的双向关联。
2. 删除任一侧资源时，在删除事务内同步清理关联，采用硬删除；审计信息写入变更日志。
3. 提供主动同步接口和页面按钮，用于清理历史脏数据、重算统计和兜底恢复。
4. 新增测试覆盖/bug 风险指标，使功能全景拓扑可以按“关联用例数 + bug 风险”渲染。
5. 新增用例变更日志，支持按时间段、目录、操作者、功能模块查询变更用例。

## 非目标

- 不在第一版强制每条用例都必须关联功能模块。
- 不用页面刷新触发全量同步。
- 不用软删除保留关联。正常关联查询只保留有效关系；历史审计走日志表。
- 不替换现有 `TestCaseSnapshot` 版本能力，快照仍用于整库回滚和版本 diff。

## 数据模型

### `testcase_panorama_links`

功能全景模块与用例资产的正式关联表。

建议字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `project_id` | 项目 ID |
| `workspace_id` | 功能全景工作区 ID |
| `module_id` | `game_module_panorama.id` |
| `library_id` | `test_case_libraries.id` |
| `module_path` | 用例目录路径，可空 |
| `case_pk` | `test_cases.id`，可空，第一版预留 |
| `link_level` | `library` / `directory` / `case` |
| `case_count` | 当前关联范围内用例数缓存 |
| `source` | `manual` / `agent` / `migration` |
| `confidence` | 置信度，默认 1 |
| `created_by` / `updated_by` | 操作者 |
| `created_at` / `updated_at` | 时间 |
| `last_verified_at` | 最近同步校验时间 |

唯一约束建议：

- 库级：`workspace_id + module_id + library_id + link_level`
- 目录级：`workspace_id + module_id + library_id + module_path + link_level`
- 用例级：`workspace_id + module_id + case_pk + link_level`

第一版实现库级和目录级；用例级字段先保留。

### `panorama_module_test_metrics`

功能全景测试覆盖与 bug 风险聚合指标表。

| 字段 | 说明 |
| --- | --- |
| `module_id` | 功能全景模块 ID |
| `workspace_id` | 工作区 ID |
| `project_id` | 项目 ID |
| `direct_case_count` | 当前节点直接关联用例数 |
| `subtree_case_count` | 当前节点及全部子节点关联用例数 |
| `linked_library_count` | 关联用例库数量 |
| `linked_directory_count` | 关联目录数量 |
| `bug_count` | bug 数，默认风险来源 |
| `bug_risk_score` | bug 风险分，Agent 可提交 |
| `bug_risk_level` | `low` / `normal` / `high` / `critical` |
| `metrics_payload` | 扩展字段 |
| `synced_at` | 最近统计时间 |

`subtree_case_count` 允许重复计算多份用例库或重复目录，不做跨库去重。

### `test_case_change_logs`

用例增量变更日志表。

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `library_id` | 用例库 ID |
| `case_pk` | `test_cases.id`，删除后保留历史值 |
| `case_id` | 用例业务编号 |
| `case_title` | 用例标题快照 |
| `module_path` | 变更时目录 |
| `change_type` | `created` / `updated` / `deleted` / `moved` / `batch_created` / `batch_deleted` |
| `changed_fields` | JSON 字段列表 |
| `old_snapshot` | 修改前关键数据 |
| `new_snapshot` | 修改后关键数据 |
| `operation_id` | 批量操作 ID |
| `linked_panorama_modules` | 变更发生时关联的功能模块快照 |
| `changed_by` | 操作者 |
| `changed_at` | 变更时间 |
| `source` | `web` / `agent` / `import` / `ai` / `rollback` |

## 删除同步策略

采用方案 A：**硬删除关联 + 写变更日志审计**。

### 删除用例

在 `DELETE /testcase-libraries/{library_id}/cases/{case_id}` 同一事务中：

1. 读取用例快照和当前关联模块快照。
2. 写 `test_case_change_logs(change_type='deleted')`。
3. 删除 `testcase_panorama_links` 中 `link_level='case' and case_pk=<case>` 的记录。
4. 重新计算该用例所在目录、用例库关联的 `case_count`。
5. 更新受影响模块的 `panorama_module_test_metrics`。

第一版若未启用用例级关联，则步骤 3 没有记录可删，但仍写变更日志和重算目录统计。

### 批量删除用例

同一 `operation_id` 记录多条 `test_case_change_logs`。批量删除后批量重算受影响的库、目录和模块指标。

### 删除用例库

在删除用例库事务中：

1. 写库级删除操作日志。
2. 删除该 `library_id` 下所有 `testcase_panorama_links`。
3. 更新受影响模块指标。

### 删除功能全景模块

当前 `delete_panorama_module()` 已递归删除子模块、关系边、代码关联和变更记录。扩展时需要：

1. 递归收集被删除模块 ID。
2. 删除 `testcase_panorama_links.module_id in (...)`。
3. 删除对应 `panorama_module_test_metrics`。
4. 兼容清理旧 `related_case_libraries` 中的残留展示数据。
5. 提交事务后，用例库侧不再展示已删除模块。

## 主动同步与定时兜底

新增接口：

```http
POST /api/v1/testcase-panorama-links/sync
```

请求示例：

```json
{
  "project_id": 6,
  "workspace_id": 12,
  "library_id": 20,
  "dry_run": false
}
```

同步职责：

- 删除不存在的模块、用例库、目录、用例关联。
- 重算 `case_count`。
- 重算 `panorama_module_test_metrics`。
- 返回清理和重算摘要。

页面行为：

- 功能全景详情和用例库详情提供“同步关联”按钮。
- 页面刷新只读取现成数据，不触发全量同步。
- 可增加每日定时任务作为兜底。

## API 设计

### 关联 CRUD

```http
GET    /api/v1/testcase-panorama-links?library_id=&module_id=&workspace_id=
POST   /api/v1/testcase-panorama-links
PUT    /api/v1/testcase-panorama-links/{id}
DELETE /api/v1/testcase-panorama-links/{id}
```

创建目录级关联：

```json
{
  "workspace_id": 12,
  "module_id": 88,
  "library_id": 20,
  "module_path": "技能/连招",
  "link_level": "directory",
  "source": "manual"
}
```

### 功能全景侧查询

```http
GET /api/v1/panorama/modules/{module_id}/testcase-links?include_children=1
GET /api/v1/panorama/modules/{module_id}/test-metrics?include_children=1
POST /api/v1/panorama/modules/{module_id}/test-metrics
```

Agent 提交 bug 风险：

```json
{
  "bug_count": 8,
  "bug_risk_score": 72,
  "bug_risk_level": "high",
  "source": "agent",
  "evidence": {
    "tapd_bug_ids": ["123", "456"],
    "window": "last_30_days"
  }
}
```

### 用例库侧查询

```http
GET /api/v1/testcase-libraries/{id}/panorama-links
GET /api/v1/testcase-libraries/{id}/changes?since=&until=&module_path=&changed_by=
GET /api/v1/panorama/modules/{module_id}/testcase-changes?since=&until=&include_children=1
```

## 前端设计

### 功能全景

节点详情：

- 关联用例库、目录、用例数量。
- 直接关联用例数。
- 含子模块总用例数。
- bug 数、风险分、风险等级。
- 最近统计时间。
- “同步关联”按钮。

拓扑图新增指标模式：

- `模块结构视图`：保留现状，大小按子模块数，颜色按 `risk_level`。
- `测试覆盖/风险视图`：大小按 `subtree_case_count`，颜色按 `bug_risk_score` 或 `bug_count`。

### 用例库

用例库详情：

- 顶部展示关联功能模块数量。
- 目录节点旁展示功能模块 badge。
- 目录详情展示模块路径、风险、测试重点、代码路径。
- “同步关联”按钮。

新增“最近变更”视图：

- 时间范围：今天、近 7 天、自定义。
- 筛选：目录、操作者、变更类型、关联功能模块。
- 可跳转用例详情。
- 后续可一键发起评审。

## 与现有字段兼容

`GameModulePanorama.related_case_libraries` 保留兼容：

- 读取模块详情时优先返回正式 link 表聚合结果。
- 旧字段只作为迁移前兼容展示和 fallback。
- 新增/编辑关联只写 `testcase_panorama_links`。
- 同步接口可将旧 JSON 迁移为正式 link 表记录。

## Skill 与 Hub 同步要求

功能实现完成并部署后，必须同步更新对应 Agent Skill 到 Hub 系统，避免 Agent 仍按旧协议操作。

需要更新：

| Skill | 更新内容 |
| --- | --- |
| `testcase-manager` | 新增用例库 ↔ 功能全景关联 API、最近变更查询 API、按时间段筛选变更用例、从变更发起评审的推荐工作流 |
| `game-module-panorama`（市场 Skill #179） | 新增功能模块关联用例库/目录、同步关联接口、测试覆盖/bug 风险指标、测试覆盖/风险拓扑视图、模块维度查询最近变更用例 |

同步要求：

1. 本地更新 `openclaw-agent/skills/testcase-manager/SKILL.md` 和 `openclaw-agent/skills/game-module-panorama/SKILL.md`。
2. 通过脚本或远端 Python 更新 Hub DB `skills.template_content`。
3. 同步远端磁盘副本：
   - `/opt/openclaw-web/openclaw-agent/skills/<skill-name>/SKILL.md`
   - `/opt/openclaw-web/hub-store/skills/<skill-name>/SKILL.md`
4. 保持 `review_status='approved'`。
5. 验证 Hub 市场中对应 Skill 已包含新 API 关键词，例如：
   - `testcase-panorama-links`
   - `testcase-changes`
   - `test-metrics`
   - `bug_risk_score`
6. 通知已安装相关 Skill 的 Claw 重新 sync skills。

这一步作为上线验收项，不能只部署 Web/API 而遗漏 Skill。

## 分期

### P0：正式关联与删除同步

- 新增 `testcase_panorama_links`。
- 关联 CRUD API。
- 功能全景和用例库双向展示。
- 删除用例库、删除功能模块时硬删除关联。
- 主动同步 API 和按钮。
- 兼容旧 `related_case_libraries`。

### P1：测试覆盖/bug 风险视图

- 新增 `panorama_module_test_metrics`。
- 同步时计算 `direct_case_count`、`subtree_case_count`。
- Agent 可提交 `bug_count`、`bug_risk_score`。
- 功能全景拓扑新增测试覆盖/风险视图。

### P2：用例变更日志

- 新增 `test_case_change_logs`。
- 覆盖创建、更新、删除、批量创建、批量删除、目录移动。
- 支持按时间段和模块查询变更用例。
- 用例库页面新增最近变更视图。

### P3：评审与影响分析联动

- 从最近变更一键发起用例评审。
- 从功能全景节点查看相关变更用例。
- 影响分析推荐用例库时改读正式 link 表和 test metrics。

## 测试计划

- 单测：关联表唯一约束、同步清理、指标聚合、变更日志生成。
- API 测试：关联 CRUD、同步 dry-run、模块删除清理、用例库删除清理。
- 前端手测：功能全景节点详情、用例库目录 badge、拓扑视图切换。
- 回归：现有 `related_case_libraries` 展示不破坏；功能全景 graph、impact、snapshot 仍可用。

