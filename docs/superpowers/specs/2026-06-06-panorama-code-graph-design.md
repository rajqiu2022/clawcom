# 功能全景代码图谱增强设计

## 背景

当前功能全景已经支持游戏功能模块树、风险等级、代码/资源路径、关联用例库、变更日志和前端拓扑视图。它适合展示“功能域视角”，但关系边主要来自 `parent_id`、`extra.cross_module`、`extra.dependencies`，没有后端正式关系模型，也没有“代码变更 -> 功能模块 -> 用例库/测试建议”的自动影响分析。

`code-review-graph` 的核心价值是把代码解析成持久图谱，通过影响半径、hub/bridge、知识缺口和最小上下文减少审查成本。本设计借鉴其图谱能力，但不整套引入；OpenClaw 的功能全景仍以游戏功能域和测试工作流为中心。

## 目标

1. 为功能模块建立正式的关系图，支持依赖、影响、共享资源、接口流、数据流等关系。
2. 建立轻量代码实体索引，使功能模块能关联真实代码文件、函数、类、测试。
3. 支持 changed files / commit range 的影响分析，输出受影响模块、风险分、推荐用例库和测试建议。
4. 在全景拓扑中展示真实关系边、hub/bridge、孤岛、未覆盖高风险模块和异常耦合。
5. 支持全景快照和快照 diff，用于版本迭代后的模块/关系/风险变化追踪。
6. 给 Agent 提供稳定写入协议，后续可由工程分析、需求分析、测试 Agent 自动维护图谱。

## 非目标

- 不在第一版引入完整 Tree-sitter 解析器或 MCP server。
- 不替换现有 `GameModulePanorama` / `GameModuleChangeLog`。
- 不强制迁移已有 `extra.cross_module` 数据；只做兼容读取和可选回填。
- 不在本轮实现自动 git diff 拉取远端仓库。影响分析先接受调用方传入 `changed_files` / `commit_range`。

## 数据模型

保留现有表：

- `GameModulePanorama`：功能模块节点。
- `GameModuleChangeLog`：模块变更记录。

新增表：

### `GameModuleRelation`

模块关系边。

字段：

- `id`
- `project_id`
- `source_module_id`
- `target_module_id`
- `relation_type`: `child` / `depends_on` / `affects` / `shared_resource` / `api_flow` / `data_flow` / `test_overlap`
- `confidence`: 0.0-1.0
- `evidence`: JSON，保存 reason、files、symbols、resource_paths、case_library_ids 等。
- `source`: `manual` / `agent` / `code_index` / `migration`
- `created_by`
- `updated_by`
- `created_at`
- `updated_at`

唯一约束：`project_id + source_module_id + target_module_id + relation_type`。

### `PanoramaCodeEntity`

轻量代码实体索引。

字段：

- `id`
- `project_id`
- `repo_key`
- `file_path`
- `entity_type`: `file` / `function` / `class` / `import` / `test` / `resource`
- `symbol_name`
- `language`
- `start_line`
- `end_line`
- `content_hash`
- `extra`: JSON，保存 signature、imports、calls、test_targets 等。
- `created_at`
- `updated_at`

唯一约束：`project_id + repo_key + file_path + entity_type + symbol_name + start_line`。

### `PanoramaModuleCodeLink`

功能模块和代码实体的关联。

字段：

- `id`
- `project_id`
- `module_id`
- `entity_id`
- `link_type`: `owns` / `uses` / `tests` / `configures` / `resource`
- `confidence`
- `evidence`: JSON，保存匹配来源，如 `code_paths`、Agent 判断、人工修正。
- `source`
- `created_at`
- `updated_at`

唯一约束：`module_id + entity_id + link_type`。

### `PanoramaSnapshot`

全景快照元信息。

字段：

- `id`
- `project_id`
- `name`
- `description`
- `module_count`
- `relation_count`
- `code_entity_count`
- `created_by`
- `created_at`

### `PanoramaSnapshotItem`

快照明细，保存模块和关系的关键状态。

字段：

- `id`
- `snapshot_id`
- `item_type`: `module` / `relation`
- `item_key`
- `payload`: JSON

### `PanoramaImpactAnalysis`

一次变更影响分析结果。

字段：

- `id`
- `project_id`
- `input_type`: `changed_files` / `commit_range` / `manual`
- `input_payload`: JSON
- `affected_modules`: JSON
- `affected_relations`: JSON
- `recommended_case_libraries`: JSON
- `risk_score`
- `risk_level`
- `test_context`: JSON
- `token_savings`: JSON，估算 full_context、selected_context、saved_ratio。
- `summary`
- `created_by`
- `created_at`

## API 设计

### 关系图 API

- `GET /api/v1/panorama/relations?project_id=&module_id=&relation_type=`
- `POST /api/v1/panorama/relations`
- `POST /api/v1/panorama/relations/batch`
- `PUT /api/v1/panorama/relations/<id>`
- `DELETE /api/v1/panorama/relations/<id>`

### 统一图 API

- `GET /api/v1/panorama/graph?project_id=`

返回：

```json
{
  "nodes": [],
  "edges": [],
  "metrics": {
    "hub_modules": [],
    "bridge_modules": [],
    "isolated_modules": [],
    "untested_high_risk_modules": [],
    "surprising_relations": []
  }
}
```

前端拓扑优先使用该接口。旧数据兼容策略：如果正式关系为空，后端可从 `parent_id` 和 `extra.cross_module` 生成只读边。

### 代码实体 API

- `GET /api/v1/panorama/code-entities?project_id=&file_path=&symbol=`
- `POST /api/v1/panorama/code-entities/batch`
- `POST /api/v1/panorama/module-code-links/batch`
- `GET /api/v1/panorama/modules/<id>/code-context`

### 影响分析 API

- `POST /api/v1/panorama/impact/analyze`
- `GET /api/v1/panorama/impact/<id>`
- `GET /api/v1/panorama/impact/recent?project_id=`

输入示例：

```json
{
  "project_id": 1,
  "changed_files": ["Assets/Scripts/Racing/DriftController.cs"],
  "commit_range": "abc123..def456"
}
```

输出包含：

- 直接命中的代码实体。
- 关联的功能模块。
- 通过模块关系扩散的二级影响。
- 推荐用例库和测试重点。
- 风险等级和解释。

### 快照 API

- `POST /api/v1/panorama/snapshots`
- `GET /api/v1/panorama/snapshots?project_id=`
- `GET /api/v1/panorama/snapshots/<id>`
- `GET /api/v1/panorama/snapshots/diff?from=<id>&to=<id>`

## 前端设计

修改 `web/templates/panorama.html`，不新增页面。

### 拓扑图

- 数据源从 `/panorama/modules` 切到 `/panorama/graph`。
- 边类型来自后端 `edges`，保留父子、依赖、影响开关。
- 节点大小可根据 degree、risk、recent changes 加权。
- 高亮本轮 impact analysis 的 affected modules。

### 详情面板

新增区块：

- 关系：上游依赖、下游影响、共享资源、接口/数据流。
- 代码实体：关联文件、函数、类、测试。
- 影响半径：最近一次分析中该模块的影响来源。
- 推荐测试上下文：用例库、目录、测试重点、风险解释。

### 统计页

新增：

- Hub 模块。
- Bridge 模块。
- 孤岛模块。
- 未覆盖高风险模块。
- 异常跨域耦合。
- 最近 impact 分析列表。

### 操作入口

新增两个轻量入口：

- “分析变更”：粘贴 changed files 或 commit range，调用 impact API。
- “快照对比”：选择两个快照并展示模块/关系/风险变化。

## Agent 协议

Agent 写入分三类：

1. 工程分析 Agent 写代码实体和 module-code link。
2. 需求/功能 Agent 写模块关系和变更记录。
3. 测试 Agent 根据 impact analysis 补充测试建议、用例库关联和风险解释。

约定：

- 每轮分析开始前可调用 `clear-highlights`。
- 写入 impact analysis 后，后端负责把 affected modules 标记 `highlight=true`。
- `confidence < 0.5` 的关系在 UI 上显示为低置信虚线，避免误导。

## 迁移策略

1. 新增表和 API，不删除旧字段。
2. `/panorama/graph` 先兼容生成旧边：
   - `parent_id` -> `child`
   - `extra.dependencies` -> `depends_on`
   - `extra.cross_module.depends_on` -> `depends_on`
   - `extra.cross_module.affects` -> `affects`
3. 后续提供一次性回填脚本，把旧 `extra` 关系写入 `GameModuleRelation`，source=`migration`。
4. 前端切到 `/panorama/graph` 后，旧模块数据仍可显示。

## 风险与处理

- 图谱过大导致前端卡顿：第一版按项目过滤，默认最多返回活跃模块；后续再做分页/层级展开。
- 关系置信度不准：所有 Agent 写入关系必须带 `confidence` 和 `evidence`。
- 影响分析过度扩散：默认只做 2 跳扩散，且边类型可配置。
- 旧数据格式不一致：后端 `_normalize_relation_ref()` 兼容 id、对象、旧字符串。

## 测试策略

- 模型/API 单元测试：关系 upsert、唯一约束、graph 兼容旧 extra、impact 命中。
- HTTP smoke：`/panorama/graph`、`/impact/analyze`、`/snapshots/diff`。
- 前端手工验证：拓扑图边开关、节点详情、分析变更、快照 diff。
- 线上发布后验证：`py_compile`、服务 active、典型项目返回 graph/impact。

## 分期

### 第一期：真实关系图

- 新增 `GameModuleRelation`。
- 新增关系 CRUD/batch API。
- 新增 `/panorama/graph`。
- 前端拓扑使用真实边。
- stats 增加 hub/bridge/isolated/untested high risk。

### 第二期：代码实体和影响分析

- 新增 `PanoramaCodeEntity`、`PanoramaModuleCodeLink`。
- 新增代码实体批量写入 API。
- 新增 impact analyze API。
- 详情面板展示代码实体和推荐测试上下文。

### 第三期：快照 diff 和上下文节省

- 新增快照表/API。
- 新增 diff API。
- impact analysis 保存 `token_savings` 估算。
- 前端增加快照对比和最小测试上下文面板。

### 第四期：Agent 自动化协议

- 更新相关 Skill 文档。
- 增加 Agent 写入示例。
- 增加旧 `extra` 关系回填脚本。

