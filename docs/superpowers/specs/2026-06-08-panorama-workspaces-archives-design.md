# 功能全景多标题与存档设计

## 背景

当前功能全景以 `project_id` 作为主要隔离维度，模块节点、关系边、代码实体、影响分析和快照都默认表示“一个项目的一份全景”。模块批量写入以 `project_id + path` 作为覆盖口径，因此同一项目无法同时维护“程序功能”“配置模块”“XX模块”等多套独立全景。

用户要求：

1. 增加唯一标题作为标识，一个标题就是一个功能全景。
2. 每个标题下保存最近 10 份存档。
3. 存档需要可查看，并可一键恢复为当前全景。

## 目标

- 一个项目下可以有多个功能全景标题。
- 每个标题拥有独立的模块树、关系边、代码实体关联、影响分析和存档。
- 保持旧 API 兼容：未传标题或工作区时写入默认全景。
- 每个标题最多保留最近 10 份存档，超出自动清理最旧存档。
- 存档可查看、对比、恢复；恢复只影响当前标题，不影响同项目其他标题。

## 非目标

- 不重做拓扑图渲染算法。
- 不迁移旧模块 ID 到全局唯一新 ID 体系之外的复杂映射。
- 不改变现有 Agent 写模块、关系、代码实体的基本 payload 结构。
- 不在第一版做多人并发编辑锁。

## 数据模型

### 新增 `PanoramaWorkspace`

表名：`panorama_workspaces`

字段：

- `id`
- `project_id`
- `title`：唯一标题，例如 `程序功能`、`配置模块`
- `description`
- `is_default`
- `created_by`
- `updated_by`
- `created_at`
- `updated_at`

唯一约束：

- `project_id + title`

说明：

- 每个项目至少有一个默认工作区。
- 旧数据迁移到默认工作区，默认标题使用 `项目功能全景图`。
- 全局项目（`project_id=NULL`）也允许默认工作区，但唯一约束需在应用层处理空值。

### 现有表补字段

以下表增加 `workspace_id`：

- `game_module_panorama`
- `game_module_relations`
- `panorama_code_entities`
- `panorama_module_code_links`
- `panorama_impact_analyses`
- `panorama_snapshots`

约束与查询口径调整：

- 模块 upsert 从 `project_id + path` 改为 `workspace_id + path`。
- 关系唯一约束逻辑从项目级改为工作区级。
- 代码实体唯一口径从 `project_id + repo_key + file_path + ...` 扩展到 `workspace_id + repo_key + file_path + ...`。
- 快照列表按 `workspace_id` 筛选。

## API 设计

### 工作区

- `GET /api/v1/panorama/workspaces?project_id=`
- `POST /api/v1/panorama/workspaces`
- `PUT /api/v1/panorama/workspaces/{id}`
- `DELETE /api/v1/panorama/workspaces/{id}`

删除规则：

- 默认工作区不允许删除。
- 非默认工作区删除时软删除或硬删除可二选一；第一版建议硬删除并级联删除其模块/关系/快照，操作前二次确认。

### 兼容参数

现有接口增加以下参数：

- `workspace_id`
- `panorama_title`

解析规则：

1. 优先使用 `workspace_id`。
2. 其次使用 `project_id + panorama_title` 查找或创建工作区。
3. 都不传时使用 `project_id` 下默认工作区。

覆盖接口：

- `/panorama/modules`
- `/panorama/modules/batch`
- `/panorama/relations`
- `/panorama/relations/batch`
- `/panorama/graph`
- `/panorama/code-entities`
- `/panorama/code-entities/batch`
- `/panorama/module-code-links/batch`
- `/panorama/impact/analyze`
- `/panorama/impact/recent`
- `/panorama/snapshots`
- `/panorama/snapshots/diff`
- `/panorama/changes`

### 存档恢复

新增：

- `POST /api/v1/panorama/snapshots/{id}/restore`

行为：

- 读取 snapshot items。
- 清空该 snapshot 对应 `workspace_id` 下当前模块、关系、代码关联。
- 按存档 payload 重建模块树和关系边。
- 需要维护旧模块 key 到新模块 id 的映射，避免关系边恢复后指向旧 id。
- 恢复完成后写一条新的快照或操作日志，便于追溯。

## 存档策略

创建存档时：

- 使用当前 `workspace_id` 收集模块、关系、代码实体和链接。
- 保存为 `PanoramaSnapshot` + `PanoramaSnapshotItem`。
- `module_count/relation_count/code_entity_count` 记录当前工作区统计。
- 提交后保留该工作区最近 10 份存档，删除更早存档及明细。

自动存档：

- 第一版保持手动“创建快照”。
- 后续可在 Agent 批量覆盖前增加 `auto_archive=true` 参数，写入前自动创建存档。

## 前端交互

顶部筛选区改为：

- 项目选择
- 全景标题选择
- 风险筛选
- 新建全景标题
- 创建存档

切换标题后刷新：

- 统计卡
- 模块列表
- 拓扑图
- 变更记录
- 存档列表

存档列表增加：

- 查看
- 对比
- 恢复

## 迁移策略

启动迁移：

1. 创建 `panorama_workspaces` 表。
2. 为每个已有 `game_module_panorama.project_id` 创建默认工作区。
3. 将旧模块、关系、代码实体、链接、影响分析、快照回填到默认工作区。
4. 后续新写入未传工作区时自动走默认工作区。

兼容性：

- 旧 Agent 不需要立即改造。
- 新 Agent 推荐在写入时传 `panorama_title`，例如 `程序功能`。

## 验证

- 一个项目下创建两个标题，写入相同 path，不互相覆盖。
- 切换标题后，拓扑图和模块列表完全隔离。
- 每个标题创建 11 份存档后只保留最近 10 份。
- 恢复旧存档后，当前模块树与关系边回到存档状态。
- 旧 API 不传标题时仍能读写默认全景。
