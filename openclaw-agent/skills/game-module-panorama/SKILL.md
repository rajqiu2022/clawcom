# Game Module Panorama — 游戏功能模块全景视图

## 使命

建立和维护游戏功能模块全景视图，通过分析代码仓库的模块结构和变更，让团队实时掌握：
- 哪个功能模块改了什么逻辑
- 换了什么资源
- 对应需要改哪里用例、要测什么
- 模块间真实依赖/影响关系、变更影响半径、推荐测试上下文

## 触发场景

1. **选择/创建工作区**：同一项目可有多个功能全景标题（如「项目功能全景图」「配置模块」）
2. **初始化全景**：首次为某工作区建立完整的功能模块树
3. **增量更新**：分析一段时间的 commit，记录各模块的变更
4. **关系维护**：写入模块间正式关系边（依赖/影响/共享资源等）
5. **代码索引**：写入代码实体和模块-代码关联
6. **影响分析**：根据 changed files 分析受影响模块和推荐用例库
7. **快照存档**：保存工作区快照（每标题保留最近 10 份），可对比或恢复
8. **风险评估**：根据变更频率和耦合度更新模块风险等级
9. **测试覆盖/bug 风险视图**：通过正式关联表读取用例库/目录覆盖，按关联用例数和 bug 风险渲染拓扑

## ⛔ 项目 vs 工作区（硬约束，必读）

**同一款游戏/产品 = 一个 Hub 项目（`project_id`）。多种功能全景视角 = 同一项目下的多个工作区标题（`workspace`）。**

| 层级 | 是什么 | 怎么建 | 典型例子 |
|------|--------|--------|----------|
| **项目** `project_id` | TAPD/业务项目，权限与用例库归属 | `POST /api/v1/projects`（极少用，需主人明确授权） | `魂斗罗` → `project_id=4` |
| **工作区** `workspace_id` | 同一项目下的一个功能全景标题 | `POST /api/v1/panorama/workspaces` | `项目功能全景图`、`GameData配置全景` |

### 禁止事项（违反即错误）

1. **禁止**为「代码全景 / 配置全景 / XX模块全景」再建一个 Hub 项目
2. **禁止**把 `project_id=0` 或「魂斗罗-GameData配置全景」这类名称当成独立项目来承载第二份全景
3. **禁止**用 `POST /projects` 代替 `POST /panorama/workspaces`

### 正确 vs 错误（魂斗罗实例）

用户说：「再建一个配置体系的功能全景」

| ❌ 错误做法 | ✅ 正确做法 |
|------------|------------|
| `POST /projects` 创建 `魂斗罗-GameData配置全景`（project #0） | 保持 `project_id=4`（魂斗罗） |
| 在新项目下写模块/关系 | `POST /panorama/workspaces { project_id: 4, title: "GameData配置全景" }` |
| 两个项目各维护一份全景 | 项目 #4 下两个标题：默认 `项目功能全景图` + `GameData配置全景` |

期望结果示意：

| 项目 | 工作区标题 | 模块数 | 用途 |
|------|-----------|--------|------|
| #4 魂斗罗 | `项目功能全景图` | 38 | 代码功能全景 |
| #4 魂斗罗 | `GameData配置全景` | 42 | 配置体系全景 |

> 判断口诀：**要新视角 → 建 workspace 标题；要新产品 → 才考虑建 project（且须主人确认）。**

## 功能全景标题 / 工作区

一个项目下可有**多个独立全景**，用唯一标题区分。每个标题有独立模块、关系、代码索引、影响分析和快照。

| 概念 | 说明 |
|------|------|
| `workspace_id` | 工作区 ID，所有读写 API 推荐显式传入 |
| `panorama_title` / `workspace_title` | 按标题解析工作区；不存在时可自动创建 |
| 默认工作区 | 未传 `workspace_id`/标题时，落到 `项目功能全景图` |
| 数据隔离 | 同项目不同标题下，相同 `path` 是不同模块 |

### 工作区管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/workspaces?project_id=N` | 列出项目下全部标题 |
| POST | `/workspaces` | 创建标题（同项目同名返回已有） |
| PUT | `/workspaces/{id}` | 重命名/改说明 |
| DELETE | `/workspaces/{id}` | 删除非默认工作区（级联清空该标题数据） |

创建示例（在**已有项目**下新建标题，不要新建项目）：

```json
POST {{HUB}}/api/v1/panorama/workspaces
Authorization: Bearer {{TOKEN}}

{
  "project_id": 4,
  "title": "GameData配置全景",
  "description": "配置表、开关、数值体系的功能全景"
}
```

创建后，后续所有 `modules/relations/graph/snapshots` 请求都带返回的 `workspace_id`（或 `panorama_title`）。

## Hub API 端点

> BASE = `{{HUB}}/api/v1/panorama`
>
> 查询参数或请求体统一支持：`workspace_id`、`panorama_title`（或 `workspace_title`）、`project_id`。
> 写入时优先带 `workspace_id`；只传 `project_id` 时写入默认标题 `项目功能全景图`。

### 模块管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/modules?project_id=N&workspace_id=W&tree=1` | 获取模块树 |
| POST | `/modules` | 创建单个模块 |
| POST | `/modules/batch` | 批量创建/更新模块（支持 is_new / highlight） |
| GET | `/modules/{id}` | 获取模块详情（含子节点+变更记录） |
| PUT | `/modules/{id}` | 更新模块 |
| DELETE | `/modules/{id}` | 删除模块（级联） |
| POST | `/modules/clear-highlights?project_id=N&workspace_id=W` | 每轮分析开始前清空 highlight |
| GET | `/modules/{id}/code-context` | 获取模块关联代码实体 |

### 模块关系（正式边，优先使用）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/relations?project_id=N&workspace_id=W&module_id=&relation_type=` | 查询关系边 |
| POST | `/relations` | 创建/更新单条关系 |
| POST | `/relations/batch` | 批量创建/更新关系 |
| PUT | `/relations/{id}` | 更新关系 |
| DELETE | `/relations/{id}` | 删除关系 |

`relation_type` 可选：`child` / `depends_on` / `affects` / `shared_resource` / `api_flow` / `data_flow` / `test_overlap`

### 统一拓扑图

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/graph?project_id=N&workspace_id=W` | 返回 nodes + edges + metrics（hub/bridge/isolated/untested high risk） |

前端拓扑优先使用该接口。若正式关系为空，后端会兼容 `parent_id` 和 `extra.cross_module` 生成只读边。

### 代码实体索引

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/code-entities?project_id=N&workspace_id=W&file_path=&symbol=` | 查询代码实体 |
| POST | `/code-entities/batch` | 批量写入文件/函数/类/测试实体 |
| POST | `/module-code-links/batch` | 批量写入模块-代码关联 |

### 影响分析

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/impact/analyze` | 基于 changed_files / commit_range 分析影响 |
| GET | `/impact/{id}` | 获取分析详情 |
| GET | `/impact/recent?project_id=N&workspace_id=W` | 最近影响分析 |

分析完成后，后端会自动把 `affected_modules` 标记为 `highlight=true`。

### 快照（按工作区存档，每标题保留最近 10 份）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/snapshots` | 创建当前工作区快照（超出 10 份自动删最旧） |
| GET | `/snapshots?project_id=N&workspace_id=W` | 快照列表 |
| GET | `/snapshots/{id}?include_items=1` | 快照详情 |
| GET | `/snapshots/diff?from={id}&to={id}` | 对比两个快照（须同一工作区） |
| POST | `/snapshots/{id}/restore` | 将快照恢复为当前工作区数据（模块/关系/代码实体/关联） |

### 变更记录

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/changes?module_id=N&change_type=X&start_date=&end_date=` | 查询变更记录 |
| POST | `/changes` | 记录单次变更 |
| POST | `/changes/batch` | 批量记录变更 |
| GET | `/changes/{id}` | 获取单条变更详情 |
| PUT | `/changes/{id}` | 修正单条变更 |
| DELETE | `/changes/{id}` | 删除单条变更 |
| GET | `/modules/{id}/changes` | 查询指定模块变更 |
| POST | `/modules/{id}/changes` | 为指定模块记录变更 |
| POST | `/modules/{id}/changes/batch` | 为指定模块批量记录变更 |

### 统计

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/stats?project_id=N&workspace_id=W` | 全景统计（含关系/代码实体/影响分析/快照摘要） |

### 用例覆盖与 bug 风险

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/modules/{id}/testcase-links?include_children=1` | 查询模块及子模块关联的用例库/目录 |
| GET | `/modules/{id}/test-metrics` | 查询模块测试覆盖与 bug 风险指标 |
| POST | `/modules/{id}/test-metrics` | Agent 提交 `bug_count` / `bug_risk_score` / `bug_risk_level` |
| GET | `/modules/{id}/testcase-changes?since=&until=&include_children=1` | 从功能模块维度查询最近变更用例 |
| POST | `/api/v1/testcase-panorama-links/sync` | 主动同步关联、清理脏数据、重算覆盖指标 |

Agent 提交 bug 风险示例：

```json
POST {{HUB}}/api/v1/panorama/modules/{MODULE_ID}/test-metrics

{
  "bug_count": 8,
  "bug_risk_score": 72,
  "bug_risk_level": "high",
  "source": "agent",
  "metrics_payload": {
    "tapd_bug_ids": ["123", "456"],
    "window": "last_30_days"
  }
}
```

功能全景拓扑有两个视图：

- `模块结构`：节点大小按子模块数，颜色按模块 `risk_level`
- `测试覆盖/风险`：节点大小按 `subtree_case_count`，颜色按 `bug_risk_score` 或 `bug_count`

当用例库、用例目录或功能模块被删除时，Hub 会在删除事务里硬删除失效关联；如需修复历史脏数据，调用同步接口。

## 推荐工作流

### 每轮增量分析标准流程

0. 确认**项目**不变：沿用当前 `project_id`（如魂斗罗=4）；需要新视角时 `POST /workspaces` 建标题，**不要** `POST /projects`
1. 确认目标工作区：`GET /workspaces?project_id=N`；选中或新建 `workspace_id`
2. `POST /modules/clear-highlights?project_id=N&workspace_id=W`
3. 分析 commit / diff，匹配模块
4. `POST /modules/batch` 更新模块（带 `workspace_id`），本轮涉及模块传 `highlight: true`
5. `POST /relations/batch` 写入/更新模块关系（**优先于 extra.cross_module**）
6. `POST /code-entities/batch` + `POST /module-code-links/batch`（如有代码索引）
7. `POST /changes/batch` 写入变更记录（`module_path` 查找须带 `workspace_id`）
8. 可选：`POST /impact/analyze` 基于 changed_files 输出影响半径和推荐用例库
9. 可选：`POST /snapshots` 在版本节点保存快照；需要回滚时 `POST /snapshots/{id}/restore`

### 流程一：初始化全景

```json
POST {{HUB}}/api/v1/panorama/modules/batch
Authorization: Bearer {{TOKEN}}

{
  "project_id": 1,
  "workspace_id": 7,
  "modules": [
    {
      "name": "赛车系统",
      "path": "赛车系统",
      "description": "赛车核心玩法",
      "code_paths": ["Assets/Scripts/Racing/"],
      "resource_paths": ["Assets/Resources/Racing/"],
      "test_focus": "核心手感、物理帧一致性"
    },
    {
      "name": "漂移系统",
      "path": "赛车系统/漂移系统",
      "parent_path": "赛车系统",
      "code_paths": ["Assets/Scripts/Racing/Drift/"],
      "test_focus": "漂移手感、集气段数"
    }
  ]
}
```

### 流程二：写入正式关系边

```json
POST {{HUB}}/api/v1/panorama/relations/batch
Authorization: Bearer {{TOKEN}}

{
  "relations": [
    {
      "source_module_id": 12,
      "target_module_id": 25,
      "relation_type": "depends_on",
      "confidence": 0.9,
      "evidence": {
        "reason": "漂移逻辑调用比赛状态同步",
        "files": ["Assets/Scripts/Racing/Drift/DriftController.cs"]
      },
      "source": "agent"
    },
    {
      "source_module_id": 12,
      "target_module_id": 6,
      "relation_type": "affects",
      "confidence": 0.85,
      "evidence": {"reason": "漂移参数变更影响匹配房间手感校验"},
      "source": "agent"
    }
  ]
}
```

> 关系写入要求：`confidence` 和 `evidence` 必填；`confidence < 0.5` 视为低置信关系。

### 流程三：写入代码实体与模块关联

```json
POST {{HUB}}/api/v1/panorama/code-entities/batch
{
  "project_id": 1,
  "entities": [
    {
      "file_path": "Assets/Scripts/Racing/Drift/DriftController.cs",
      "entity_type": "class",
      "symbol_name": "DriftController",
      "language": "csharp"
    }
  ]
}

POST {{HUB}}/api/v1/panorama/module-code-links/batch
{
  "links": [
    {
      "module_id": 12,
      "entity_id": 100,
      "link_type": "owns",
      "confidence": 0.95,
      "evidence": {"matched_by": "code_paths"},
      "source": "agent"
    }
  ]
}
```

### 流程四：影响分析

```json
POST {{HUB}}/api/v1/panorama/impact/analyze
Authorization: Bearer {{TOKEN}}

{
  "project_id": 1,
  "workspace_id": 7,
  "changed_files": [
    "Assets/Scripts/Racing/Drift/DriftController.cs"
  ],
  "commit_range": "abc123..def456"
}
```

返回重点字段：
- `affected_modules`：直接命中 + 关系扩散模块
- `recommended_case_libraries`：推荐回归用例库
- `risk_level` / `risk_score`
- `test_context`：测试重点、命中实体
- `token_savings`：相对全量上下文的节省比例

### 流程五：快照与 diff

```json
POST {{HUB}}/api/v1/panorama/snapshots
{
  "project_id": 1,
  "workspace_id": 7,
  "name": "v1.2.0 发布前",
  "description": "版本封板前全景"
}

GET {{HUB}}/api/v1/panorama/snapshots/diff?from=3&to=5

POST {{HUB}}/api/v1/panorama/snapshots/5/restore
```

diff 返回：`added_modules` / `removed_modules` / `changed_modules` / `added_relations` / `removed_relations` / `summary.risk_escalations`

restore 返回：`restored_modules` / `restored_relations` / `restored_code_entities` / `restored_code_links`

### 流程六：批量记录变更

```json
POST {{HUB}}/api/v1/panorama/changes/batch
{
  "changes": [
    {
      "module_path": "赛车系统/漂移系统",
      "project_id": 1,
      "workspace_id": 7,
      "change_type": "logic",
      "summary": "漂移集气阈值调整",
      "detail": {
        "changed_files": ["Assets/Scripts/Racing/Drift/DriftCharger.cs"]
      },
      "risk_level": "high",
      "test_suggestion": "回归漂移集气边界值"
    }
  ]
}
```

## 跨模块关联（legacy 兼容）

仍可在模块 `extra.cross_module` 中写依赖/影响，前端和后端会兼容读取。但**新数据应优先写 `/relations/batch`**，不要继续只写 `extra`。

旧格式示例：
```json
{
  "extra": {
    "cross_module": {
      "depends_on": [{"id": 3, "name": "UI系统", "reason": "车辆展示UI框架"}],
      "affects": [{"id": 25, "name": "比赛逻辑", "reason": "状态同步"}],
      "risk_if_changed": "high"
    }
  }
}
```

## 模块识别规则

| 维度 | 示例 |
|------|------|
| 目录结构 | `Scripts/Racing/` → 赛车系统 |
| 命名空间 | `namespace Game.Racing.Drift` → 漂移系统 |
| Unity 组件 | 挂载了 `DriftController` 的 Prefab |
| 配置表 | `Config/Drift.json` → 漂移配置 |
| 资源目录 | `Resources/VFX/Drift/` → 漂移特效 |

## 变更类型定义

| change_type | 含义 |
|-------------|------|
| `logic` | 游戏逻辑变更 |
| `resource` | 美术/音效/UI资源替换 |
| `config` | 数值配置/表格变更 |
| `api` | 接口/协议变更 |
| `refactor` | 代码重构 |
| `bugfix` | Bug修复 |
| `feature` | 新功能开发 |

## 风险等级定义

| risk_level | 测试策略 |
|------------|----------|
| `critical` | 全量回归+专项测试 |
| `high` | 重点回归 |
| `normal` | 影响范围内回归 |
| `low` | 冒烟验证 |

## 认证

```
Authorization: Bearer {{TOKEN}}
```

## 注意事项

1. **多种全景视角 = 多 workspace，不是多 project**；除主人明确要求新产品外，不得新建项目
2. 批量接口对路径做幂等：同 `workspace_id + path` 存在则更新（不再按项目全局唯一）
3. 关系边唯一键：`workspace_id + source_module_id + target_module_id + relation_type`
4. 快照按工作区保留最近 10 份；`restore` 会覆盖当前工作区模块/关系/代码实体/关联
5. 快照 diff 使用模块 `path` 和关系 `source_path->target_path:type` 作为稳定键，不依赖数据库自增 ID
6. `highlight` 可由 Agent 显式设置，也可由 `impact/analyze` 自动标记受影响模块
7. `code_paths` / `resource_paths` 使用 glob，匹配时用 fnmatch
8. 模块删除会级联删除子模块、关系、代码关联和变更记录；默认工作区 `项目功能全景图` 不可删除
