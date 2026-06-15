# 用例库管理 (testcase-manager)

## 简介

本 Skill 用于管理 Hub 中心的测试用例库，支持完整的用例生命周期管理：

- **CRUD**：用例库和用例的增删改查
- **批量操作**：批量创建/删除用例
- **AI 智能生成**：通过需求描述自动生成用例、对话式管理
- **导入导出**：YAML / XMind 格式
- **版本管理**：类 git 的快照/回滚/对比（commit / log / checkout / diff）
- **功能全景关联**：用例库/目录可关联功能全景模块，支持双向展示和覆盖统计
- **最近变更追踪**：按时间段快速查询新增、修改、删除的用例，用于评审或测试

---

## 用例格式规范

**每条用例必须包含以下固定字段，提交到 Hub 时严格按此格式。**

### 必填字段（按顺序填写）

| 字段（key） | 必填 | 中文名 | 类型 | 可选值（中文 → key） | 示例 |
|-------------|------|--------|------|---------------------|------|
| `title` | ✅ 必填 | 用例名称 | string（≤255字符） | — | "登录成功后跳转到首页" |
| `content.module_name` | ✅ 必填 | 所属模块 | string | 外围系统 / 核心单局 / 商业化 / 客户端性能 / 服务器专项 / 其他专项 | "外围系统" |
| `priority` | ✅ 必填 | 优先级 | enum | 最高=`P0` / 高=`P1` / 中=`P2` / 低=`P3` | "P1" |
| `type` | ✅ 必填 | 用例类型 | enum | 功能测试=`functional` / 接口测试=`interface` / 性能测试=`performance` / 安全测试=`security` | "functional" |
| `content.preconditions` | ✅ 必填 | 前置条件 | string | — | "用户已注册且账号未被封禁" |
| `content.steps` | ✅ 必填 | 操作步骤 | string[] | 每步一条，数组格式 | ["打开登录页面", "输入用户名和密码"] |
| `content.expected_results` | ✅ 必填 | 预期结果 | string[] | 与操作步骤一一对应 | ["登录页面正常显示", "成功跳转到首页"] |

### 选填字段

| 字段（key） | 必填 | 中文名 | 类型 | 可选值（中文 → key） | 示例 |
|-------------|------|--------|------|---------------------|------|
| `content.status` | 选填 | 用例状态 | enum | 正常=`normal` / 待定=`pending` / 废弃=`deprecated` | "normal" |
| `module_path` | 选填 | 所属目录 | string | 多级用 / 分隔，如 "登录模块/手机号登录" | "登录模块/手机号登录" |
| `tags` | 选填 | 标签 | string[] | 自定义标签，如：回归、冒烟、核心流程 | ["冒烟", "核心流程"] |
| `tapd_story_url` | 选填 | TAPD需求链接 | string | TAPD 需求 URL | "https://www.tapd.cn/..." |
| `tapd_story_title` | 选填 | 需求标题 | string | 需求标题，方便展示 | "用户登录优化" |
| `content.notes` | 选填 | 备注 | string | — | "需在 WiFi 环境下测试" |
| `case_id` | 选填 | 用例编号 | string | 不传则自动生成 TC_001 格式 | "TC_042" |

### 完整用例 JSON 示例

```json
{
  "title": "登录成功后跳转到首页",
  "priority": "P1",
  "type": "functional",
  "tags": ["冒烟", "核心流程"],
  "content": {
    "module_name": "外围系统",
    "status": "normal",
    "preconditions": "1. 用户已注册\n2. 账号未被封禁\n3. 网络正常",
    "steps": [
      "打开客户端登录页面",
      "输入已注册的用户名和密码",
      "点击登录按钮",
      "等待页面跳转"
    ],
    "expected_results": [
      "登录页面正常显示，输入框可交互",
      "用户名和密码正确填入",
      "显示加载状态，无报错",
      "成功跳转到首页，显示用户昵称"
    ],
    "notes": ""
  }
}
```

### 批量创建格式

```json
{
  "cases": [
    {
      "title": "用例1标题",
      "priority": "P1",
      "type": "functional",
      "content": {
        "module_name": "核心单局",
        "preconditions": "...",
        "steps": ["步骤1", "步骤2"],
        "expected_results": ["结果1", "结果2"]
      }
    },
    {
      "title": "用例2标题",
      "priority": "P2",
      "type": "functional",
      "content": { ... }
    }
  ]
}
```

### 一级模块分类（module_name 标准值）

| 模块名 | 说明 |
|--------|------|
| 外围系统 | 商城、大厅、活动、社交系统 |
| 核心单局 | 匹配、单局玩法、操控系统 |
| 商业化 | 会员、充值、抽奖系统 |
| 客户端性能 | 内存、包体、启动、帧率性能 |
| 服务器专项 | 服务器压测、网络延迟 |
| 其他专项 | 兼容性测试、安全测试 |

### 格式校验规则

OpenClaw 在创建/修改用例时**必须确保**：

1. `title` 不能为空，长度 ≤ 255 字符
2. `priority` 必须是 P0/P1/P2/P3 之一
3. `type` 必须是 functional/interface/performance/security 之一
4. `steps` 和 `expected_results` 必须是数组，每个元素是字符串
5. `steps` 和 `expected_results` 的数组长度应一致（步骤和预期结果一一对应）
6. 通过 AI 生成的用例，`ai_generated` 自动标记为 `true`

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar`（SSE 通信链路在线）
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub 地址: http://clawteam.woa.com:18800
API 前缀: /api/v1
```

## 认证方式

```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

> **重要**：`HUB_API_TOKEN` 必须是 `oc_tk_` 开头的明文 Token，不是数据库中加密存储的密文。如果收到 401 错误，请先通过 Web 管理端「OpenClaw 详情 → 查看 Token」获取正确的 Token。

---

## 数据结构

### 用例库 (TestCaseLibrary)

```json
{
  "id": 1,
  "name": "登录模块用例库",
  "description": "覆盖登录功能的所有测试场景",
  "project_name": "QQ飞车",
  "module_name": "登录",
  "owner": "龙虾王",
  "status": "active",
  "case_count": 25,

  // —— v2 评审/共享相关字段（GET /testcase-libraries 列表 + GET /testcase-libraries/{id} 详情都会返回）——
  "review_status": "pending_review",          // draft / pending_review / approved / rejected（最近一次的粗略指示，准确状态以 pending_reviews 为准）
  "current_review_id": 12,                    // 最近一次发起的 review id（当存在 pending 时）
  "review_status_at": "2026-04-23T18:07:56",
  "pending_reviews": [                        // ★ v2 新增：所有进行中的评审（支持并行多个不同 scope）
    {"id": 11, "scope_type": "library", "scope_module_path": "",          "scope_case_count": 184, "submitted_by": "龙虾王", "submitted_at": "2026-04-23T18:07:56"},
    {"id": 12, "scope_type": "module",  "scope_module_path": "登录/手机号", "scope_case_count": 56,  "submitted_by": "alice",  "submitted_at": "2026-04-23T18:08:10"}
  ],
  "pending_review_count": 2,
  "shared_with_me": false,                    // 是否被他人共享给当前 OpenClaw
  "my_share_permission": null,                // 若 shared_with_me=true：readonly / reviewer / editor
  "active_share_count": 3,                    // 该库当前对外开放的 share grant 数量
  "can_manage": true,                         // 当前 OpenClaw 是否可管理（作者/项目 admin/super_admin）
  "can_share": true,                          // 是否可发起共享
  "can_review": true                          // 是否可审批评审（管理者 OR 被授权 reviewer/editor）
}
```

### 用例 (TestCase)

```json
{
  "id": 1,
  "library_id": 1,
  "case_id": "TC_001",
  "title": "验证正确用户名密码登录",
  "priority": "P0",
  "type": "functional",
  "content": {
    "preconditions": "用户已注册，账号未锁定",
    "steps": ["打开登录页面", "输入正确的用户名和密码", "点击登录按钮"],
    "expected_results": ["登录成功", "跳转到首页", "显示用户昵称"]
  },
  "tags": ["登录", "冒烟测试"],
  "ai_generated": false
}
```

**优先级**：`P0`（最高）、`P1`（高）、`P2`（中）、`P3`（低）
**用例类型**：`functional`（功能）、`interface`（接口）、`performance`（性能）、`security`（安全）

### 共享授权 (TestCaseLibraryShare) · v2 新增

```json
{
  "id": 7,
  "library_id": 1,
  "share_type": "user",                  // user / claw / public
  "target_user_id": 12,                  // share_type=user 时填
  "target_claw_id": null,                // share_type=claw 时填
  "permission": "reviewer",              // readonly / reviewer / editor
  "granted_by": "龙虾王",
  "note": "邀请评审 v2.0 登录模块",
  "expires_at": null,                    // 过期时间，null=永久
  "created_at": "2026-04-23T18:00:00"
}
```

> `share_type=public` 表示对所有已注册的 OpenClaw 开放（仍受 permission 限制）。

### 评审记录 (TestCaseLibraryReview) · v2 新增

```json
{
  "id": 11,
  "library_id": 1,
  "status": "submitted",                  // submitted / approved / rejected / withdrawn
  "submitted_by": "龙虾王",
  "submitted_at": "2026-04-23T18:07:56",
  "submit_note": "本次重点：登录新增接口、关注异常分支",
  "scope_summary": "子目录 登录/手机号登录（56 条）",  // 自动生成（用户没填时）
  "scope_type": "module",                 // ★ library / module / cases
  "scope_module_path": "登录/手机号登录",   // ★ scope_type=module 时必填，按前缀匹配子树
  "scope_case_count": 56,                 // ★ 发起时锁定的用例数快照
  "invited_reviewers": [{"type":"user","id":12,"name":"alice"}],
  "decided_by": "",
  "decided_at": null,
  "decision_note": "",
  "related_topic_id": null,
  "created_at": "2026-04-23T18:07:56"
}
```

**评审 scope 重点**（v2 必读）：
- 同一库支持**并行多个 pending review**（不同 scope 互不阻塞）
- 唯一性约束：`(library_id, scope_type, scope_module_path)` —— 同 scope 重复发起返回 409
- `library.review_status` 仅作为「最近一次/全局粗略指示」，**准确的"是否在评审中"应该看 `pending_reviews` 数组**
- approve/reject/withdraw 时若库内仍有其他 pending，library 状态保持 pending_review，**只有最后一个处理完才真正切到结束态**

### 版本快照 (TestCaseSnapshot)

```json
{
  "id": 1,
  "library_id": 1,
  "version": 3,
  "tag": "v1.0",
  "message": "AI 生成 15 条用例后",
  "snapshot_type": "auto",
  "case_count": 40,
  "diff": {
    "added": ["TC_026", "TC_027"],
    "removed": [],
    "modified": [{"case_id": "TC_001", "fields": ["priority"]}],
    "added_count": 2,
    "removed_count": 0,
    "modified_count": 1
  },
  "created_by": "龙虾王",
  "created_at": "2026-04-04T19:30:00"
}
```

---

## 一、用例库管理

### 1. 获取用例库列表

```
GET /api/v1/testcase-libraries

查询参数：
  project_name  按项目筛选
  search        搜索关键词
```

### 2. 创建用例库

```
POST /api/v1/testcase-libraries

{
  "name": "登录模块用例库",        // 必填
  "description": "覆盖登录功能",
  "project_name": "QQ飞车",
  "module_name": "登录",
  "owner": "龙虾王"
}
```

### 3. 获取用例库详情（含所有用例）

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}
```

### 4. 更新用例库

```
PUT /api/v1/testcase-libraries/{LIBRARY_ID}

{
  "name": "新名称",
  "description": "新描述",
  "owner": "新负责人"
}
```

### 5. 删除用例库

```
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}
```

> 删除前系统会自动创建快照（安全网），但删除后用例库本体不可恢复。

---

## 二、目录管理

用例库支持多级目录（模块）组织用例，目录通过 `module_path` 字段实现（如 `"登录模块/手机号登录"`），无需单独建表。

### 5.1 获取目录树

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/modules
```

**响应**：
```json
[
  {"name": "登录模块", "path": "登录模块", "count": 10, "children": [
    {"name": "手机号登录", "path": "登录模块/手机号登录", "count": 5, "children": []}
  ]},
  {"name": "(未分类)", "path": "", "count": 3, "children": []}
]
```

> `count` 为该目录及其子目录下的真实用例数（不含占位用例）。空目录通过占位用例（`is_placeholder=True`）实现。

### 5.2 新增子目录

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/modules

{
  "parent_path": "登录模块",     // 父目录路径，空字符串表示根目录
  "name": "手机号登录"           // 新目录名（不能包含 /）
}
```

**实现原理**：在目标路径创建一个占位用例（`is_placeholder=True`），使空目录能出现在目录树中。当该目录下创建真实用例时，占位用例会自动清理。

**响应**：
```json
{
  "message": "目录 \"登录模块/手机号登录\" 已创建",
  "path": "登录模块/手机号登录"
}
```

**错误**：
- `400`：目录名包含 `/` 或为空
- `409`：同名目录已存在

### 5.3 删除目录

```
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/modules

{
  "path": "登录模块/手机号登录"
}
```

> **仅允许删除空目录**（目录下无真实用例）。如果目录下有真实用例，返回 400 错误。删除时会同时清理该路径及子路径下的占位用例。

**响应**：
```json
{
  "message": "目录 \"登录模块/手机号登录\" 已删除"
}
```

**错误**：
- `400`：path 为空，或目录下有真实用例（会提示用例数量）

### 5.4 重命名目录

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/modules/rename

{
  "old_path": "登录模块",
  "new_path": "登录功能"
}
```

> 批量更新所有匹配 `old_path` 及其子路径的用例的 `module_path`。

---

## 三、用例管理

### 6. 获取用例列表

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases

查询参数：
  priority              按优先级筛选（P0/P1/P2/P3）
  type                  按类型筛选（functional/interface/performance/security）
  search                搜索用例标题
  module_path           按目录筛选（精确匹配或前缀匹配，包含子目录）
  include_placeholders  是否包含占位用例（默认 false，构建目录树时传 true）
```

### 7. 创建用例

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases

{
  "title": "验证密码错误提示",     // 必填
  "priority": "P1",
  "type": "functional",
  "content": {
    "preconditions": "用户已注册",
    "steps": ["打开登录页面", "输入错误密码", "点击登录"],
    "expected_results": ["提示'密码错误'", "密码框清空"]
  },
  "tags": ["登录", "异常"]
}
```

> `case_id` 不传会自动生成（TC_001, TC_002...）

### 8. 获取用例详情

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}
```

### 9. 更新用例

```
PUT /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}

{
  "title": "新标题",
  "priority": "P0",
  "content": { ... }
}
```

### 10. 删除用例

```
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}
```

### 10.1 绑定单条用例到 TAPD 需求

```
PUT /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}/tapd-bind

{
  "tapd_story_url": "https://www.tapd.cn/12345678/stories/view/1112345678001001539",
  "tapd_story_title": "用户登录功能优化"
}
```

> 传入空字符串可清除绑定。

### 10.2 批量绑定 TAPD 需求

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases/tapd-bind-batch

{
  "tapd_story_url": "https://www.tapd.cn/...",
  "tapd_story_title": "需求标题",
  "module_path": "登录模块"       // 按目录批量绑定（二选一）
  // 或 "case_ids": [1, 2, 3]   // 按用例 ID 批量绑定（二选一）
}
```

> `module_path` 和 `case_ids` 二选一，指定要绑定的用例范围。

### 10.3 自动获取 TAPD 需求标题

```
GET /api/v1/tapd/story-title?story_url=https://www.tapd.cn/12345678/stories/view/1112345678001001539

响应：
{
  "id": "1112345678001001539",
  "title": "用户登录功能优化",
  "status": "developing",
  "priority": "High",
  "owner": "zhangsan"
}
```

> 根据需求链接自动从 TAPD API 获取标题等信息，用于粘贴链接时自动填充标题。

---

## 四、批量操作

### 11. 批量创建用例

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch

{
  "cases": [
    {"title": "验证空用户名", "priority": "P1", "type": "functional", "module_path": "登录模块/异常场景", "content": {...}, "tags": ["边界"]},
    {"title": "验证空密码", "priority": "P1", "type": "functional", "module_path": "登录模块/异常场景", "content": {...}, "tags": ["边界"]}
  ]
}
```

> **重要**：`module_path` 字段支持在批量创建时直接指定用例所属目录，用例会直接创建到对应目录中，无需二次移动。如果 `module_path` 对应的目录不存在，系统会自动创建占位用例来建立目录结构。
>
> 同时支持 `tapd_story_url` 和 `tapd_story_title` 字段，用于批量绑定 TAPD 需求。

### 12. 批量删除用例

```
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch

{
  "case_ids": [1, 2, 3]
}
```

> 批量删除前系统会自动创建快照。

---

## 五、导入导出

### 15. 导出 YAML

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/export/yaml
```

返回带注释的 YAML 格式文本文件。

### 16. 导入 YAML

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/import/yaml

{
  "yaml": "cases:\n  - id: TC_001\n    title: \"验证登录\"\n    priority: P0\n    ..."
}
```

### 17. 导出 XMind

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/export/xmind
```

返回 `.xmind` 文件（ZIP 格式），按优先级→类型分层。

---

## 七、功能全景关联与最近变更

### 18. 查询用例库关联的功能全景模块

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/panorama-links
```

返回该用例库或目录关联的功能全景模块、模块路径、关联目录和覆盖用例数。

### 19. 创建/维护用例库 ↔ 功能全景关联

```
POST /api/v1/testcase-panorama-links

{
  "workspace_id": 12,
  "module_id": 88,
  "library_id": 20,
  "module_path": "技能/连招",
  "link_level": "directory",
  "source": "agent"
}
```

`link_level` 可选：

- `library`：整库覆盖该功能模块
- `directory`：某个用例目录覆盖该功能模块
- `case`：单条用例覆盖该功能模块（预留/高级用法）

删除用例、用例库或功能全景模块时，Hub 会硬删除失效关联；审计信息写入变更日志。

### 20. 同步/清理关联

```
POST /api/v1/testcase-panorama-links/sync

{
  "project_id": 6,
  "workspace_id": 12,
  "library_id": 20,
  "dry_run": false
}
```

用于清理历史脏关联、重算 `case_count` 和功能全景测试覆盖指标。页面刷新不会自动全量同步；需要时主动调用。

### 21. 查询最近变更用例

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/changes?since=2026-06-15T00:00&until=2026-06-15T23:59
GET /api/v1/testcase-libraries/{LIBRARY_ID}/changes?module_path=技能/连招&change_type=updated
GET /api/v1/panorama/modules/{MODULE_ID}/testcase-changes?since=2026-06-15T00:00&include_children=1
```

返回新增/修改/删除/批量操作日志。Agent 收到“评审最近一天变更用例”时，优先用该接口，不要通过整库快照 diff 现场全量计算。

若从功能全景模块维度反查用例变更，使用 `testcase-changes` 接口。功能全景侧的覆盖与风险指标由 `test-metrics` 提供，其中 `bug_risk_score` / `bug_count` 可由 Agent 在 `game-module-panorama` Skill 中提交。

推荐汇报：

```markdown
## 最近变更用例
- 用例库：<library_name>
- 时间范围：<since> ~ <until>
- 新增 / 修改 / 删除：<n>/<n>/<n>
- 重点目录：<module_path>
- 关联功能模块：<module_path 列表>
```

---

## 八、版本管理（类 git）

版本管理支持对用例库进行快照、回滚、对比，防止误操作丢失数据。

### 22. 创建快照（commit）

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots

{
  "message": "AI 生成 15 条支付用例后",  // 提交说明
  "tag": "v1.0",                         // 可选标签
  "snapshot_type": "manual",              // manual/auto/ai
  "created_by": "龙虾王"
}
```

**响应**：
```json
{
  "version": 3,
  "case_count": 40,
  "message": "AI 生成 15 条支付用例后",
  "diff": {
    "added_count": 15,
    "removed_count": 0,
    "modified_count": 0,
    "added": ["TC_026", "TC_027", "..."],
    "removed": [],
    "modified": []
  }
}
```

### 23. 查看版本历史（log）

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots

查询参数：
  limit   返回条数（默认 50）
```

**响应**：
```json
[
  {"version": 3, "tag": "v1.0", "message": "AI 生成后", "case_count": 40, "snapshot_type": "manual", "created_at": "..."},
  {"version": 2, "message": "补充边界用例", "case_count": 25, "snapshot_type": "manual", "created_at": "..."},
  {"version": 1, "message": "初始版本", "case_count": 10, "snapshot_type": "manual", "created_at": "..."}
]
```

### 24. 查看某个版本详情（show）

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots/{VERSION}
```

返回该版本的完整用例数据（`cases_data` 字段）。

### 21. 回滚到某个版本（checkout）

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots/{VERSION}/checkout

{
  "message": "回滚原因说明",
  "created_by": "龙虾王"
}
```

**回滚流程**：
1. 自动保存当前状态为新快照（安全网，不会丢失）
2. 清空当前所有用例
3. 从目标版本的快照数据重建所有用例
4. 创建一个 `rollback` 类型的快照记录

**响应**：
```json
{
  "message": "已回滚到 v1",
  "restored_cases": 10,
  "saved_version": 4,
  "new_version": 5
}
```

### 22. 版本对比（diff）

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots/diff?from=1&to=3
```

**响应**：
```json
{
  "from_version": 1,
  "to_version": 3,
  "from_case_count": 10,
  "to_case_count": 40,
  "added": ["TC_011", "TC_012", "..."],
  "removed": [],
  "modified": [{"case_id": "TC_001", "title": "...", "fields": ["priority", "content"]}],
  "added_count": 30,
  "removed_count": 0,
  "modified_count": 1
}
```

### 21. 给版本打标签（tag）

```
PUT /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots/{VERSION}/tag

{
  "tag": "release-v2.0"
}
```

---

## 八、共享授权（v2 新增）

> 把用例库读/评审/编辑权限开放给指定用户、指定 OpenClaw 实例，或全网公开。
> 共享只增加「访问权限」，不影响所有权（仍归 owner 管理）。

### 23. 列出当前库的所有共享授权

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/shares
```

**响应**：返回 active 状态的 grants 数组（已过期或撤销的不返回）。

### 24. 新增/更新共享授权（幂等）

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/shares

{
  "share_type": "user",            // 必填: user / claw / public
  "target_user_id": 12,            // share_type=user 时必填
  "target_claw_id": null,          // share_type=claw 时必填
  "permission": "reviewer",        // 默认 readonly；可选 readonly / reviewer / editor
  "note": "邀请评审 v2.0",
  "expires_at": "2026-12-31T23:59:59"   // 选填，null=永久
}
```

> **幂等**：对同一 (library, share_type, target) 重复 POST 会更新现有 grant（不会报 409），方便改 permission/note/expires_at。

### 25. 撤销某条共享授权

```
DELETE /api/v1/testcase-libraries/shares/{SHARE_ID}
```

### 26. 一键开启/关闭"全网公开"共享

```
POST   /api/v1/testcase-libraries/{LIBRARY_ID}/shares/public   # 开启（permission 默认 readonly，可在 body 指定）
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/shares/public   # 关闭
```

> 等价于对 `share_type=public` 做创建 / 撤销，是 UI 一键开关的快捷方式。

### 权限矩阵

| 权限 | 读用例 | 评审 approve/reject | 编辑用例 |
|---|---|---|---|
| `readonly` | ✅ | ❌ | ❌ |
| `reviewer` | ✅ | ✅ | ❌ |
| `editor`   | ✅ | ✅ | ✅ |

> 库的 owner / 项目 admin / super_admin 永远拥有全部权限，不受 share 影响。

---

## 九、评审工作流（v2: 支持子目录评审 + 并行 pending）

> v1 只支持「整库一起评」；v2 起支持按 **scope** 区分评审范围，同库内多个不同 scope 的 review 可以并行进行。
> 业务场景示例：每个新版本的用例先写到草稿目录（如 `_draft_v2.0/...`），对该子目录单独发起评审，approved 后批量改 `module_path` 合入主目录。

### 27. 列出某用例库的所有评审记录

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/reviews
```

**响应**：
```json
{
  "library_id": 1,
  "review_status": "pending_review",
  "current_review_id": 12,
  "reviews": [
    { ...TestCaseLibraryReview... },
    ...
  ],
  "total": 5
}
```

### 28. 发起一次评审请求 ★ v2 关键变化

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/reviews

{
  "scope_type": "module",                    // ★ 必填: library / module（cases 预留）；默认 library
  "scope_module_path": "登录/手机号登录",      // ★ scope_type=module 时必填，按前缀匹配整棵子树
  "submit_note": "本次重点评审登录新增接口",
  "scope_summary": "v2.0 登录模块",          // 选填；不填会自动生成"子目录 X（N 条）"或"整库（N 条）"
  "invited_reviewers": [
    {"type":"user","id":12,"name":"alice"},
    {"type":"claw","id":3, "name":"claw-qa-1"}
  ],
  "related_topic_id": 87                     // 选填：关联的 topic（讨论区）
}
```

**响应**：返回创建的 `TestCaseLibraryReview`（含自动统计的 `scope_case_count`）。

**权限**：作者 / 项目 admin / super_admin。

**冲突 (409)**：同 `(library_id, scope_type, scope_module_path)` 已存在 pending review 时返回：
```json
{
  "error": "该范围已有进行中的评审 #12，请先处理它再重新发起",
  "current_review_id": 12
}
```

> **不同 scope 仍可并行**。例如 `scope=library` 整库评审 + `scope=module path=登录` 子目录评审 + `scope=module path=支付` 子目录评审 三者可同时存在。

### 29. 通过评审

```
POST /api/v1/testcase-libraries/reviews/{REVIEW_ID}/approve

{
  "decision_note": "用例覆盖完整，通过"
}
```

**权限**：作者 / 项目 admin / super_admin **OR** 被授权 `reviewer` / `editor` 的用户。

> approve 后若库内还有其他 pending review，`library.review_status` **仍保持 `pending_review`**（不会被覆盖成 approved）。只有最后一个 pending 处理完才真正切到 approved。

### 30. 驳回评审

```
POST /api/v1/testcase-libraries/reviews/{REVIEW_ID}/reject

{
  "decision_note": "登录失败分支用例缺失，建议补充再发"
}
```

权限同 approve。

### 31. 撤回评审请求

```
POST /api/v1/testcase-libraries/reviews/{REVIEW_ID}/withdraw
```

**权限**：评审发起者本人 / 用例库管理者。

> withdraw 后若该库**之前曾有 approved 记录**，library.review_status 回到 approved；否则回到 draft。

### 32. 获取单条评审详情

```
GET /api/v1/testcase-libraries/reviews/{REVIEW_ID}
```

返回完整 `TestCaseLibraryReview`（包含 `invited_reviewers`、`scope_*`、`decision_*`）。

---

### 评审状态机

```
                 ┌──────────► approved (decided_by, decision_note)
                 │
draft ──submit──►submitted ──reject──► rejected
                 │
                 └──withdraw──► withdrawn  (回到 draft 或保持 approved)
```

### 评审记录时间线（v2.1：2026-04-25 新增）

> 每次 submit / approve / reject / withdraw 操作都会**额外**写一条到统一的 `review_comments` 表（多模块共用），用于多轮评审追溯。
> `decision_note` 字段保留不动，新表只是补充时间线。

```bash
# 拉这个用例库的完整评审历史（含 4 个模块通用接口）
GET /api/v1/review-comments?resource_type=testcase_library&resource_id={LIBRARY_ID}
```

返回示例（按 created_at 升序）：
```json
[
  {"action":"submit",   "from_status":"",          "to_status":"submitted", "author":"condibot", "author_type":"openclaw", "content":"v2.0 候选用例提审", "parent_review_id":12},
  {"action":"reject",   "from_status":"submitted", "to_status":"rejected",  "author":"龙虾王",   "author_type":"openclaw", "content":"登录失败分支缺失",   "parent_review_id":12},
  {"action":"submit",   "from_status":"",          "to_status":"submitted", "author":"condibot", "author_type":"openclaw", "content":"已补充失败分支",     "parent_review_id":13},
  {"action":"approve",  "from_status":"submitted", "to_status":"approved",  "author":"龙虾王",   "author_type":"openclaw", "content":"覆盖完整，通过",     "parent_review_id":13}
]
```

`parent_review_id` 关联回 `test_case_library_reviews.id`（每次新提交是一行新 review，所以同一个 library 多次提交会对应不同 parent_review_id）。

### 我的提交聚合（跨模块）

```bash
# 我作为 owner_username 提交的所有用例库（以及我作为 created_by 的 skill/rule、
# 我作为 source_openclaw 的 knowledge）+ 各自最新评审记录摘要
GET /api/v1/review-comments/my-submissions
GET /api/v1/review-comments/my-submissions?status=pending_review
```

### 子目录评审范围匹配规则

`scope_type=module` 时，后端按 `module_path` **前缀匹配整棵子树**：

```sql
TestCase.module_path = '登录/手机号'  OR  TestCase.module_path LIKE '登录/手机号/%'
```

所以「登录/手机号」会包含「登录/手机号/正常登录」「登录/手机号/异常」等所有子目录的用例。

### "草稿目录 → 评审 → 合入主体" 推荐工作流

```
1. POST /modules { parent_path: "", name: "_draft_v2.0" }     # 建草稿目录
2. POST /cases/batch { cases: [..., module_path: "_draft_v2.0/登录"] }   # 新版本用例先写草稿
3. POST /reviews { scope_type: "module", scope_module_path: "_draft_v2.0", submit_note: "v2.0 候选" }
4. 评审人 approve 后 → 批量 PUT /cases/{id} 把 module_path 从 "_draft_v2.0/登录" 改到 "登录"（合入主体）
5. DELETE /modules { path: "_draft_v2.0" }   # 清理空草稿目录
```

> 当前未提供"一键合入" API；批量改 `module_path` 后再删空目录即可。后续有强需求会加 `POST /testcase-libraries/{id}/reviews/{rid}/merge { target_path }`。

---

## 推荐工作流

### 日常用例维护

```
1. 收到测试需求
2. 查找/创建用例库 → GET/POST /testcase-libraries
3. 创建快照 → POST /snapshots { "message": "开始前基线" }
4. 批量创建用例 → POST /cases/batch
5. 人工审核，手动补充边界用例
6. 创建快照 → POST /snapshots { "message": "本轮完成", "tag": "v1.0" }
7. 汇报日报
```

### 版本管理最佳实践

```
- 大批量操作前：手动 commit 一个快照（有 message 和 tag）
- AI 生成后：commit 记录生成结果
- 发现问题：查看 log → diff 对比 → checkout 回滚
- 发版前：打 tag 标记里程碑版本（如 "release-v2.0"）
- 定期：每天自动 commit 一次快照（可配置为待办）
```

### 误操作恢复

```
1. 发现误删/误改
2. GET /snapshots → 查看版本历史
3. GET /snapshots/diff?from=当前&to=目标 → 确认差异
4. POST /snapshots/{目标版本}/checkout → 一键回滚
5. 回滚前的状态也自动保存了，安全无忧
```

### 共享 + 评审 联动（v2 推荐）

```
A. 自己评审整库
   POST /reviews { scope_type: "library", submit_note: "v1.0 上线前 review" }
   → 评审人（默认作者本人/项目 admin）approve

B. 邀请外部 OpenClaw 评审某子目录
   1) POST /shares { share_type: "claw", target_claw_id: 5, permission: "reviewer", note: "登录模块评审" }
   2) POST /reviews { scope_type: "module", scope_module_path: "登录", submit_note: "请 claw#5 帮看下" }
   3) 被邀请的 claw 看到 shared_with_me=true + can_review=true，调 approve/reject
   4) 评审完成后 DELETE /shares/{share_id} 撤销共享（或留着以备下次复用）

C. 全网公开（适合规范类用例库）
   POST /shares/public { permission: "readonly" }
   → 所有 OpenClaw 都能 GET 到，但不能改、不能 approve
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 201 | 创建成功 | 正常处理 |
| 400 | 参数错误 | 检查必填字段（如 scope_type=module 时 scope_module_path 必填） |
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` |
| 403 | 无权限 | 重新获取 Token，或检查 share permission（评审需 reviewer/editor） |
| 404 | 资源不存在 | 检查 ID/版本号 |
| 409 | 冲突 | 共享：同 (lib, type, target) 已存在；评审：同 (lib, scope_type, scope_module_path) 已有 pending |
| 500 | 服务器错误 | 查看 Hub 日志 |

---

## 触发词

- "创建用例库"
- "生成测试用例"
- "添加用例"
- "查看用例"
- "删除用例"
- "批量生成用例"
- "用例管理"
- "导出用例"
- "导出 YAML"
- "导出 XMind"
- "创建快照"
- "用例版本"
- "版本历史"
- "回滚用例"
- "用例回滚"
- "版本对比"
- "用例库备份"
- "新增目录"
- "创建目录"
- "删除目录"
- "用例目录"
- "模块目录"
- "用例库共享"
- "邀请评审"
- "共享用例库"
- "用例库授权"
- "公开用例库"
- "撤销共享"
- "发起评审"
- "用例库评审"
- "子目录评审"
- "整库评审"
- "通过评审"
- "驳回评审"
- "撤回评审"
- "草稿目录评审"
- "合入主体"

---

## 经验沉淀（Rule #15 + knowledge-manager 联动）

完成一次用例设计 / 评审 / 批量生成后，按 Rule #15 §3 决策树判断是否要沉淀经验：

| 你产出的内容 | 落层 | 动作 |
|---|---|---|
| 当前用例库的设计草稿、评审中间记录 | 本地 | 不上报 |
| 一次性的、单需求专用的设计技巧（首次出现） | Memos | `POST /memos/upsert tag=method scope_key={module}` |
| 同类设计模式 / 批量生成 prompt 模板 **重复 ≥ 2 次**（多需求复用） | MySQL | `POST /knowledge category=method scope=module` |
| 评审时反复出现的"易漏覆盖点 / 高频用例缺陷" | MySQL | `POST /knowledge category=pitfall scope=module` |
| 性能/兼容/安全等类目的稳定基线 | MySQL | `POST /knowledge category=perf-baseline` |

> 详细分层判断与 API 参数模板见 `knowledge-manager` skill §A/B/C；
> 升级触发条件自检表见 `knowledge-manager` skill §C；
> 一律遵循 Rule #15 §7 的七大禁止事项（不要把临时草稿直接写 MySQL，也不要把 SOP 长期只放 Memos）。
