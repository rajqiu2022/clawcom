# 项目与模块管理 (project-manager)

## 简介

本 Skill 用于查询和管理 Hub 中的项目与模块信息，让 OpenClaw 能够：

- 查询所属项目和模块信息
- 获取模块分类（一级模块体系）
- 了解项目下有哪些 OpenClaw 在工作

**触发词**：项目信息、模块列表、所属项目、模块分类

---

## 前置条件

| 变量 | 说明 |
|------|------|
| `HUB_URL` | Hub 服务地址 |
| `HUB_API_TOKEN` | API Token |

---

## API 接口

### 1. 项目列表

```
GET /api/v1/projects

返回：项目数组
[
  {
    "id": 1,
    "name": "QQ飞车",
    "description": "QQ飞车手游",
    "tapd_workspace_id": "12345678",
    "modules": [...]
  }
]
```

### 2. 创建项目

```
POST /api/v1/projects

请求体：
{
  "name": "QQ飞车",                    // 必填，唯一
  "description": "QQ飞车手游",
  "tapd_workspace_id": "12345678"      // 可选，TAPD 关联
}
```

### 3. 更新项目

```
PUT /api/v1/projects/{id}

请求体（部分更新）：
{
  "description": "更新描述",
  "tapd_workspace_id": "87654321"
}
```

### 4. 删除项目

```
DELETE /api/v1/projects/{id}
```

---

### 5. 模块列表（全局）

```
GET /api/v1/modules

查询参数：
  category=core_gameplay   按一级分类筛选

返回：模块数组
```

### 6. 创建模块

```
POST /api/v1/modules

请求体：
{
  "name": "匹配系统",               // 必填，唯一
  "description": "核心匹配逻辑",
  "category": "core_gameplay",      // 一级分类 key
  "project_id": 1                   // 可选，关联项目
}
```

### 7. 更新模块

```
PUT /api/v1/modules/{id}

请求体（部分更新）：
{
  "description": "更新描述",
  "category": "peripheral"
}
```

### 8. 删除模块

```
DELETE /api/v1/modules/{id}
```

### 9. 获取模块分类列表

```
GET /api/v1/modules/categories

返回：
{
  "peripheral": "外围系统",
  "core_gameplay": "核心单局",
  "commercialization": "商业化",
  "client_performance": "客户端性能",
  "server_special": "服务器专项",
  "other": "其他专项"
}
```

### 10. 项目下的模块

```
GET /api/v1/projects/{project_id}/modules

返回：该项目关联的模块列表
```

### 11. 为项目添加模块

```
POST /api/v1/projects/{project_id}/modules

请求体：
{
  "name": "大厅系统",
  "category": "peripheral"
}
```

---

## 一级模块分类

| 分类 key | 中文名 | 说明 |
|----------|--------|------|
| `peripheral` | 外围系统 | 商城、大厅、活动、社交系统 |
| `core_gameplay` | 核心单局 | 匹配、单局玩法、操控系统 |
| `commercialization` | 商业化 | 会员、充值、抽奖系统 |
| `client_performance` | 客户端性能 | 内存、包体、启动、帧率性能 |
| `server_special` | 服务器专项 | 服务器压测、网络延迟 |
| `other` | 其他专项 | 兼容性测试、安全测试 |

---

## 触发词速查

| 触发词 | 对应操作 |
|--------|----------|
| 项目列表 / 查项目 | GET /projects |
| 模块列表 / 查模块 | GET /modules |
| 模块分类 | GET /modules/categories |
| 创建项目 | POST /projects |
| 创建模块 | POST /modules |
| 项目下的模块 | GET /projects/{id}/modules |
