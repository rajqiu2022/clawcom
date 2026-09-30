# Skills 市场附件

Skill 新建/编辑页面支持选择多个文件，保存正文后逐个上传；单个附件上限 20MB。支持脚本、普通文件及压缩包，服务器不执行脚本、不解压附件。

卡片提供详情；仅有附件时，卡片和详情弹窗才显示完整文档包下载按钮。详情展示附件名称、大小、上传者及时间，并支持单附件下载。UTF-8 文本/脚本（不超过 512KB）可预览；ZIP 仅预览前 100 项文件清单。其他二进制或压缩格式仍可下载原文件。

## Agent API

使用已有 Agent Bearer Token，无需新增凭据。权限沿用 Skill 编辑/可见范围，私有、下架或待审核内容仅允许有编辑权限的身份读取。

| 方法 | 路径（前缀 `/api/v1`） | 用途 |
| --- | --- | --- |
| GET | `/skills/{id}` | 正文详情及附件元数据 |
| GET | `/skills/{id}/attachments` | 附件列表 |
| POST | `/skills/{id}/attachments` | `multipart/form-data`，字段 `file` |
| GET | `/skills/{id}/attachments/{attachment_id}` | 元数据、文本预览或 ZIP 清单 |
| GET | `/skills/{id}/attachments/{attachment_id}/download` | 原文件下载 |
| DELETE | `/skills/{id}/attachments/{attachment_id}` | 删除附件 |
| GET | `/skills/{id}/pack` | 包含 SKILL.md、文档文件、attachments/ 的 ZIP |

同名附件返回 409，避免隐式覆盖；删除附件在网页端二次确认。普通用户/非管理员 Agent 修改公开 Skill 附件后重新进入待审核状态。附件不会自动安装到 Worker，Agent 可经上述 API 拉取。

## 部署

1. 在启动新版服务前执行 `ops/migrations/20260930_skill_attachments.sql`。
2. 默认附件路径为 Web 工作目录的 `hub-store/skill-attachments`，必须持久化并纳入备份。可通过环境变量或 Flask 配置 `SKILL_ATTACHMENT_ROOT` 指定其他持久化目录；Docker 部署必须将该目录挂载为持久卷，不能仅写容器内部。
3. 反向代理及应用上传大小限制需允许至少 21MB 请求（含 multipart 开销）；附件接口自身始终限制文件内容为 20MB。
4. 数据库保存元数据和 SHA-256，原文件以 UUID 存储。备份/恢复应同时包含数据库及附件目录。

验收覆盖原始字节与哈希、打包下载、脚本/ZIP 预览、路径穿越、大小限制、同名冲突、跨 Skill ID、私有权限和 Agent 上传。
