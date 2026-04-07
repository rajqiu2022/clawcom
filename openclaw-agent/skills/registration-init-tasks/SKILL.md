# 注册初始化任务管理 (registration-init-tasks)

## 简介

本 Skill 定义了新 OpenClaw 注册到 Hub 时自动下发的初始化待办任务列表。管理员（龙虾王）可以修改此 Skill 来调整：

- 新增/删除/修改初始化任务
- 调整任务的紧急度和优先级
- 自定义验证目标和执行说明

**修改此 Skill 后**，新注册的 OpenClaw 会自动按最新配置下发待办任务。已注册的不受影响。

---

## Hub 地址

```
Hub API: http://your-hub-host:8088/api/v1
认证: Authorization: Bearer {HUB_API_TOKEN}
```

---

## 当前初始化任务列表

### 任务 1：验证 Memos 连接

| 属性 | 值 |
|------|-----|
| 标题 | 验证 Memos 连接 |
| 描述 | 确认你能正常访问 Memos 服务，执行一次读取操作（如获取最近的 memo 列表）。如果不可用，报告具体错误。 |
| 紧急度 | 🔄 background（后台/空闲时做） |
| 优先级 | P0 |
| 验证目标 | `memos-access` |
| 频率 | once（一次性） |
| 类别 | init（初始化） |

### 任务 2：验证本地 Skills 加载

| 属性 | 值 |
|------|-----|
| 标题 | 验证本地 Skills 加载 |
| 描述 | 检查本地 ~/.qclaw/skills/ 目录下的 Skill 文件是否正确加载。列出已加载的 Skill 名称和文件数量。 |
| 紧急度 | 🔄 background |
| 优先级 | P0 |
| 验证目标 | `local-skills` |
| 频率 | once |
| 类别 | init |

### 任务 3：验证 Hub 通信

| 属性 | 值 |
|------|-----|
| 标题 | 验证 Hub 通信 |
| 描述 | 调用 Hub 心跳接口 POST /heartbeat，确认返回 200 且 pending_messages 字段存在。 |
| 紧急度 | 🔁 retry（可重试，失败延后5分钟再试1次） |
| 优先级 | P0 |
| 验证目标 | `hub-heartbeat` |
| 频率 | once |
| 类别 | init |

### 任务 4：执行首次日报上报

| 属性 | 值 |
|------|-----|
| 标题 | 执行首次日报上报 |
| 描述 | 生成一份简单的注册日报，包含：1) 注册时间 2) 已安装的 Skills/Rules 列表 3) 初始化任务完成情况。通过 POST /report 上报。 |
| 紧急度 | 📋 flexible（当天完成即可） |
| 优先级 | P1 |
| 验证目标 | `first-report` |
| 频率 | once |
| 类别 | init |

---

## 管理 API

### 查看当前初始化任务配置

```
GET /api/v1/registration/init-tasks

返回当前配置的所有初始化任务列表。
```

### 更新初始化任务配置

```
PUT /api/v1/registration/init-tasks

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "tasks": [
    {
      "title": "验证 Memos 连接",
      "description": "确认你能正常访问 Memos 服务...",
      "priority": "P0",
      "urgency_level": "background",
      "verification_target": "memos-access"
    },
    {
      "title": "验证本地 Skills 加载",
      "description": "检查本地 ~/.qclaw/skills/ 目录...",
      "priority": "P0",
      "urgency_level": "background",
      "verification_target": "local-skills"
    }
  ]
}

返回：{ "message": "已更新 N 个初始化任务", "count": N }
```

### 查看某个 OpenClaw 的初始化任务完成情况

```
GET /api/v1/openclaws/{CLAW_ID}/todos?category=init

返回该 OpenClaw 的所有 init 类任务及今日状态。
```

### 手动为已注册的 OpenClaw 补发初始化任务

```
POST /api/v1/openclaws/{CLAW_ID}/init-tasks

Header:
  Authorization: Bearer {HUB_API_TOKEN}

返回：{ "message": "已下发 N 个初始化任务", "count": N }
```

---

## 字段说明

### urgency_level 紧急度

| 值 | 中文 | 说明 |
|----|------|------|
| `interrupt` | ⚡ 定时中断 | 到点立即中断当前任务执行 |
| `flexible` | 📋 当天弹性 | 当天完成即可 |
| `background` | 🔄 后台任务 | 无时间要求，空闲时做 |
| `periodic` | 🔁 周期-可跳过 | 错过就下次 |
| `retry` | 🔁 周期-可重试 | 错过延后重试 |

### priority 优先级

| 值 | 中文 |
|----|------|
| `P0` | 最高 |
| `P1` | 高 |
| `P2` | 中 |
| `P3` | 低 |

### verification_target 验证目标

自定义字符串，用于标识这个任务验证的是什么能力。OpenClaw 完成后上报时会带上此值。

---

## 如何修改初始化任务

### 方式 1：通过 API 修改（推荐）

```
PUT /api/v1/registration/init-tasks
```

直接提交新的任务列表，立即生效，后续新注册的 OpenClaw 会按新配置下发。

### 方式 2：告诉龙虾王修改

龙虾王可以用自然语言描述需求，例如：
- "注册时增加一个验证 TAPD 连接的任务"
- "把首次日报的紧急度改为 interrupt"
- "删除 Memos 验证任务"
- "增加一个验证用例库操作的初始化任务"

龙虾王解析需求后调用 PUT API 完成修改。

---

## 触发词

- "查看初始化任务"、"注册时的待办"
- "修改初始化任务"、"调整注册待办"
- "补发初始化任务"、"重新下发 init 任务"
- "初始化任务完成情况"
