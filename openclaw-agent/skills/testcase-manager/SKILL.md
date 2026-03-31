# 用例库管理 (testcase-manager)

## 简介

本 Skill 用于管理 Hub 中心的测试用例库，支持用例库的创建、用例的增删改查、批量操作和 AI 智能生成。

**适用场景**：
- 根据需求文档自动生成测试用例
- 对已有用例库进行维护（添加、修改、删除用例）
- 按优先级、类型筛选和查询用例
- 批量创建/删除用例
- 通过 AI 对话方式管理用例

---

## 前置条件

- 已通过 `hub-connect` Skill 连接到 Hub
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

---

## Hub 地址

```
Hub 地址: http://your-hub-host:8088
API 前缀: /api/v1
```

## 认证方式

所有 API 调用需要在 Header 中携带 Token：
```
Authorization: Bearer {HUB_API_TOKEN}
```

---

## 数据结构

### 用例库 (TestCaseLibrary)

```json
{
  "id": 1,
  "name": "登录模块用例库",
  "description": "覆盖登录功能的所有测试场景",
  "project_name": "智能助手",
  "module_name": "登录",
  "owner": "龙虾王",
  "status": "active",
  "case_count": 25,
  "created_at": "2026-03-30T10:00:00",
  "updated_at": "2026-03-30T15:00:00"
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
    "steps": [
      "打开登录页面",
      "输入正确的用户名和密码",
      "点击登录按钮"
    ],
    "expected_results": [
      "登录成功",
      "跳转到首页",
      "显示用户昵称"
    ]
  },
  "tags": ["登录", "冒烟测试"],
  "ai_generated": false,
  "created_at": "2026-03-30T10:00:00"
}
```

**优先级**：`P0`（最高）、`P1`（高）、`P2`（中）、`P3`（低）

**用例类型**：`functional`（功能测试）、`interface`（接口测试）、`performance`（性能测试）、`security`（安全测试）

---

## API 接口

### 一、用例库管理

#### 1. 获取用例库列表

**接口**：`GET /api/v1/testcase-libraries`

**查询参数**：
- `project_name`：按项目筛选
- `search`：搜索关键词（匹配名称和描述）

**响应示例**：
```json
[
  {
    "id": 1,
    "name": "登录模块用例库",
    "project_name": "智能助手",
    "case_count": 25,
    "status": "active"
  }
]
```

---

#### 2. 创建用例库

**接口**：`POST /api/v1/testcase-libraries`

**请求体**：
```json
{
  "name": "登录模块用例库",
  "description": "覆盖登录功能的所有测试场景",
  "project_name": "智能助手",
  "module_name": "登录",
  "owner": "龙虾王"
}
```

**必填字段**：`name`

---

#### 3. 获取用例库详情

**接口**：`GET /api/v1/testcase-libraries/{LIBRARY_ID}`

**响应**：包含用例库基本信息和所有用例列表。

---

#### 4. 更新用例库

**接口**：`PUT /api/v1/testcase-libraries/{LIBRARY_ID}`

**请求体**（只传需要更新的字段）：
```json
{
  "name": "新名称",
  "description": "新描述"
}
```

---

#### 5. 删除用例库

**接口**：`DELETE /api/v1/testcase-libraries/{LIBRARY_ID}`

> 注意：删除用例库会同时删除该库下所有用例，不可恢复。

---

### 二、用例管理

#### 6. 获取用例列表

**接口**：`GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases`

**查询参数**：
- `priority`：按优先级筛选（P0/P1/P2/P3）
- `type`：按类型筛选（functional/interface/performance/security）
- `search`：搜索用例标题

**响应示例**：
```json
[
  {
    "id": 1,
    "case_id": "TC_001",
    "title": "验证正确用户名密码登录",
    "priority": "P0",
    "type": "functional",
    "content": {...},
    "tags": ["登录", "冒烟测试"]
  }
]
```

---

#### 7. 创建用例

**接口**：`POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases`

**请求体**：
```json
{
  "title": "验证密码错误提示",
  "priority": "P1",
  "type": "functional",
  "content": {
    "preconditions": "用户已注册",
    "steps": [
      "打开登录页面",
      "输入正确用户名和错误密码",
      "点击登录按钮"
    ],
    "expected_results": [
      "登录失败",
      "提示'密码错误'",
      "密码输入框清空"
    ]
  },
  "tags": ["登录", "异常"]
}
```

**必填字段**：`title`

**说明**：`case_id` 如果不传会自动生成（TC_001, TC_002...）

---

#### 8. 获取用例详情

**接口**：`GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}`

---

#### 9. 更新用例

**接口**：`PUT /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}`

**请求体**（只传需要更新的字段）：
```json
{
  "title": "新标题",
  "priority": "P0",
  "content": {
    "preconditions": "...",
    "steps": ["..."],
    "expected_results": ["..."]
  }
}
```

---

#### 10. 删除用例

**接口**：`DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/cases/{CASE_ID}`

---

### 三、批量操作

#### 11. 批量创建用例

**接口**：`POST /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch`

**请求体**：
```json
{
  "cases": [
    {
      "title": "验证空用户名提交",
      "priority": "P1",
      "type": "functional",
      "content": {
        "preconditions": "打开登录页面",
        "steps": ["不输入用户名", "点击登录"],
        "expected_results": ["提示'用户名不能为空'"]
      },
      "tags": ["登录", "边界"]
    },
    {
      "title": "验证空密码提交",
      "priority": "P1",
      "type": "functional",
      "content": {
        "preconditions": "打开登录页面",
        "steps": ["输入用户名", "不输入密码", "点击登录"],
        "expected_results": ["提示'密码不能为空'"]
      },
      "tags": ["登录", "边界"]
    }
  ]
}
```

**响应**：
```json
{
  "message": "成功创建 2 个用例",
  "cases": [...]
}
```

---

#### 12. 批量删除用例

**接口**：`DELETE /api/v1/testcase-libraries/{LIBRARY_ID}/cases/batch`

**请求体**：
```json
{
  "case_ids": [1, 2, 3]
}
```

---

### 四、AI 智能生成

#### 13. AI 批量生成用例

**接口**：`POST /api/v1/ai/testcases/generate`

**请求体**：
```json
{
  "library_id": 1,
  "requirement": "用户登录功能，包括正常登录、密码错误、账号锁定、验证码等场景",
  "count": 10,
  "type": "functional"
}
```

**说明**：AI 会根据需求描述自动生成指定数量的用例，直接写入用例库。

---

#### 14. AI 对话管理用例

**接口**：`POST /api/v1/ai/testcases/chat`

**请求体**：
```json
{
  "library_id": 1,
  "message": "帮我增加3个关于验证码的测试用例"
}
```

**说明**：通过自然语言与 AI 对话，AI 会理解意图后自动执行添加/修改/删除用例的操作。

---

## 工作流程

### 典型使用流程

```
1. 查看用例库
   └── GET /testcase-libraries?project_name=xxx → 获取项目下的用例库

2. 选择用例库（或创建新的）
   ├── 已有 → GET /testcase-libraries/{id} 查看详情
   └── 新建 → POST /testcase-libraries

3. 管理用例
   ├── 手动添加 → POST /testcase-libraries/{id}/cases
   ├── AI 批量生成 → POST /ai/testcases/generate
   ├── AI 对话 → POST /ai/testcases/chat
   ├── 修改用例 → PUT /testcase-libraries/{id}/cases/{cid}
   └── 删除用例 → DELETE /testcase-libraries/{id}/cases/{cid}

4. 批量操作
   ├── 批量创建 → POST /testcase-libraries/{id}/cases/batch
   └── 批量删除 → DELETE /testcase-libraries/{id}/cases/batch
```

### 需求分析→用例生成流程

```
1. 收到测试需求（如 TAPD 需求单、口头需求）
2. 确认项目和模块 → 查找或创建对应的用例库
3. 调用 AI 生成 → POST /ai/testcases/generate
4. 人工审核 AI 生成的用例 → 查看列表确认
5. 补充边界用例 → 手动添加或 AI 对话补充
6. 汇报 → 在日报中记录用例产出数量
```

---

## 代码示例

### Python - 创建用例库并 AI 生成用例

```python
import requests

HUB_URL = "http://your-hub-host:8088"
TOKEN = "your_token_here"
headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# 1. 创建用例库
lib = requests.post(f"{HUB_URL}/api/v1/testcase-libraries", json={
    "name": "支付模块用例库",
    "project_name": "电商平台",
    "module_name": "支付",
    "owner": "龙虾王"
}, headers=headers).json()

library_id = lib["id"]
print(f"用例库已创建: {lib['name']} (ID: {library_id})")

# 2. AI 批量生成用例
result = requests.post(f"{HUB_URL}/api/v1/ai/testcases/generate", json={
    "library_id": library_id,
    "requirement": "微信支付功能，包括正常支付、余额不足、超时、退款等场景",
    "count": 15,
    "type": "functional"
}, headers=headers).json()

print(f"AI 生成: {result.get('message')}")

# 3. 查看生成的用例
cases = requests.get(
    f"{HUB_URL}/api/v1/testcase-libraries/{library_id}/cases",
    headers=headers
).json()

for c in cases:
    print(f"  [{c['priority']}] {c['case_id']}: {c['title']}")
```

### Python - 手动添加用例

```python
# 添加单个用例
case = requests.post(
    f"{HUB_URL}/api/v1/testcase-libraries/{library_id}/cases",
    json={
        "title": "验证支付金额为0时的处理",
        "priority": "P1",
        "type": "functional",
        "content": {
            "preconditions": "用户已登录，购物车有商品",
            "steps": [
                "修改订单金额为0",
                "点击支付按钮"
            ],
            "expected_results": [
                "系统拒绝支付",
                "提示'支付金额不能为0'"
            ]
        },
        "tags": ["支付", "边界值"]
    },
    headers=headers
).json()

print(f"用例已创建: {case['case_id']} - {case['title']}")
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
| 404 | 资源不存在 | 检查 ID 是否正确 |
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
- "测试用例"
