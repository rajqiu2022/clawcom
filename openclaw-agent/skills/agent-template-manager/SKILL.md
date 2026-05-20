# Agent 模板库管理 (agent-template-manager)

## 简介

本 Skill 用于管理 Hub 的 Agent 身份模板库，覆盖模板创建、编辑、版本回滚、文件引用校验和模板应用。

适用场景：

- 维护某类 Agent 的标准身份模板（角色/职责/技能/规则/定时任务）
- 通过版本历史做追踪和回滚
- 复用模板快速初始化新的 Agent
- 校验模板说明中的文件引用是否有效

## 前置条件

- 仅超级管理员可用（Hub 权限控制）
- 已配置 Hub 地址和 Token

```text
Hub: http://clawteam.woa.com:18800
API 前缀: /api/v1
Authorization: Bearer {HUB_API_TOKEN}
```

## 核心数据结构

```json
{
  "id": 1,
  "template_key": "qa-owner-v1",
  "name": "测试负责人模板",
  "profile_name": "大赫",
  "role_name": "测试负责人",
  "main_responsibility": "负责测试策略，详见 {{file:docs/owner.md}}",
  "skills_summary": "核心技能组合说明",
  "rules_summary": "规则组合说明",
  "schedules_summary": "例行任务说明",
  "installed_skill_ids": [113, 136],
  "rule_ids": [12, 15],
  "schedule_items": [
    {
      "title": "每日测试日报",
      "schedule_type": "daily",
      "schedule_time": "21:30",
      "urgency_level": "flexible",
      "priority": "P1"
    }
  ],
  "status": "approved",
  "current_version": 7
}
```

## API 能力

### 1) 模板 CRUD

```text
GET    /api/v1/agent-templates?status=
POST   /api/v1/agent-templates
GET    /api/v1/agent-templates/{template_id}
PUT    /api/v1/agent-templates/{template_id}
DELETE /api/v1/agent-templates/{template_id}
```

### 2) 审核状态流转

```text
POST /api/v1/agent-templates/{template_id}/review
{
  "status": "pending_review|approved|rejected|archived|draft",
  "review_comment": "可选"
}
```

### 3) Agent 侧提交模板

```text
POST /api/v1/agent-templates/submit
```

说明：支持 Agent Bearer Token 提交，提交后状态为 `pending_review`。

### 4) 模板应用（真实落地）

```text
POST /api/v1/agent-templates/{template_id}/apply
{
  "target_claw_id": 14,
  "strict_references": true
}
```

应用动作：

- 按模板安装/启用 Skill（写入 `openclaw_skills`）
- 按模板安装/启用 Rule（写入 `openclaw_rules`）
- 同步模板定时任务（写入/更新/禁用 `claw_todos`）
- 生成同步通知和待办，触发 SSE 推送

### 5) 版本历史与回滚

```text
GET  /api/v1/agent-templates/{template_id}/versions
POST /api/v1/agent-templates/{template_id}/rollback
{
  "version_no": 5
}
```

回滚会生成一个新版本，保留完整审计链路。

### 6) 文件管理与预览

```text
GET    /api/v1/agent-templates/{template_id}/files
POST   /api/v1/agent-templates/{template_id}/files      (multipart/form-data: file, dir)
DELETE /api/v1/agent-templates/{template_id}/files/{file_id}
GET    /api/v1/agent-templates/{template_id}/files/{file_id}/download
GET    /api/v1/agent-templates/{template_id}/files/{file_id}/preview
```

### 7) 引用校验

```text
GET /api/v1/agent-templates/{template_id}/references/validate
```

引用语法：

```text
{{file:docs/owner.md}}
```

## 推荐流程

1. 新建模板，填写基础信息
2. 上传相关文件
3. 在职责/规则说明中引用文件
4. 执行引用校验，修复缺失引用
5. 保存并提交审核
6. 审核通过后 apply 到目标 Agent
7. 若异常，按版本回滚

## 常见错误

- `400 模板引用文件校验未通过`：存在 `{{file:...}}` 指向不存在文件
- `404 目标 OpenClaw 不存在`：`target_claw_id` 错误
- `403 需要超级管理员权限`：当前账号角色不足

