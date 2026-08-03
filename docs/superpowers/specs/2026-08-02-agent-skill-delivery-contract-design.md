# Agent Skill 下发端点契约设计

## 1. 目标

为 Windows/Linux 定制 Agent 的 Skill Loader 提供一套稳定、可鉴权、可缓存、可校验的 Hub 下发契约。

本次只改 Hub。定制 Agent 的 Loader 实现不在本次范围内。

## 2. 现状

Hub 已有以下能力：

- `GET /api/v1/openclaws/{claw_id}/assigned-skills`：返回已分配 Skill。
- `GET /api/v1/skills/{skill_id}/raw`：返回主 Markdown。
- `GET /api/v1/skills/{skill_id}/files`：返回文档包文件。
- `GET /api/v1/skills/{skill_id}/files/{filename}`：返回单文件。
- `GET /api/v1/skills/{skill_id}/pack`：返回 ZIP。
- sidecar config 下发 active Profile，其中包含 `required_skills`。
- task context 返回任务级 `required_skills`。

现有接口没有形成 Loader 契约：缺少统一授权、动态任务解析、内容版本、SHA256、轻量文件清单和 Agent 专用下载地址。

## 3. 授权原则

Manifest 合并以下来源：

1. 当前 Agent 已显式分配且启用的 Skill。
2. 当前 Agent active Profile 的 `required_skills`。
3. 指定任务上下文重新计算出的 `required_skills`。

Profile 和 task context 中的 Skill 自动获得当前 Agent 的本次下载权限，不要求先写入 `openclaw_skills`。

自动授权仍必须满足：

- Skill 存在。
- `review_status=approved`。
- `is_deleted=false`。
- Skill 项目范围允许当前 Agent/任务项目使用。
- 私有 Skill 只能由归属 Agent 使用。

任务上下文必须属于当前 Agent。若 `ref_type/ref_id` 指向其他 Agent 的任务，返回 403。

## 4. Manifest API

```http
GET /api/v1/openclaws/{claw_id}/skill-manifest
Authorization: Bearer <CLAW_TOKEN>
```

可选查询参数：

```text
ref_type=todo|agent_task|workflow_step
ref_id=<integer>
```

`ref_type` 与 `ref_id` 必须同时提供。

响应：

```json
{
  "manifest_version": 1,
  "claw_id": 27,
  "generated_at": "2026-08-02T16:30:00",
  "task_ref": {
    "ref_type": "workflow_step",
    "ref_id": 501
  },
  "skills": [
    {
      "id": 143,
      "name": "workflow-manager",
      "display_name": "Workflow 管理",
      "content_version": "2026-08-02T10:00:00",
      "sha256": "<bundle-sha256>",
      "sources": ["assigned", "profile", "task_context"],
      "files": [
        {
          "path": "SKILL.md",
          "size": 12345,
          "sha256": "<file-sha256>",
          "updated_at": "2026-08-02T10:00:00",
          "download_url": "/api/v1/openclaws/27/skills/143/files/SKILL.md"
        }
      ],
      "pack_url": "/api/v1/openclaws/27/skills/143/pack"
    }
  ],
  "missing_skills": [
    {
      "name": "requirement-analysis",
      "sources": ["profile"],
      "reason": "not_found"
    }
  ]
}
```

`missing_skills.reason` 合法值：

- `not_found`
- `not_approved`
- `deleted`
- `project_forbidden`
- `private_forbidden`

## 5. Agent 专用下载 API

单文件：

```http
GET /api/v1/openclaws/{claw_id}/skills/{skill_id}/files/{filename}
Authorization: Bearer <CLAW_TOKEN>
```

ZIP：

```http
GET /api/v1/openclaws/{claw_id}/skills/{skill_id}/pack
Authorization: Bearer <CLAW_TOKEN>
```

任务级自动授权的下载 URL 携带原始 `ref_type/ref_id` 查询参数，服务端重新计算权限，不能只相信 Manifest。

下载响应包含：

```text
ETag: "<sha256>"
X-Skill-Content-Version: <version>
Cache-Control: private, max-age=60
```

客户端可使用 `If-None-Match`；内容未变化时返回 304。

旧 `/skills/{id}/raw|files|pack` 接口本次保留，避免破坏现有 Agent。新 Loader 只能依赖 Agent 专用端点。

## 6. 文档包规范化

服务端以 MySQL 中的 `SkillFile` 为文档包来源。

兼容旧 Skill：

- 如果不存在 `SkillFile("SKILL.md")`，但 `Skill.template_content` 非空，则动态合成 `SKILL.md`。
- 文件路径使用 `/`，按路径排序。
- Bundle SHA256 对每个文件的路径、长度和内容按固定格式串联后计算，确保顺序稳定且无拼接歧义。
- Manifest 不返回文件正文。

## 7. 组件边界

新增 `web/app/services/skill_delivery.py`：

- 解析已分配、Profile 和 task context 三种来源。
- 校验任务归属。
- 校验 Skill 可用性与可见性。
- 规范化文档包。
- 计算文件和 Bundle SHA256。
- 生成 Manifest。

API 层只负责：

- 参数解析。
- Claw Token 鉴权。
- 将服务结果映射为 JSON、文件或 ZIP 响应。

## 8. 错误处理

- 缺少一半任务参数：400。
- 非法 `ref_type`：400。
- 任务不存在：404。
- 任务属于其他 Agent：403。
- Skill 不在当前 Manifest 授权集合：403。
- 文件不存在：404。
- Agent Token 与 URL claw_id 不一致：由统一 `require_claw_token` 返回 403。

单个 required Skill 不可用时 Manifest 仍返回 200，并写入 `missing_skills`，让 Loader 统一返回业务 `blocked`。

## 9. 测试

按 TDD 实现：

1. 已分配 Skill 出现在 Manifest。
2. active Profile required Skill 未分配也能自动授权。
3. task context required Skill 未分配也能自动授权。
4. 其他 Agent 的任务返回 403。
5. 未审核、已删除、项目不匹配和私有 Skill进入 `missing_skills`。
6. 没有 SkillFile 时合成 `SKILL.md`。
7. 文件顺序不影响 Bundle SHA256。
8. 内容变化会改变 SHA256/content_version。
9. Agent 专用单文件和 ZIP 下载通过。
10. 未授权 Skill 下载返回 403。
11. `If-None-Match` 命中返回 304。
12. 现有 Skill/Workflow/sidecar 相关测试保持通过。

## 10. 文档更新

实现完成后更新：

- `docs/自研定制Agent接入Hub改造说明.md`
- `docs/经验记录.md`

文档必须明确：

- Agent Loader 使用新 Manifest 与 Agent 专用下载端点。
- 旧公开接口仅用于兼容，不是新 Loader 契约。
- Profile/task_context 自动授权边界。
- 缓存、SHA256、缺失 Skill 和 blocked 处理方式。
