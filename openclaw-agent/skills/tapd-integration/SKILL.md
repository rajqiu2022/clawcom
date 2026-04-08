# TAPD 集成 (tapd-integration)

## 简介

本 Skill 用于从 Hub 查询 TAPD 项目管理数据，让 OpenClaw 能够：

- 查询当前迭代的需求列表和 Bug 列表
- 获取 Bug/需求的状态分布和趋势
- 查看 TAPD Dashboard 统计
- 根据需求/Bug 信息指导测试工作

**触发词**：TAPD、需求列表、Bug列表、迭代、需求状态、Bug统计

---

## 前置条件

| 变量 | 说明 |
|------|------|
| `HUB_URL` | Hub 服务地址 |
| `HUB_API_TOKEN` | API Token |

TAPD 配置（workspace_id、api_user、api_password）由 Hub 管理员在系统设置中配置。

---

## API 接口

### 1. 获取 TAPD 配置

```
GET /api/v1/tapd/config

返回：
{
  "workspace_id": "12345678",
  "api_user": "xxx",
  "has_password": true
}
```

### 2. 更新 TAPD 配置（管理员）

```
PUT /api/v1/tapd/config

请求体：
{
  "workspace_id": "12345678",
  "api_user": "xxx",
  "api_password": "xxx"
}
```

### 3. 查询需求列表

```
GET /api/v1/tapd/stories

查询参数：
  iteration_id=123    按迭代筛选
  status=planning     按状态筛选：planning / developing / testing / closed
  keyword=登录        按关键词搜索
  limit=50            返回条数（默认 50）

返回：需求列表数组，每条包含 id、name、status、priority、owner、iteration_id 等
```

### 4. 查询 Bug 列表

```
GET /api/v1/tapd/bugs

查询参数：
  iteration_id=123    按迭代筛选
  status=new          按状态筛选：new / open / fixed / closed / rejected
  severity=fatal      按严重度筛选：fatal / serious / normal / prompt / low
  keyword=闪退        按关键词搜索
  limit=50            返回条数（默认 50）

返回：Bug 列表数组，每条包含 id、title、status、severity、priority、reporter、fixer 等
```

### 5. 查询迭代列表

```
GET /api/v1/tapd/iterations

查询参数：
  status=open         按状态筛选：open / done

返回：迭代列表数组，每条包含 id、name、startdate、enddate、status
```

### 6. TAPD Dashboard 统计

```
GET /api/v1/tapd/dashboard

返回：
{
  "stories": { "total": 50, "by_status": {"planning": 10, "testing": 15, ...} },
  "bugs": { "total": 30, "by_severity": {"fatal": 2, "serious": 8, ...}, "by_status": {...} },
  "iterations": { "total": 5, "open": 2 }
}
```

---

## 推荐工作流

### 测试前准备

```
1. GET /tapd/iterations?status=open → 找到当前迭代
2. GET /tapd/stories?iteration_id=xxx&status=testing → 获取待测需求
3. 根据需求列表规划测试用例
```

### 日常 Bug 跟踪

```
1. GET /tapd/bugs?status=new → 查看新 Bug
2. GET /tapd/bugs?severity=fatal&status=open → 紧急关注致命 Bug
3. GET /tapd/dashboard → 查看整体趋势
```

### 日报引用

```
1. GET /tapd/bugs?status=closed → 今日关闭的 Bug
2. GET /tapd/stories?status=testing → 正在测试的需求
3. 引用到日报的 tasks_completed 中
```

---

## 触发词速查

| 触发词 | 对应操作 |
|--------|----------|
| 需求列表 / 查需求 | GET /tapd/stories |
| Bug 列表 / 查 Bug | GET /tapd/bugs |
| 当前迭代 / 迭代列表 | GET /tapd/iterations |
| TAPD 统计 / 看板 | GET /tapd/dashboard |
| 致命 Bug / 紧急 Bug | GET /tapd/bugs?severity=fatal&status=open |
| 待测需求 | GET /tapd/stories?status=testing |
