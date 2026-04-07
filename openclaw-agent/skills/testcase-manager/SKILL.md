# 用例库管理 (testcase-manager)

## 简介

本 Skill 用于管理 Hub 中心的测试用例库，支持完整的用例生命周期管理：

- **CRUD**：用例库和用例的增删改查
- **批量操作**：批量创建/删除用例
- **AI 智能生成**：通过需求描述自动生成用例、对话式管理
- **导入导出**：YAML / XMind 格式
- **版本管理**：类 git 的快照/回滚/对比（commit / log / checkout / diff）

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
| `tags` | 选填 | 标签 | string[] | 自定义标签，如：回归、冒烟、核心流程 | ["冒烟", "核心流程"] |
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

- 已通过 `hub-connect` Skill 连接到 Hub
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub 地址: http://your-hub-host:8088
API 前缀: /api/v1
```

## 认证方式

```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

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
  "case_count": 25
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

## 二、用例管理

### 6. 获取用例列表

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases

查询参数：
  priority  按优先级筛选（P0/P1/P2/P3）
  type      按类型筛选（functional/interface/performance/security）
  search    搜索用例标题
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

---

## 三、批量操作

### 11. 批量创建用例

```
POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch

{
  "cases": [
    {"title": "验证空用户名", "priority": "P1", "type": "functional", "content": {...}, "tags": ["边界"]},
    {"title": "验证空密码", "priority": "P1", "type": "functional", "content": {...}, "tags": ["边界"]}
  ]
}
```

### 12. 批量删除用例

```
DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch

{
  "case_ids": [1, 2, 3]
}
```

> 批量删除前系统会自动创建快照。

---

## 四、AI 智能生成

### 13. AI 批量生成用例

```
POST /api/v1/ai/testcases/generate

{
  "library_id": 1,
  "requirement": "用户登录功能，包括正常登录、密码错误、账号锁定、验证码等场景",
  "count": 10,
  "type": "functional"
}
```

> AI 根据需求描述自动生成用例，直接写入用例库。

### 14. AI 对话管理用例

```
POST /api/v1/ai/testcases/chat

{
  "library_id": 1,
  "message": "帮我增加3个关于验证码的测试用例"
}
```

> 自然语言对话，AI 理解意图后自动执行添加/修改/删除。

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

## 六、版本管理（类 git）

版本管理支持对用例库进行快照、回滚、对比，防止误操作丢失数据。

### 18. 创建快照（commit）

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

### 19. 查看版本历史（log）

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

### 20. 查看某个版本详情（show）

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

### 23. 给版本打标签（tag）

```
PUT /api/v1/testcase-libraries/{LIBRARY_ID}/snapshots/{VERSION}/tag

{
  "tag": "release-v2.0"
}
```

---

## 推荐工作流

### 日常用例维护

```
1. 收到测试需求
2. 查找/创建用例库 → GET/POST /testcase-libraries
3. 创建快照 → POST /snapshots { "message": "开始前基线" }
4. AI 生成用例 → POST /ai/testcases/generate
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

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 201 | 创建成功 | 正常处理 |
| 400 | 参数错误 | 检查必填字段 |
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` |
| 403 | Token 无效 | 重新获取 Token |
| 404 | 资源不存在 | 检查 ID/版本号 |
| 500 | 服务器错误 | 查看 Hub 日志 |

---

## 触发词

- "创建用例库"
- "生成测试用例"
- "添加用例"
- "查看用例"
- "删除用例"
- "批量生成用例"
- "AI 生成用例"
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
