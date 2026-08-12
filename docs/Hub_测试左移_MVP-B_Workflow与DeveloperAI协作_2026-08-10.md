# Hub 测试左移 MVP-B：Workflow 与 Developer AI 协作

## 本阶段范围

本阶段在 MVP-A 的结构化代码分析与 Finding 状态机上，补齐三个入口：

1. Workflow 节点可配置“代码分析协议”，并把分析参数、报告绑定方式和 Shift-left API 契约下发给 Agent。
2. 测试报告可一键生成 Developer AI 临时协作链接。
3. 用例评审复用同一套临时凭据协议，Developer AI 可读取评审范围、评论和打标。

所有能力继续受 `SHIFT_LEFT_ENABLED` 控制。开关关闭时：

- 报告与用例评审页面不显示 Developer AI 邀请入口；
- Workflow 不回填内置分析配置，也不会把 `analysis` 契约下发给 Agent；
- Shift-left API 保持不可用；
- 现有 Workflow、测试报告和用例评审接口不改变。

## Workflow 代码分析节点

节点新增可选 `analysis` 元数据，未升级的 Runner 可直接忽略，不影响原节点执行协议。

```json
{
  "enabled": true,
  "profile": "requirement_code_joint",
  "create_report": true,
  "report_title_template": "代码分析报告 - {run_name}",
  "report_format": "html",
  "iteration_id_var": "iteration_id",
  "report_id_var": "analysis_report_id",
  "baseline_vars": {
    "client_target_sha": "client_target_sha",
    "server_target_sha": "server_target_sha",
    "requirement_revision": "requirement_revision"
  },
  "require_independent_review": true
}
```

Agent 任务载荷会附带以下契约：

- `POST /api/v1/shift-left/analysis-runs`
- `PUT /api/v1/shift-left/findings:upsert`
- `POST /api/v1/shift-left/analysis-runs/{analysis_run_id}/result`
- `POST /api/v1/test-reports`

内置“需求评审到用例评审闭环”的 `engineering_analysis` 节点会在开关开启后做兼容回填：只补不存在的 `analysis` 元数据，不覆盖人工已配置内容。

## 临时协作链接

报告页和用例评审页调用：

```http
POST /api/v1/collaboration-sessions
Idempotency-Key: <unique-key>
Content-Type: application/json
```

服务端返回一次性 `invitation_code`。页面生成的链接形如：

```text
/developer-ai/collaborate#invite=<one-time-code>&exchange=<exchange-api>
```

邀请码放在 URL fragment 中，不会随页面请求进入服务端访问日志。交接页不会自动兑换，必须由目标 Developer AI 显式点击或调用兑换 API，避免聊天软件链接预览提前消耗邀请码。

兑换后使用：

```http
Authorization: Bearer hub_cs_xxx
```

Token 仍受对象范围、scope、有效期、调用预算、撤销和审计约束。

## 用例评审 Developer AI API

`case_review` 的 `subject_id` 是现有 `Topic.id`，不新增评审数据模型。

- `GET /api/v1/shift-left/case-reviews/{topic_id}/context`
- `GET /api/v1/shift-left/case-reviews/{topic_id}/cases?page=1&page_size=100`
- `POST /api/v1/shift-left/case-reviews/{topic_id}/comments`
- `GET /api/v1/shift-left/case-reviews/{topic_id}/marks`
- `PUT /api/v1/shift-left/case-reviews/{topic_id}/marks`

默认 scope：

- `case_review:read`
- `case_review:comment`
- `case_review:mark`

默认不授予 `case_review:decision`，因此 Developer AI 不能直接通过或驳回评审。只有创建会话时显式授予该 scope 才能提交 `approve/reject`，同时同步现有用例库评审状态。

评审用例读取严格受 `review_case_ids` 或 `review_module_paths` 限制；节点标记不能越过评审范围，也不能覆盖或清除其他评审人的标记。

## 发布顺序

1. 先部署 MVP-A 数据库迁移 `ops/migrations/20260810_shift_left_mvp_a.sql`（若尚未执行）。MVP-B 不增加表结构。
2. 部署 Web 代码，保持 `SHIFT_LEFT_ENABLED=0`，验证现有 Workflow、报告和用例评审。
3. 在灰度环境设置 `SHIFT_LEFT_ENABLED=1` 并重启 Web。
4. 验证 Workflow 节点配置、报告邀请、用例评审邀请、兑换、撤销和审计。
5. 灰度稳定后再逐步开放项目；回滚时先关闭开关，不需要回滚数据库。

## 验证结果

- Shift-left、Workflow、报告、用例评审相关自动化回归通过。
- 4 个模板通过 Jinja 解析和 JavaScript 语法检查。
- 浏览器验证交接页在加载后保持“邀请待兑换”，不会自动发起兑换；桌面布局无横向溢出。
