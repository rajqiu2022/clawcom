# Game Module Panorama — 游戏功能模块全景视图

## 使命

建立和维护游戏功能模块全景视图，通过分析代码仓库的模块结构和变更，让团队实时掌握：
- 哪个功能模块改了什么逻辑
- 换了什么资源
- 对应需要改哪里用例、要测什么

## 触发场景

1. **初始化全景**：首次为某项目建立完整的功能模块树
2. **增量更新**：分析一段时间的 commit，记录各模块的变更
3. **风险评估**：根据变更频率和耦合度更新模块风险等级
4. **关联测试**：为变更的模块关联测试用例和回归建议

## Hub API 端点

> BASE = `{{HUB}}/api/v1/panorama`

### 模块管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/modules?project_id=N&tree=1` | 获取模块树 |
| POST | `/modules` | 创建单个模块 |
| POST | `/modules/batch` | 批量创建/更新模块 |
| GET | `/modules/{id}` | 获取模块详情（含子节点+变更记录） |
| PUT | `/modules/{id}` | 更新模块 |
| DELETE | `/modules/{id}` | 删除模块（级联） |

### 变更记录

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/changes?module_id=N&change_type=X&start_date=&end_date=` | 查询变更记录（全局） |
| POST | `/changes` | 记录单次变更 |
| POST | `/changes/batch` | 批量记录变更 |
| GET | `/changes/{id}` | 获取单条变更详情 |
| PUT | `/changes/{id}` | **修正**单条变更（用于历史数据更正，可改 module_id 迁移模块） |
| DELETE | `/changes/{id}` | 删除单条变更（清理错误历史） |
| GET | `/modules/{id}/changes?start_date=&end_date=&limit=&offset=` | 查询指定模块的变更记录 |
| POST | `/modules/{id}/changes` | 为指定模块记录单次变更 |
| POST | `/modules/{id}/changes/batch` | 为指定模块批量记录变更 |

> **关于 PUT / DELETE 单条变更**：
> - 主要用于修正历史误录入数据（例如 module_id 关联错了、summary 写错、change_type 标错）
> - PUT 可更新字段：`change_type / summary / detail / affected_cases / test_suggestion / risk_level / source / module_id`
> - PUT 时如果传 `module_id`，会校验目标模块存在；否则保持原 module_id 不变
> - DELETE 用于彻底删除错误条目；如果只是想标记"该变更已废弃"，建议用 PUT 改 `summary` 加 `[已废弃]` 前缀

### 统计

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/stats?project_id=N` | 全景统计 |

## 工作流

### 流程一：初始化全景（建立模块树）

**输入**：项目代码仓库路径/URL
**步骤**：

1. 克隆/打开代码仓库
2. 分析目录结构，识别功能模块边界（按目录、命名空间、组件划分）
3. 构建模块层次树，确定每个模块的：
   - `name`：模块名称（中文，易于理解）
   - `path`：层次路径，如 `赛车/漂移系统`
   - `parent_path`：父模块路径（批量创建时用于匹配）
   - `description`：功能简述
   - `code_paths`：代码路径 glob 列表
   - `resource_paths`：资源路径 glob 列表
   - `test_focus`：测试重点
   - `is_new`：是否新增模块（true/false），由 Agent 标记，前端会显示红色 NEW 徽章
4. 调用 `POST /modules/batch` 一次性写入

**请求示例**：
```json
POST {{HUB}}/api/v1/panorama/modules/batch
Authorization: Bearer {{TOKEN}}
Content-Type: application/json

{
  "project_id": 1,
  "modules": [
    {
      "name": "赛车系统",
      "path": "赛车系统",
      "description": "赛车核心玩法，包括加速、漂移、碰撞等",
      "code_paths": ["Assets/Scripts/Racing/"],
      "resource_paths": ["Assets/Resources/Racing/"],
      "test_focus": "核心手感、物理帧一致性"
    },
    {
      "name": "漂移系统",
      "path": "赛车系统/漂移系统",
      "parent_path": "赛车系统",
      "description": "漂移入弯、集气、出弯加速的完整逻辑",
      "code_paths": ["Assets/Scripts/Racing/Drift/", "Assets/Scripts/Racing/DriftVFX/"],
      "resource_paths": ["Assets/Resources/VFX/Drift/"],
      "test_focus": "漂移手感、集气段数、氮气加速值"
    },
    {
      "name": "加速系统",
      "path": "赛车系统/加速系统",
      "parent_path": "赛车系统",
      "description": "小喷、CW喷、氮气加速",
      "code_paths": ["Assets/Scripts/Racing/Boost/"],
      "test_focus": "各段加速值数值校验、叠加规则",
      "is_new": true
    }
  ]
}
```

> **关于 `is_new` 字段**：
> - 首次建立模块树时，所有模块可不传或传 `false`（默认）
> - 增量分析新版本时，识别出**本次新加的模块**（从未在 panorama 中出现过的），传 `is_new: true`
> - 模块"成熟"后（如下个迭代周期开始时），调用 `PUT /modules/{id}` 或 batch 接口将 `is_new` 改回 `false`，移除 NEW 标记
> - 该字段由 Agent 自主管理，不依赖时间自动判断

### 流程二：增量更新（记录变更）

**输入**：最近一段时间的 commit 列表 / PR 列表
**步骤**：

1. 获取 commit diff（文件变更列表）
2. 根据变更文件路径匹配到对应模块（用 `code_paths` / `resource_paths` 做 glob 匹配）
3. 分析每个模块的变更内容，生成摘要：
   - `change_type`：逻辑(logic)、资源(resource)、配置(config)、接口(api)、重构(refactor)、修复(bugfix)、新功能(feature)
   - `summary`：一句话变更摘要
   - `detail`：变更详情（文件列表、关键代码片段等）
   - `risk_level`：变更风险（low/normal/high/critical）
   - `test_suggestion`：测试建议
   - `affected_cases`：受影响的用例描述
4. 调用 `POST /changes/batch` 批量写入

**请求示例**：
```json
POST {{HUB}}/api/v1/panorama/changes/batch
Authorization: Bearer {{TOKEN}}
Content-Type: application/json

{
  "changes": [
    {
      "module_path": "赛车系统/漂移系统",
      "project_id": 1,
      "change_type": "logic",
      "summary": "漂移集气公式调整：三段集气阈值从0.8/1.6/2.4改为0.6/1.4/2.2",
      "detail": {
        "commits": ["abc123", "def456"],
        "changed_files": ["Assets/Scripts/Racing/Drift/DriftCharger.cs"],
        "key_changes": "修改了 CalcChargeLevel() 中的阈值常量"
      },
      "risk_level": "high",
      "test_suggestion": "需回归漂移集气的各段判定边界值，验证手感是否一致",
      "affected_cases": ["漂移-集气段数判定", "漂移-出弯加速值校验"]
    },
    {
      "module_path": "赛车系统/漂移系统",
      "project_id": 1,
      "change_type": "resource",
      "summary": "漂移拖尾特效材质球替换为新版PBR材质",
      "detail": {
        "changed_files": ["Assets/Resources/VFX/Drift/trail_mat_v2.mat"]
      },
      "risk_level": "normal",
      "test_suggestion": "视觉验证漂移拖尾效果，检查低端机性能",
      "affected_cases": ["漂移-视觉效果", "性能-特效DrawCall"]
    }
  ]
}
```

### 流程三：风险评估与更新

根据以下信号评估模块风险等级：
- **变更频率**：近期频繁改动 → 风险上升
- **变更类型**：核心逻辑变更 > 资源替换 > 配置调整
- **耦合度**：被多个模块依赖 → 变更风险更高
- **历史 Bug 密度**：曾经多次修复 → 高风险

评估后调用 `PUT /modules/{id}` 更新 `risk_level`。

### 流程四：关联测试

1. 根据 `affected_cases` 或 `module_path` 匹配 Hub 中的用例库
2. 输出建议回归的用例清单
3. 可在变更记录中标注 `test_suggestion`

## 模块识别规则

Agent 分析代码时，应按以下维度识别功能模块：

| 维度 | 示例 |
|------|------|
| 目录结构 | `Scripts/Racing/` → 赛车系统 |
| 命名空间 | `namespace Game.Racing.Drift` → 漂移系统 |
| Unity 组件 | 挂载了 `DriftController` 的 Prefab |
| 配置表 | `Config/Drift.json` → 漂移配置 |
| 资源目录 | `Resources/VFX/Drift/` → 漂移特效 |

## 变更类型定义

| change_type | 含义 | 典型文件 |
|-------------|------|----------|
| `logic` | 游戏逻辑变更 | .cs, .lua, .py |
| `resource` | 美术/音效/UI资源替换 | .png, .mat, .prefab, .fbx |
| `config` | 数值配置/表格变更 | .json, .csv, .xml, .asset |
| `api` | 接口/协议变更 | .proto, API定义文件 |
| `refactor` | 代码重构（不改行为） | 大规模重命名/移动 |
| `bugfix` | Bug修复 | 关联 Issue/Bug 的 commit |
| `feature` | 新功能开发 | 新增文件/目录 |

## 风险等级定义

| risk_level | 条件 | 测试策略 |
|------------|------|----------|
| `critical` | 核心玩法逻辑+高频变更 | 全量回归+专项测试 |
| `high` | 核心逻辑变更/多模块耦合 | 重点回归 |
| `normal` | 常规变更 | 影响范围内回归 |
| `low` | 纯资源/配置微调 | 冒烟验证 |

## 认证

所有请求需携带 Bearer Token：
```
Authorization: Bearer {{TOKEN}}
```

Token 来源：Hub 分配给当前 OpenClaw 实例的 API Token。

## 注意事项

1. 批量接口对路径做幂等：同 `project_id + path` 存在则更新，不重复创建
2. 变更记录只追加不修改，保留完整历史
3. 模块树支持任意层级嵌套，但建议不超过4层
4. `code_paths` 和 `resource_paths` 使用 glob 模式，Agent 匹配时用 fnmatch
5. 模块删除会级联删除所有子模块和变更记录，操作需谨慎
