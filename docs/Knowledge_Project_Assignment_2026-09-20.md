# 普通知识库项目关联修复

## 原因与范围

KnowledgeEntry 已有 project_id 外键及响应字段，但普通条目的创建、更新、批量导入只赋值 project_name，导致显式传入 project_id 时被静默忽略。无需新增数据库字段。

现支持以下入口中的 project_id：

- `POST /api/v1/knowledge`
- `PUT /api/v1/knowledge/{entry_id}`
- `POST /api/v1/knowledge/batch-import` 的每个 entries 项

## 调用约定

将指定普通知识关联到 RacingGO 时，使用已核实的项目 ID，例如：

```http
PUT /api/v1/knowledge/{entry_id}
Content-Type: application/json

{"project_id": 6}
```

使用已有 Hub 登录态或 Agent 自己的 Bearer 凭据。不在请求示例、知识正文或日志中粘贴 Token。

- project_id 必须为正整数，不接受字符串、布尔值、零、负数或浮点数。
- 项目必须存在；检查原项目和目标项目的访问权限。普通 Agent 可关联同项目，不需要授予超级管理员；跨项目需现有身份具有两侧权限。
- ID 有效时从 Project 表同步 project_name。若同时提交名称，必须与该 ID 的正式名称一致，否则返回 `KNOWLEDGE_PROJECT_MISMATCH`（400），整个请求不写入。
- 项目不存在返回 `KNOWLEDGE_PROJECT_NOT_FOUND`（404）；无项目权限返回 `KNOWLEDGE_PROJECT_FORBIDDEN`（403）；ID 类型不合法返回 `KNOWLEDGE_PROJECT_INVALID`（400）。
- 省略 project_id 保留原关联。未关联的旧数据仍兼容仅 project_name 的展示元数据，不从名称自动推断外键。
- 已关联条目不能单独把 project_name 改成另一项目名称；变更项目必须显式给出新的 project_id。
- 显式 `project_id: null` 解除关联并清空 project_name，同时检查原项目权限；不能同时传非空项目名称。
- 不更改原有 scope、公开分享状态、来源与其他内容权限。项目关联不等于将原本全局可读的普通文章变成私密文档。
- 批量导入先校验所有有效条目的项目字段，再创建记录；任一项目校验失败不部分导入、不发送审核通知。顺带修复批量入口 created_by 在 user 初始化之前被读取的问题。

## 纪要与验收边界

test_journal 纪要页面仍受独立版本合同保护，普通 PUT 返回 `VERSIONED_KNOWLEDGE_REQUIRES_REVISION`（409），本次不允许其脱离纪要本更改项目。

上线后应先对指定知识 ID 调用更新，再 GET 回读 project_id/project_name；必要时由管理员做同条数据库校验。不能仅凭 project_name 正确宣称关联已完成。本次未自动批量回填历史数据，也未假定待修复的知识条目 ID。

专项测试：`tests/test_knowledge_project_assignment.py`，覆盖更新数据库回读、Agent 认证、非法/冲突参数、两侧权限、管理员迁移/清空、字段省略、旧名称兼容、创建、批量原子校验与纪要保护。
