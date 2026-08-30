# Hub × Codex 多 Flow P0-J 自动化闭环看板开发说明

## 1. 本阶段目标

在不改变现有 Workflow 创建、执行、回调和状态推进逻辑的前提下，提供项目级“自动化闭环”只读管理页，把 P0-A～P0-I 已落地的数据统一聚合展示。

页面入口：`/automation-closed-loop`

聚合接口：`GET /api/v1/automation-closed-loop/overview`

## 2. 展示范围

- 候选用例各状态数量、平均/最长滞留时长、超过阈值的滞留数量。
- 等待 Capability、待资格验证、待发布及发布异常队列。
- Capability Gap 状态、等待候选数、待回队数和关联开发需求。
- 当前资源租约、过期但待清理记录及可追踪冲突事件。
- 默认用例库 #33 的当前 revision、用例数、最近 promotion/rollback。
- 默认 Flow12 最近 Run 的执行结果、冻结 revision 和冻结用例数。
- Evidence Manifest 完整率、跑后分类及最近明细。
- Finding 状态/严重度、人工反馈标签、分析规则与 replay gate 结果。
- 候选卡片可调用已有 `entity-relations/trace` API 展示最多 5 层的完整对象链路。

页面保留项目、用例库 ID 和 Workflow Definition ID 三个筛选项。#33 和 Flow12 是默认值，不是硬编码唯一值。

## 3. API

### 3.1 请求

```http
GET /api/v1/automation-closed-loop/overview
    ?project_id=1
    &library_id=33
    &workflow_definition_id=12
    &stale_hours=24
    &limit=12
```

参数：

| 参数 | 必填 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `project_id` | 是 | - | 正整数，且调用方必须有项目权限 |
| `library_id` | 否 | `33` | 正整数 |
| `workflow_definition_id` | 否 | `12` | 正整数 |
| `stale_hours` | 否 | `24` | `1`～`2160` |
| `limit` | 否 | `12` | `1`～`50` |

响应设置 `Cache-Control: no-store`，防止浏览器或代理缓存旧态势。

### 3.2 响应顶层结构

```json
{
  "generated_at": "2026-08-12 18:00:00",
  "project": {},
  "filters": {},
  "candidates": {},
  "capability_gaps": {},
  "resource_leases": {},
  "testcase_library": {},
  "workflow_runs": {},
  "evidence": {},
  "findings": {},
  "analysis_rules": {}
}
```

这是只读聚合接口，不创建对象、不推进状态、不清理租约，也不触发 Flow。

## 4. 统计口径

### 4.1 候选滞留

- `ACTIVE`、`RETIRED` 视为终态，不计入滞留。
- 其余状态以 `updated_at`（无值时回退 `created_at`）计算滞留小时数。
- 默认超过 24 小时计为滞留，页面和 API 可调整阈值。

### 4.2 发布失败队列

现有 promotion 失败响应没有单独持久化失败对象；P0 首版将持久状态 `QUARANTINED` 作为发布/生产异常队列，并同时返回 `publish_failures` 和兼容别名 `publish_exceptions`。接口中的 `queue_semantics` 明确记录该口径，避免误读为完整失败尝试历史。

### 4.3 资源租约归属

`resource_leases` 当前没有独立 `project_id`，聚合时按以下信息判定项目归属：

1. `metadata.project_id`；
2. `controller_run_id` 对应项目内 Workflow Run；
3. `owner_type=workflow_run/run` 且 `owner_id` 对应项目内 Run。

无法确认归属的租约不会进入项目看板，避免跨项目泄漏。

旧版 acquire 冲突只存在于 409 响应中，没有持久化。因此看板只统计生产方已写入的 `event_type=conflict` 事件，并通过 `conflict_tracking` 返回该限制；当前占用租约仍可正常展示。

### 4.4 证据完整率

`complete_count / manifest total`。无 Manifest 时返回 `null`，不伪造为 0% 或 100%。

## 5. 权限与兼容性

- 沿用 Hub 现有 Web session / OpenClaw Bearer Token 认证。
- 沿用项目权限判断；无权访问时统一返回 404，避免暴露项目存在性。
- 指定用例库带有 `project_name` 时必须与当前项目同名；不匹配按未找到处理，避免跨项目展示 revision 和用例数。空 `project_name` 的历史共享库保持兼容。
- 本阶段没有数据库迁移。
- 没有修改 Workflow Run、候选、用例库、Finding、规则或租约的写接口。
- 页面通过已有对象接口做跳转和链路查询，没有引入第二套状态机。

## 6. 代码位置

- 聚合服务：`web/app/services/automation_closed_loop.py`
- 聚合 API：`web/app/api/automation_closed_loop.py`
- 页面路由：`web/app/views/__init__.py`
- 页面与交互：`web/templates/automation_closed_loop.html`
- 侧边栏入口：`web/templates/base.html`
- 专项测试：`tests/test_automation_closed_loop_api.py`

页面视觉沿用 Hub 深浅主题变量，采用紧凑的工业控制台信息层级；没有引入第三方图表库或新的前端构建链。

## 7. 测试

专项测试覆盖：

- #33 / Flow12 默认筛选。
- 候选、Capability Gap、租约、revision/snapshot、Evidence、Finding/反馈、规则回放的聚合。
- 资源租约和候选的项目隔离。
- 普通用户项目权限与越权 404。
- 参数上限校验、空数据稳定结构、`no-store`。
- 页面区块、路由、导航和链路交互契约。

```powershell
python -m pytest tests/test_automation_closed_loop_api.py -q
python -m pytest tests -q
```

## 8. 部署与下一阶段

代码部署前仍需先完成 P0-A～P0-I 所需数据库迁移。本阶段自身无迁移，页面上线不会改变原 Flow 执行结果。

P0-J 完成后建议进入“P0 端到端联调与灰度验收”：使用真实 #33、Flow12 和一个隔离测试项目跑通 AT-01～AT-12，核对看板统计与源对象一致，再按开关和迁移顺序灰度部署整条闭环。
