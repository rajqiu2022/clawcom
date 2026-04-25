# 测试用例字段 Schema 定义

## 完整字段结构

```
TestCase {
  case_id:    string          // 用例编号，格式 TC_XXX（选填，自动生成）
  title:      string          // 用例名称（必填，≤255字符）
  priority:   enum            // 优先级（必填）
  type:       enum            // 用例类型（必填）
  tags:       string[]        // 标签（选填）
  content: {
    module_name:      enum        // 所属模块（必填）
    status:           enum        // 用例状态（选填，默认 normal）
    preconditions:    string      // 前置条件（必填）
    steps:            string[]    // 操作步骤（必填）
    expected_results: string[]    // 预期结果（必填，与 steps 等长）
    notes:            string      // 备注（选填）
  }
}
```

## 枚举值定义

### priority（优先级）

| 值 | 中文含义 | 说明 |
|----|---------|------|
| `P0` | 最高 | 核心流程，阻塞级缺陷对应的验证用例 |
| `P1` | 高 | 重要功能路径，主干业务逻辑验证 |
| `P2` | 中 | 次要功能、边界条件、异常分支验证 |
| `P3` | 低 | UI 细节、提示文案、非关键交互验证 |

### type（用例类型）

| 值 | 中文含义 | 适用场景 |
|----|---------|---------|
| `functional` | 功能测试 | 验证功能是否按预期工作 |
| `interface` | 接口测试 | 验证 API 请求/响应、参数校验、错误码 |
| `performance` | 性能测试 | 验证响应时间、吞吐量、资源占用 |
| `security` | 安全测试 | 验证权限控制、数据安全、注入防护 |

### content.module_name（所属模块）

| 值 | 适用范围 |
|----|---------|
| `外围系统` | 登录、注册、好友、聊天、大厅、设置等外围功能 |
| `核心单局` | 游戏核心玩法、单局内战斗/操作逻辑 |
| `商业化` | 充值、商城、活动、道具、抽奖等付费相关 |
| `客户端性能` | 帧率、内存、加载耗时、包体大小、卡顿 |
| `服务器专项` | 服务端接口、并发、稳定性、压力测试 |
| `其他专项` | 不属于以上分类的专项测试 |

### content.status（用例状态）

| 值 | 中文含义 | 说明 |
|----|---------|------|
| `normal` | 正常 | 用例有效可执行（默认值） |
| `pending` | 待定 | 用例待确认或待补充 |
| `deprecated` | 废弃 | 用例已过期不再执行 |

## 约束规则

### 1. 必填校验

以下字段为必填项，缺少任何一个则判定用例不合格：

- `title`
- `content.module_name`
- `priority`
- `type`
- `content.preconditions`
- `content.steps`
- `content.expected_results`

### 2. 长度约束

| 字段 | 约束 |
|------|------|
| `title` | ≤ 255 字符 |
| `content.steps` | ≥ 1 条 |
| `content.expected_results` | ≥ 1 条 |

### 3. 对齐约束

```
len(content.steps) === len(content.expected_results)
```

steps 和 expected_results 数组长度必须严格相等。每一条 step 在相同索引位置都必须有对应的 expected_result。

### 4. 编号规则

- 格式: `TC_` + 三位数字
- 范围: `TC_001` ~ `TC_999`
- 递增: 批量生成时从 `TC_001` 开始连续递增
- 续编: 若上下文已有编号，从最大编号 +1 继续
- 唯一: 同一批次内不允许重复编号

### 5. 标签规范

常用标签参考值：

| 标签 | 含义 |
|------|------|
| `冒烟` | 冒烟测试覆盖范围 |
| `回归` | 回归测试覆盖范围 |
| `核心流程` | 核心业务流程 |
| `边界` | 边界值测试 |
| `异常` | 异常场景测试 |
| `兼容性` | 兼容性测试 |
| `新增` | 新功能新增用例 |
| `优化` | 功能优化对应用例 |

## JSON Schema（形式化定义）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TestCase",
  "type": "object",
  "required": ["title", "priority", "type", "content"],
  "properties": {
    "case_id": {
      "type": "string",
      "pattern": "^TC_\\d{3}$",
      "description": "用例编号，格式 TC_XXX"
    },
    "title": {
      "type": "string",
      "maxLength": 255,
      "minLength": 1,
      "description": "用例名称"
    },
    "priority": {
      "type": "string",
      "enum": ["P0", "P1", "P2", "P3"],
      "description": "优先级"
    },
    "type": {
      "type": "string",
      "enum": ["functional", "interface", "performance", "security"],
      "description": "用例类型"
    },
    "tags": {
      "type": "array",
      "items": { "type": "string" },
      "description": "标签"
    },
    "content": {
      "type": "object",
      "required": ["module_name", "preconditions", "steps", "expected_results"],
      "properties": {
        "module_name": {
          "type": "string",
          "enum": ["外围系统", "核心单局", "商业化", "客户端性能", "服务器专项", "其他专项"],
          "description": "所属模块"
        },
        "status": {
          "type": "string",
          "enum": ["normal", "pending", "deprecated"],
          "default": "normal",
          "description": "用例状态"
        },
        "preconditions": {
          "type": "string",
          "minLength": 1,
          "description": "前置条件"
        },
        "steps": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1,
          "description": "操作步骤"
        },
        "expected_results": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1,
          "description": "预期结果"
        },
        "notes": {
          "type": "string",
          "description": "备注"
        }
      }
    }
  }
}
```
