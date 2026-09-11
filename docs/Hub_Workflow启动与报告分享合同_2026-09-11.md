# Hub Workflow 启动与报告分享合同

更新时间：2026-09-11

## 1. Run 启动参数

`POST /api/v1/workflow-runs` 会在创建任何 `WorkflowRun` 记录前校验
Definition 的 `start_vars_schema`。Hub 同时兼容历史字段映射格式和标准
JSON Schema 对象格式，并将 schema 中的默认值冻结到 Run 的
`context.start_vars`。

缺少必填字段时返回 HTTP 422：

```json
{
  "code": "WORKFLOW_START_BINDING_REQUIRED",
  "details": {
    "workflow_definition_id": 12,
    "missing_start_vars": ["hub_url"]
  }
}
```

此响应不会创建 Run。Worker、企微启动器和定时启动器应修正参数后创建新
Run，不应轮询或重启一个不存在的 Run。

## 2. 指定 Flow 查询最新 Run

使用 Definition 返回的 `latest_run_url`，或直接调用：

```text
GET /api/v1/workflow-definitions/{definition_id}/runs/latest
```

可选 `status=running,blocked`。该接口只会在指定 Definition 内选择
`created_at/id` 最新的 Run，不会回退到其他 Flow。响应包含：

```json
{
  "id": 589,
  "workflow_definition_id": 12,
  "selection": {
    "mode": "latest_for_definition",
    "workflow_definition_id": 12
  }
}
```

用户明确指定 `run_id` 时，Worker 应继续读取该历史 Run，不应用本接口替换。

## 3. 发布后创建或复用分享链接

报告已通过 `workflow_report` Artifact 绑定当前 Run 后，调用：

```text
POST /api/v1/test-reports/{report_id}/share?workflow_run_id={run_id}
Authorization: Bearer {claw_token}
Content-Type: application/json

{}
```

报告作者、管理员，或该 Run 冻结绑定的 Worker/执行者/复核者可以调用。
Workflow 参与者必须显式提供 `workflow_run_id`，且报告、Run、项目及调用方
绑定均须匹配。重复调用会复用同一 token 和 `shared_at`；Hub 以行锁串行化
并发请求。只有报告作者或管理员可使用 `?refresh=1` 旋转 token。

成功响应合同：

```json
{
  "schema": "hub.test_report_share@1",
  "report_id": 608,
  "workflow_run_id": 589,
  "is_shared": true,
  "reused": false,
  "share_token": "...",
  "share_url": "https://clawteam.woa.com/r/...",
  "public_readback_url": "/api/v1/test-reports/shared/...",
  "share_readback_url": "/api/v1/test-reports/608/share",
  "attachment_policy": "authenticated_only",
  "shared_at": "2026-09-11 09:00:00"
}
```

不得记录或转发 `share_token` 以外的 Hub/Worker 认证凭据。

## 4. 分享状态回读

```text
GET /api/v1/test-reports/{report_id}/share?workflow_run_id={run_id}
```

权限与创建接口一致。Worker 必须回读并确认 `is_shared=true`、`report_id`、
`workflow_run_id` 和 `share_url` 后，才可把链接写入持久 outbox。

分享或通知失败只重试对应副作用，不重跑游戏、不重新创建报告。附件策略固定为
`authenticated_only`：匿名分享页可读报告正文，附件仍要求登录。
