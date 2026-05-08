# 需求分析中心 (requirement-analysis)

## 简介

本 Skill 让 OpenClaw Agent 完成 **TAPD 需求拉取 → Hub 快照推送 → 字段级变更跟踪 → 工程/用例闭环** 的全流程。

核心使命：

- **快照同步**：按 Hub 测试迭代（TestIteration）维度拉取 TAPD Story 全量字段，POST 到 Hub `/requirements/iterations/<id>/snapshots` 落库
- **下拉缓存**：维护 `tapd_iterations_cache` / `tapd_versions` / `tapd_baselines` / `tapd_field_map_cache`，让前端 TestIteration 创建/编辑表单的"TAPD 迭代下拉"零延迟
- **字段级变更日志**：Hub 端在 upsert 时自动 diff 并写入 `requirement_change_logs`，Agent 不必关心 diff 算法
- **工程关联**：基于 `tapd_story_id` 自动建立**函数/方法级**关联（`requirement_function_links`，包含 `file_path + symbol_name + start_line + end_line`）
- **响应实时刷新**：反向轮询 `/requirements/agent/tapd-refresh-queue`，当用户在 Hub 点击"实时刷新"按钮，agent 在 ≤15s 内拉取最新 TAPD 数据并推送
- **每日全量**：定时任务（cron 或 APScheduler）每日凌晨同步所有活跃迭代，作为 baseline

---

## 前置条件

- 已完成 OpenClaw Agent 注册并启动 `hub-sse-sidecar`
- 拥有有效的 `HUB_API_TOKEN`、`CLAW_ID`
- **TAPD 鉴权零配置**：本 Skill 通过 `mcporter-internal` 调用 TAPD MCP 服务，鉴权使用 **太湖 Token（Taihu Token）** —— 由 Knot 平台官方内置打通，**无需** Agent 任何手动配置；不要在 Agent 代码 / 配置 / Git 中存放 TAPD 凭证或 Taihu Token
- Hub 端已部署 `requirement-analysis` 模块（9 张表 + `/api/v1/requirements/*` 蓝图）
- LLM 配置可用：默认走 Hub 的 `system_config.llm_*`（豆包 doubao-pro-32k）

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

## 数据流总览

```
┌──────────────┐  1. pick refresh    ┌──────────────┐  2. mcp 调 TAPD   ┌─────────────┐
│  Hub UI 点击  │ ──────────────────> │ OpenClaw     │ ────────────────> │ mcporter-   │
│ "实时刷新"   │                     │ Agent        │                   │ internal    │
└──────────────┘                     │              │                   │ (Taihu Tok) │
       ▲                             │              │ <──── stories ─── │             │
       │ 5. 轮询查 status             │              │                   └─────────────┘
       │                             │  3. 字段映射  │                          │
┌──────────────┐  4. push snapshot   │  + 抽 HTML   │                          │
│  Hub /api/v1/│ <─────────────────  │              │                   ┌─────────────┐
│ requirements/│                     │              │                   │ TAPD V2     │
│ snapshots    │                     │              │                   │ (内部 API)  │
└──────────────┘                     └──────────────┘                   └─────────────┘
       │
       │ 自动 diff（Hub 内）
       v
┌──────────────────────────────────────┐
│ requirement_items（upsert）          │
│ requirement_change_logs（diff 写）   │
│ requirement_function_links（自动）   │
└──────────────────────────────────────┘
```

---

## 需求快照格式规范

> **OpenClaw 提交 stories 数组到 Hub 时必须严格遵守本节字段、枚举值、校验规则。**

### 一、Story 字段映射表（TAPD → Hub）

OpenClaw 推送 `stories[]` 时 **直接使用 TAPD 原字段名**（蛇形命名），Hub 端会按下表映射到本地列：

| TAPD 字段 | Hub 字段 | 类型 | 说明 |
|-----------|----------|------|------|
| `id` | `tapd_story_id` | string | **必填**，TAPD Story 主键 |
| `name` | `title` | string(≤500) | 需求标题 |
| `description` | `description` + `description_text`（自动抽 HTML） | text | HTML 富文本 |
| `status` | `status` | string(≤40) | TAPD 状态键，如 `status_3`/`status_22`/`status_resolved` |
| `priority_label` 或 `priority` | `priority_label` | string(≤40) | High / Middle / Low / Nice To Have |
| `owner` | `owner` | string(≤500) | 处理人，多人用 `;` 分隔 |
| `creator` | `creator` | string(≤100) | |
| `developer` | `developer` | string(≤500) | |
| `category_id` | `category_id` | string(≤64) | TAPD 需求分类 |
| `workitem_type_id` | `workitem_type_id` | string(≤64) | TAPD 需求类别 |
| `module` | `tapd_module` | string(≤200) | TAPD 模块名 |
| `feature` | `feature` | string(≤200) | TAPD 特性名 |
| `version` | `tapd_version` | string(≤100) | 外发版本号，如 `M1版本` |
| `release_id` | `tapd_release_id` | string(≤64) | TAPD 版本 ID |
| `baseline_id` | `tapd_baseline_id` | string(≤64) | 转测基线 ID |
| `iteration_id` | `tapd_iteration_id` | string(≤64) | TAPD 迭代 ID（≠ Hub iteration_id） |
| `custom_field_eight` 或 `acceptance_criteria` | `acceptance_criteria` | text | **测试验收标准**（关键） |
| `custom_field_three` 或 `test_focus` | `test_focus` | text | 测试关注点 |
| `test_suggestions` | `test_suggestions` | JSON string（LONGTEXT） | 测试点建议（建议传 JSON 数组/对象） |
| `custom_field_six` 或 `test_result` | `test_result` | text | 测试结果 |
| `custom_field_18` 或 `need_test` | `need_test` | string(≤40) | 是否需要测试 |
| `custom_field_19` 或 `review_progress` | `review_progress` | string(≤40) | 评审进度 |
| `parent_id` / `children_id` / `path` | `parent_id` / `children_id` / `tree_path` | string | 层级关系 |
| `progress` / `effort` / `effort_completed` / `remain` | 同名 | int/float | 进度工时 |
| `created` / `modified` / `completed` | `tapd_created_at` / `tapd_modified_at` / `tapd_completed_at` | datetime | TAPD 时间，格式 `YYYY-MM-DD HH:MM:SS` |
| `begin` / `due` | `tapd_begin` / `tapd_due` | date | TAPD 排期，格式 `YYYY-MM-DD` |
| `impl_status` | `impl_status` | string(≤20) | **Agent 填写**，实现状态：`not_impl`/`in_progress`/`implemented`/`unknown` |
| `impl_remark` | `impl_remark` | string(≤500) | **Agent 填写**，实现状态备注（如"服务端已完成，客户端未开始"） |

> **额外**：完整原始 JSON 会落库到 `raw_payload`（LONGTEXT），便于以后扩展字段不用 ALTER。

### 二、自定义字段映射（custom_field_*）

不同 workspace 的 `custom_field_eight` 可能含义不同。**OpenClaw Agent 必须先调 MCP 获取字段映射并 POST 到 Hub** `/requirements/tapd-cache/field-map`，Hub 后续展示 / LLM 提示词会按此映射翻译。

racinggo（workspace `70202650`）已知映射（参考）：

| TAPD 字段 | 中文名 |
|-----------|--------|
| `custom_field_three` | 测试执行 |
| `custom_field_six` | 测试结果 |
| `custom_field_eight` | **测试验收**（用例必参考） |
| `custom_field_18` | 是否需要测试 |
| `custom_field_19` | 评审进度 |

### 三、状态枚举对照（status）

racinggo workspace 常见状态（详见 `Documents/TAPD_API/05_story_status_map.json`）：

| key | 中文 |
|-----|------|
| `status_1` | 草稿 |
| `status_3` | **转测试** |
| `status_22` | **测试中** |
| `status_resolved` | 已解决 |
| `status_closed` | 已关闭 |
| `status_4` | 已完成 |

### 四、推送 payload 范式

#### 4.1 推送需求快照（`POST /requirements/iterations/<id>/snapshots`）

**全量 upsert**：每次推送应包含本 Hub TestIteration 关联的 **所有** TAPD Story；Hub 会按 `(iteration_id, tapd_story_id)` upsert，**未在本次推送中出现的旧需求不会被自动删除**（除非显式列在 `removed_story_ids`）。

```json
{
  "tapd_workspace_id": "70202650",
  "stories": [
    {
      "id": "1170202650001178001",
      "name": "登录优化：支持手机短信验证",
      "status": "status_22",
      "priority_label": "High",
      "owner": "rajqiu",
      "developer": "abc;def",
      "module": "登录系统",
      "feature": "用户中心",
      "version": "M1版本",
      "release_id": "1170202650...",
      "baseline_id": "1170202650...",
      "iteration_id": "1170202650...",
      "description": "<p>原描述 HTML...</p>",
      "custom_field_eight": "1. 短信发送 ≤3s\n2. 错误验证码 5 次锁定...",
      "custom_field_three": "重点验证错峰场景",
      "test_suggestions": [
        {"title": "验证码风控", "points": ["5 次错误锁定", "锁定后解锁策略"]},
        {"title": "短信通道", "points": ["发送时延 <= 3s", "失败重试与兜底"]}
      ],
      "progress": 60,
      "effort": 8,
      "effort_completed": 5,
      "created": "2026-04-15 10:00:00",
      "modified": "2026-04-20 14:23:00",
      "begin": "2026-04-15",
      "due": "2026-04-30",
      "impl_status": "in_progress",
      "impl_remark": "服务端已完成，客户端联调中"
    }
  ],
  "removed_story_ids": ["1170202650000999000"]
}
```

#### 4.2 推送 TAPD 下拉缓存（`POST /requirements/tapd-cache/iterations`）

```json
{
  "tapd_workspace_id": "70202650",
  "iterations": [
    {
      "id": "1170202650...",
      "name": "M1 迭代",
      "status": "open",
      "startdate": "2026-04-01",
      "enddate": "2026-04-30",
      "creator": "rajqiu",
      "description": "M1 阶段迭代",
      "parent_id": "0"
    }
  ]
}
```

#### 4.3 推送 TAPD 版本（`POST /requirements/tapd-cache/versions`）

```json
{
  "tapd_workspace_id": "70202650",
  "project_id": 0,
  "versions": [
    {
      "id": "1170202650...",
      "name": "M1版本",
      "status": "Unclosed",
      "version_type": "Normal version",
      "start": "2026-04-01",
      "due": "2026-04-30",
      "testtime": "2026-04-25",
      "releasetime": "2026-04-30",
      "creator": "rajqiu",
      "owner": "abc;def",
      "created": "2026-03-20 10:00:00",
      "modified": "2026-04-20 14:23:00"
    }
  ]
}
```

#### 4.4 推送 TAPD 基线（`POST /requirements/tapd-cache/baselines`）

```json
{
  "tapd_workspace_id": "70202650",
  "baselines": [
    {
      "id": "1170202650...",
      "version_id": "1170202650...",
      "name": "M1-第一次转测",
      "creator": "rajqiu",
      "created": "2026-04-25 18:00:00",
      "stories_snapshot": ["1170202650001178001", "1170202650001178002"]
    }
  ]
}
```

#### 4.5 推送字段映射（`POST /requirements/tapd-cache/field-map`）

```json
{
  "tapd_workspace_id": "70202650",
  "entity_type": "story",
  "field_map": {
    "custom_field_three": "测试执行",
    "custom_field_six": "测试结果",
    "custom_field_eight": "测试验收",
    "custom_field_18": "是否需要测试",
    "custom_field_19": "评审进度"
  }
}
```

### 五、校验规则（OpenClaw 提交前必须自检）

1. 所有 ID 字段（`tapd_story_id` / `tapd_iteration_id` / `iteration_id` / `tapd_workspace_id`）：
   - 类型必须是 **字符串或整数**，非 None / 非空白
   - **`0` 是合法值**（如 RacingGO 的 `project_id=0`），不要用 `if not x` 判空
2. 时间字段：
   - 接受 `YYYY-MM-DD HH:MM:SS` / `YYYY-MM-DDTHH:MM:SS` / `YYYY-MM-DD`
   - `0000-00-00` 视为空
3. 数值字段（`progress`/`effort`/`effort_completed`/`remain`）：
   - 缺失填 0，**不要传 null**
4. `removed_story_ids`：
   - 必须是 **本迭代里之前推送过、本次确认从迭代中移出**的 story；不在的会被忽略
5. 字段映射 `field_map`：
   - 必须是 `{custom_field_xxx: 中文}` 形式；空字典会清空 Hub 缓存

---

## 同步策略

### 策略 A：用户触发实时刷新（≤15s 端到端）

1. 用户在 Hub `/requirements` 页面点击 **"🔄 实时刷新"** 按钮
2. Hub 写入 `tapd_refresh_requests` 表（status=`pending`）
3. **Agent 每 10s 轮询** `GET /requirements/agent/tapd-refresh-queue?limit=10`
   - Hub 在 SELECT 时把 status 改为 `picked`，避免多 agent 并发处理
4. Agent 根据 request 的 scope/iteration_id：
   - `scope=iterations`：调 `mcporter-internal` 拉取 workspace 全量迭代 → POST 缓存
   - `scope=stories` + `iteration_id`：拉本 iteration 关联的 TAPD Story → POST snapshot
5. Agent 完成后调 `POST /requirements/agent/tapd-refresh-queue/<id>/done`（或 `/fail`）
6. Hub 前端轮询 `GET /requirements/tapd-refresh-requests/<id>` 看到 `done` 后刷新视图

### 策略 B：每日全量同步（baseline）

每日凌晨 02:00（agent 侧 cron 或 APScheduler）：

1. 列出 Hub 所有 `status in ('draft','active')` 的 TestIteration（GET `/test-iterations?status=active`）
2. 对每个 iteration：
   - 调 MCP 拉 TAPD Story 列表（按 iteration_id 过滤）
   - POST snapshot 到 Hub
3. 顺便刷新一次：迭代下拉缓存 / 版本缓存 / 基线缓存 / 字段映射

### 策略 C：增量同步（小时级，可选）

每小时拉一次"最近 1 小时修改过"的 Story，POST snapshot。Hub 端 diff 自动产生变更日志。

---

## MCP 工具调用示例

> 以下为 `mcporter-internal` 暴露的 TAPD MCP 工具调用约定，具体工具名以本地 `mcporter-internal --list` 为准。

```python
# 伪代码 / 概念示意
import mcporter_internal as mcp

# 1. 拉某 workspace 的迭代列表
iterations = mcp.call('tapd.iterations.list', workspace_id='70202650')

# 2. 拉某迭代的需求（带全部 custom_field）
stories = mcp.call('tapd.stories.list',
    workspace_id='70202650',
    iteration_id='1170202650...',
    fields='*')   # 关键：拿全字段，不要白名单

# 3. 拉字段映射
field_map = mcp.call('tapd.stories.fields_label', workspace_id='70202650')

# 4. 拉版本与基线
versions = mcp.call('tapd.versions.list', workspace_id='70202650')
baselines = mcp.call('tapd.baselines.list', workspace_id='70202650', version_id=...)
```

---

## 推送代码片段（Python，Agent 侧）

```python
import os, requests
HUB = os.environ['HUB_BASE_URL']    # http://your-hub-host:8088
TOKEN = os.environ['HUB_API_TOKEN']
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type': 'application/json',
}

def push_snapshot(iteration_id, workspace_id, stories, removed_ids=None):
    r = requests.post(
        f'{HUB}/api/v1/requirements/iterations/{iteration_id}/snapshots',
        headers=HEADERS, timeout=60,
        json={
            'tapd_workspace_id': str(workspace_id),
            'stories': stories,
            'removed_story_ids': removed_ids or [],
        },
    )
    r.raise_for_status()
    return r.json()
    # → {"upserted": 12, "new_count": 2, "diff_count": 5,
    #    "removed_count": 1, "auto_linked_engineering_changes": 3, ...}

def push_iterations_cache(workspace_id, iterations):
    r = requests.post(
        f'{HUB}/api/v1/requirements/tapd-cache/iterations',
        headers=HEADERS, timeout=30,
        json={'tapd_workspace_id': str(workspace_id),
              'iterations': iterations},
    )
    r.raise_for_status()
    return r.json()

def poll_refresh_queue():
    r = requests.get(f'{HUB}/api/v1/requirements/agent/tapd-refresh-queue?limit=10',
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()['requests']

def mark_done(req_id):
    requests.post(f'{HUB}/api/v1/requirements/agent/tapd-refresh-queue/{req_id}/done',
                  headers=HEADERS, timeout=10).raise_for_status()

def mark_fail(req_id, msg):
    requests.post(f'{HUB}/api/v1/requirements/agent/tapd-refresh-queue/{req_id}/fail',
                  headers=HEADERS, timeout=10,
                  json={'error_message': msg}).raise_for_status()
```

---

## 工作循环范式

```python
import time, traceback

POLL_INTERVAL = 10   # 秒，反向轮询周期

def main_loop():
    while True:
        try:
            requests_to_handle = poll_refresh_queue()
            for req in requests_to_handle:
                handle_one(req)
        except Exception as e:
            print(f'[refresh-queue] poll error: {e}')
            traceback.print_exc()
        time.sleep(POLL_INTERVAL)


def handle_one(req):
    rid = req['id']
    try:
        scope = req['scope']
        ws = req['tapd_workspace_id']
        if scope == 'iterations':
            iters = mcp.call('tapd.iterations.list', workspace_id=ws)
            push_iterations_cache(ws, iters)
        elif scope == 'stories' and req.get('iteration_id'):
            iter_id_local = req['iteration_id']
            tapd_iter_id = req.get('tapd_iteration_id')
            stories = mcp.call('tapd.stories.list',
                               workspace_id=ws,
                               iteration_id=tapd_iter_id,
                               fields='*')
            push_snapshot(iter_id_local, ws, stories)
        elif scope == 'all':
            # 完整刷新：iterations + versions + baselines + field-map
            sync_workspace_full(ws)
        mark_done(rid)
    except Exception as e:
        mark_fail(rid, str(e)[:5000])
```

---

## 需求分析图谱（Requirement Analysis Graph）

> **新增功能（2026-05-08）**：Agent 在完成需求快照推送后，应对所有需求进行**关联分析**，将功能域聚类和一致性问题推送到 Hub，供前端"需求图谱"按钮展示。

### 一、数据模型

#### 1.1 功能域聚类（`requirement_domain_clusters`）

按功能域（如"关卡系统"、"3C_手势_镜头"）对需求聚类，统计该域的需求数、平均质量分、TOP 问题。

| 字段 | 类型 | 说明 |
|------|------|------|
| `iteration_id` | int FK | 所属迭代 |
| `domain_name` | varchar(50) | 功能域名 |
| `requirement_count` | int | 该域需求数 |
| `avg_score` | decimal(5,1) | 该域平均质量分（0-10） |
| `top_issues` | text(JSON数组) | 该域 TOP 问题列表 |
| `created_at` | datetime | 创建时间 |

**幂等键**：`(iteration_id, domain_name)` — 重复推送会覆盖更新。

#### 1.2 一致性问题（`requirement_consistency_issues`）

记录跨需求的一致性问题（复制粘贴、覆盖遗漏、差异化缺失、模板化严重、关联提醒）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `iteration_id` | int FK | 所属迭代 |
| `issue_type` | varchar(30) | 问题类型 |
| `severity` | varchar(10) | 严重度：高/中/低 |
| `domain` | varchar(50) | 所属功能域 |
| `requirement_ids` | text(JSON数组) | 涉及的需求 ID 列表 |
| `detail` | text | 问题描述 |
| `status` | varchar(20) | open/closed（可标记已修复） |
| `resolved_at` | datetime | 修复时间 |
| `resolved_by` | varchar(100) | 修复人 |

---

### 二、API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/requirements/iterations/<id>/domain-clusters` | 获取功能域聚类列表 |
| `POST` | `/requirements/domain-clusters` | 推送单条功能域聚类（幂等 upsert） |
| `GET` | `/requirements/iterations/<id>/consistency-issues` | 获取一致性问题列表 |
| `POST` | `/requirements/consistency-issues` | 推送单条一致性问题 |
| `PUT` | `/requirements/consistency-issues/<id>` | 更新问题状态（标记已修复） |
| `DELETE` | `/requirements/consistency-issues/<id>` | 删除问题 |
| `DELETE` | `/requirements/domain-clusters/<id>` | 删除功能域聚类 |

> **认证**：所有接口需要 `Authorization: Bearer {HUB_API_TOKEN}` 头。

---

### 三、推送范式

#### 3.1 推送功能域聚类

```json
POST /api/v1/requirements/domain-clusters
{
  "iteration_id": 42,
  "domain_name": "关卡系统",
  "requirement_count": 8,
  "avg_score": 6.5,
  "top_issues": ["验收标准缺失", "描述歧义"]
}
```

**响应示例：**
```json
{
  "id": 1,
  "iteration_id": 42,
  "domain_name": "关卡系统",
  "requirement_count": 8,
  "avg_score": 6.5,
  "top_issues": "[\"验收标准缺失\", \"描述歧义\"]",
  "created_at": "2026-05-08 14:30:00"
}
```

#### 3.2 推送一致性问题

```json
POST /api/v1/requirements/consistency-issues
{
  "iteration_id": 42,
  "issue_type": "复制粘贴",
  "severity": "高",
  "domain": "关卡系统",
  "requirement_ids": [101, 102, 103],
  "detail": "需求 101/102/103 的验收标准完全复制粘贴，未体现差异化"
}
```

**响应示例：**
```json
{
  "id": 1,
  "iteration_id": 42,
  "issue_type": "复制粘贴",
  "severity": "高",
  "domain": "关卡系统",
  "requirement_ids": "[101, 102, 103]",
  "detail": "需求 101/102/103 的验收标准完全复制粘贴，未体现差异化",
  "status": "open",
  "resolved_at": null,
  "resolved_by": "",
  "created_at": "2026-05-08 14:35:00"
}
```

#### 3.3 标记问题已修复

```json
PUT /api/v1/requirements/consistency-issues/1
{
  "status": "closed"
}
```

---

### 四、Agent 分析流程

推荐在**每日全量同步完成后**触发需求图谱分析。

```
每日 02:00 全量同步完成后：
  1. 获取本迭代所有需求列表（GET /requirements/iterations/{id}/items）
  2. 调用 LLM 分析：
     a. 按功能域聚类（提取需求标题/描述中的功能域关键词）
     b. 评估每个需求的质量分（0-10，基于描述完整性、验收标准清晰度等）
     c. 检测跨需求的一致性问题：
        - 复制粘贴：验收标准/描述高度相似
        - 覆盖遗漏：关联工程变更但需求描述未更新
        - 差异化缺失：同类需求但验收标准完全一致
        - 模板化严重：描述过于抽象，缺乏具体细节
        - 关联提醒：需求 A 引用需求 B 但未明确依赖关系
  3. 删除本迭代旧的图谱数据（可选：前端按 created_at 区分版本）
  4. 推送功能域聚类（POST /requirements/domain-clusters）
  5. 推送一致性问题（POST /requirements/consistency-issues）
```

#### 4.1 LLM 提示词模板（参考）

```
你是一个需求质量分析专家。给定以下需求列表（JSON 数组），请完成：

1. 功能域聚类：将需求按功能域分组（如"关卡系统"、"3C_手势_镜头"、"UI_交互"等）
2. 质量评分：为每个需求打分（0-10），考虑：
   - 描述完整性（是否有背景、目标、验收标准）
   - 验收标准清晰度（是否可测试、无歧义）
   - 依赖关系是否明确
3. 一致性问题检测：找出跨需求的问题（复制粘贴、覆盖遗漏、差异化缺失、模板化严重、关联提醒）

输出 JSON：
{
  "domain_clusters": [
    {
      "domain_name": "关卡系统",
      "requirement_ids": [1, 2, 3],
      "avg_score": 6.5,
      "top_issues": ["验收标准缺失", "描述歧义"]
    }
  ],
  "consistency_issues": [
    {
      "issue_type": "复制粘贴",
      "severity": "高",
      "domain": "关卡系统",
      "requirement_ids": [1, 2],
      "detail": "需求 1 和 2 的验收标准完全复制粘贴"
    }
  ]
}

需求列表：
{requirements_json}
```

---

### 五、Agent 侧代码示例

```python
import os, requests, json as pyjson
HUB = os.environ['HUB_BASE_URL']
TOKEN = os.environ['HUB_API_TOKEN']
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type': 'application/json',
}

def push_domain_cluster(iteration_id, domain_name, req_ids, scores):
    """推送单条功能域聚类"""
    avg = sum(scores) / len(scores) if scores else 0
    r = requests.post(
        f'{HUB}/api/v1/requirements/domain-clusters',
        headers=HEADERS, timeout=30,
        json={
            'iteration_id': iteration_id,
            'domain_name': domain_name,
            'requirement_count': len(req_ids),
            'avg_score': round(avg, 1),
            'top_issues': pyjson.dumps(['问题1', '问题2']),  # JSON 字符串
        },
    )
    r.raise_for_status()
    return r.json()

def push_consistency_issue(iteration_id, issue_type, severity, domain, req_ids, detail):
    """推送单条一致性问题"""
    r = requests.post(
        f'{HUB}/api/v1/requirements/consistency-issues',
        headers=HEADERS, timeout=30,
        json={
            'iteration_id': iteration_id,
            'issue_type': issue_type,
            'severity': severity,
            'domain': domain,
            'requirement_ids': pyjson.dumps(req_ids),  # JSON 字符串
            'detail': detail,
        },
    )
    r.raise_for_status()
    return r.json()

def run_graph_analysis(iteration_id, workspace_id):
    """执行需求图谱分析（在每日全量同步后调用）"""
    # 1. 获取本迭代所有需求
    items_resp = requests.get(
        f'{HUB}/api/v1/requirements/iterations/{iteration_id}/items',
        headers=HEADERS, params={'page_size': 500}, timeout=30,
    ).json()
    items = items_resp.get('items', [])

    # 2. 调用 LLM 分析（伪代码）
    analysis_result = call_llm_for_graph_analysis(items)

    # 3. 推送功能域聚类
    for cluster in analysis_result.get('domain_clusters', []):
        req_ids = cluster['requirement_ids']
        scores = [next(i['completeness_score'] for i in items if i['id'] in req_ids)]  # 假设有 completeness_score
        push_domain_cluster(iteration_id, cluster['domain_name'], req_ids, scores)
        # 推送 TOP issues
        # ...

    # 4. 推送一致性问题
    for issue in analysis_result.get('consistency_issues', []):
        push_consistency_issue(
            iteration_id,
            issue['issue_type'],
            issue['severity'],
            issue['domain'],
            issue['requirement_ids'],
            issue['detail'],
        )

    return {'clusters': len(analysis_result.get('domain_clusters', [])),
            'issues': len(analysis_result.get('consistency_issues', []))}
```

---

### 六、注意事项

1. **`top_issues` 和 `requirement_ids` 字段存储格式是 JSON 字符串**（LONGTEXT），不是原生 JSON 类型。推送时先用 `json.dumps()` 序列化。
2. **功能域聚类是幂等的**：相同 `(iteration_id, domain_name)` 的 POST 会覆盖更新，无需先 DELETE 再 POST。
3. **一致性问题不支持幂等**：每次分析直接 INSERT，建议在分析前先清空本迭代的旧问题（可选，前端按 `created_at` 区分版本更简单）。
4. **LLM 调用成本**：需求图谱分析需要把所有需求传给 LLM，注意 token 限制。建议分批（每批 20-30 条需求）。
5. **触发时机**：推荐在每日全量同步（策略 B）完成后触发，或者作为独立的定时任务（每日 03:00）。

---

## 与其它模块的协作

### 与 engineering-analysis

- 推送工程刷新批次（POST `/engineering/refresh`）时若 `change_item.tapd_story_ids` 含 story_id，**Hub 自动**建立 `requirement_function_links`（函数/方法级，含文件信息）
- 反向：本 Skill 推送 snapshot 时，Hub 也会扫描存量 EngineeringChangeItem 自动补建函数级链接（优先读取 `symbol_names`，若含 AST 行号会写入 `start_line/end_line`）

**联调前置检查（非常重要）**

1. `engineering_change_items.tapd_story_ids` 不能是空数组 `[]`
   - 若全是 `[]`，Hub 无法把工程变更挂到需求，函数关联会是 0 条
2. `engineering_change_items.symbol_names` 需要包含函数/方法信息
   - 推荐对象数组：
     ```json
     [
       {"name":"CreateOrder","start_line":128,"end_line":196},
       {"name":"ValidateCoupon","start_line":210,"end_line":248}
     ]
     ```
3. `tapd_story_ids` 与需求快照中的 `stories[].id` 必须同源同值（字符串化后一致）

> 排查建议：先抽样 10 条 `engineering_change_items`，确认 `tapd_story_ids` 非空且 `symbol_names` 有效，再看 `/requirements/items/{id}/engineering-changes` 返回。

### 与 testcase-manager

- 用户/AI 在 Hub `/requirements/items/<id>` 详情页点 "AI 用例设计" → 调 `ai_generator.by_requirement`，输入 = 当前 RequirementItem.acceptance_criteria + test_focus + 关联的 EngineeringChangeItem 列表
- 生成的用例可手动 POST `/requirements/items/<id>/testcase-links` 建立绑定

### 与 testplan-manager

- TestIteration 创建/编辑表单的 "TAPD 迭代" 多选下拉直接读 `/test-plans/tapd-iterations?project_id=` → 走 `tapd_iterations_cache` 本地查询，0 延迟

---

## 数据管理 API

### 删除整个迭代数据（`DELETE /requirements/iterations/<id>/data`）

删除某个迭代下的所有需求分析数据（需求条目 + 变更日志 + 用例关联 + 工程关联 + 函数级关联），允许用户清空后重新上传快照。

**响应示例：**
```json
{
  "iteration_id": 42,
  "deleted_items": 79,
  "deleted_logs": 156,
  "deleted_links": 12,
  "deleted_eng_links": 8,
  "deleted_function_links": 32
}
```

### 删除单条需求记录（`DELETE /requirements/items/<id>`）

删除单条需求记录及其关联的变更日志、用例关联、工程关联。删除后可通过重新推送快照恢复。

**响应示例：**
```json
{
  "id": 123,
  "deleted_logs": 5,
  "deleted_links": 2,
  "deleted_eng_links": 1,
  "deleted_function_links": 6
}
```

> **注意**：删除操作不可撤销。删除后如需恢复，需要 Agent 重新推送 snapshot。

### 更新需求实现状态（`PATCH /requirements/items/<id>/impl-status`）

Agent 基于工程代码分析结果，判断某条需求的功能是否已实现，更新实现状态和备注。

**请求示例：**
```json
{
  "impl_status": "in_progress",
  "impl_remark": "服务端已完成，客户端还没完成"
}
```

**`impl_status` 允许值：**
| 值 | 含义 |
|---|---|
| `not_impl` | 未实现 |
| `in_progress` | 实现中（部分完成） |
| `implemented` | 已实现 |
| `unknown` | 未评估（默认） |

**`impl_remark`**：备注说明，最大 500 字符，前端鼠标悬浮时 tooltip 显示。

**响应**：返回更新后的完整需求 `to_dict()` JSON。

### 批量更新实现状态（`PATCH /requirements/iterations/<id>/impl-status-batch`）

Agent 一次性更新某迭代下多条需求的实现状态。

**请求示例：**
```json
{
  "items": [
    {"tapd_story_id": "1012345", "impl_status": "implemented", "impl_remark": "全部完成"},
    {"tapd_story_id": "1012346", "impl_status": "in_progress", "impl_remark": "服务端完成，客户端未开始"},
    {"tapd_story_id": "1012347", "impl_status": "not_impl", "impl_remark": ""}
  ]
}
```

**响应示例：**
```json
{
  "updated_count": 3,
  "updated_stories": ["1012345", "1012346", "1012347"],
  "errors": []
}
```

> **使用场景**：Agent 完成工程代码分析后，对比需求列表和代码实现，批量标记每条需求的实现进度。结合 `engineering-analysis` skill 的代码分析结果使用效果最佳。

---

## 实现状态分析流程

Agent 基于工程代码分析结果来判断需求的实现进度，推荐流程：

### 触发时机

1. **工程分析完成后**：`engineering-analysis` skill 完成代码变更分析后，自动触发实现状态评估
2. **每日全量同步后**：策略 B 全量同步完成后，对比代码仓库状态更新实现进度
3. **手动触发**：用户在 Hub 页面请求评估某迭代的实现状态

### 判断逻辑

```
对于每条需求 story：
1. 获取该需求关联的工程变更（函数/方法级，含文件与符号）
2. 分析关联代码变更的覆盖情况：
   - 服务端代码是否有对应实现？
   - 客户端代码是否有对应实现？
   - 配置/数据表是否已变更？
3. 综合判断：
   - 所有相关模块均有代码变更且已合入 → implemented
   - 部分模块有变更但不完整 → in_progress + 备注说明缺少哪部分
   - 无关联代码变更 → not_impl
   - 无法确定（如无法匹配到代码）→ unknown
```

### 代码示例

```python
def evaluate_impl_status(iteration_id, workspace_id):
    """评估某迭代下所有需求的实现状态"""
    # 1. 获取迭代下的需求列表
    items = requests.get(
        f'{HUB}/api/v1/requirements/iterations/{iteration_id}/items?page_size=200',
        headers=HEADERS
    ).json()['items']

    # 2. 对每条需求分析工程关联
    batch_updates = []
    for item in items:
        eng_resp = requests.get(
            f'{HUB}/api/v1/requirements/items/{item["id"]}/engineering-changes',
            headers=HEADERS
        ).json()
        changes = eng_resp.get('changes', [])

        status, remark = analyze_changes(item, changes)
        batch_updates.append({
            'tapd_story_id': item['tapd_story_id'],
            'impl_status': status,
            'impl_remark': remark,
        })

    # 3. 批量更新
    requests.patch(
        f'{HUB}/api/v1/requirements/iterations/{iteration_id}/impl-status-batch',
        headers=HEADERS,
        json={'items': batch_updates}
    ).raise_for_status()


def analyze_changes(item, eng_changes):
    """根据工程变更分析单条需求的实现状态"""
    if not eng_changes:
        return 'not_impl', '无关联工程变更'

    # 按模块归类变更
    server_changes = [c for c in eng_changes if is_server_code(c)]
    client_changes = [c for c in eng_changes if is_client_code(c)]

    parts = []
    if server_changes:
        parts.append('服务端已完成')
    else:
        parts.append('服务端未实现')
    if client_changes:
        parts.append('客户端已完成')
    else:
        parts.append('客户端未实现')

    remark = '，'.join(parts)

    if server_changes and client_changes:
        return 'implemented', remark
    elif server_changes or client_changes:
        return 'in_progress', remark
    else:
        return 'not_impl', remark
```

---

## 注意事项 / 经验

- **Taihu Token 绝不存代码 / 配置 / Git**：依赖 Knot 平台运行时注入；本地调试如需手动指定，请用环境变量并 `.gitignore`
- **`raw_payload` 兜底**：如果 TAPD 后续加字段，先不用改 Hub 表，全在 `raw_payload` 里看到，再决定要不要 ALTER
- **diff 由 Hub 算**：Agent 只管"推全量当前快照"，不要在 Agent 侧算变化；Hub 的 `_diff_and_log` 已经 dedup（同一天同字段同变化只记一次）
- **`removed_story_ids` 谨慎传**：只在你能确定"这条 story 已经从该 iteration 移走"时传；宁可漏传也不要错传
- **大需求迭代分页**：单次 POST snapshot 建议 ≤ 200 条；超过请分多次同 iteration 推送，Hub 接受多次推送（每次都是 upsert）
- **轮询间隔**：10s 是 R1 默认值；如对实时性要求高，可调到 5s，但同时部署多个 agent 时注意 SELECT...UPDATE 的并发（Hub 当前 MVP 单事务足够）
- **失败要 mark_fail**：千万别漏；否则 request 永远停在 `picked` 状态，前端会一直转

---

## 经验沉淀（Rule #15 + knowledge-manager 联动）

完成一次需求分析（拉取 + 解析 + 关联用例/工程）后，按 Rule #15 §3 决策树判断是否要沉淀经验：

| 你产出的内容 | 落层 | 动作 |
|---|---|---|
| 当前迭代的解析草稿、临时归类逻辑 | 本地 | 不上报 |
| 单次需求里发现的"易漏验收点 / 描述歧义模式"（首次） | Memos | `POST /memos/upsert tag=pitfall scope_key={project}` |
| 跨多个需求重复出现的需求模板/验收套路（≥ 2 次） | MySQL | `POST /knowledge category=method scope=project` |
| 已稳定的"需求拆解 SOP / 验收标准模板" | MySQL | `POST /knowledge category=workflow` |
| 某个项目的需求字段映射规范（结构化、需要长期检索） | MySQL | `POST /knowledge category=workflow scope=project` |

> 详细分层判断与 API 参数模板见 `knowledge-manager` skill §A/B/C；
> 升级触发条件自检表见 `knowledge-manager` skill §C；
> Rule #15 §7 七大禁止事项（不要把临时草稿直接写 MySQL，也不要把已稳定 SOP 长期只放 Memos）。
