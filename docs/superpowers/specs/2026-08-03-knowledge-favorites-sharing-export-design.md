# 知识库收藏、匿名分享与 Markdown 导出设计

## 目标

让知识库具备与测试报告一致的个人收藏和匿名外链能力，并支持单条知识直接下载为 Markdown 文件。

## 已确认规则

- 收藏按 Web 用户或 Agent 独立保存。
- 列表支持「仅收藏」筛选，列表和详情返回 `is_favorite`。
- 分享链接允许知识创建者及管理员生成、刷新和撤销。
- 创建者可以分享自己能编辑的任意审核状态知识。
- 匿名分享只读，不暴露数据库 ID、Memos ID、审核意见和内部 Agent ID。
- 登录态和匿名分享页都支持下载单条 Markdown。
- 删除知识时清理收藏；知识硬删除后分享链接自然失效。

## 数据模型

`KnowledgeFavorite` 已存在，无需新表。

`KnowledgeEntry` 新增：

```text
is_shared BOOLEAN DEFAULT FALSE
share_token VARCHAR(64) UNIQUE NULL
shared_at DATETIME NULL
```

Token 使用 `secrets.token_urlsafe(16)`；重复启用保留原 token，`refresh=1` 生成新 token。

## 权限

收藏：

- Web 用户收藏归 `user_id`。
- Bearer Agent 收藏归 `claw_id`。
- 未认证调用拒绝。

分享管理：

- `super_admin/admin` 可管理。
- Web 用户 `username == entry.created_by` 可管理。
- Bearer Agent `claw.id == entry.source_openclaw_id` 或 `claw.name == entry.created_by` 可管理。
- Agent owner 的 Web 用户可管理该 Agent 创建的知识。

匿名读取：

- 只校验 token、`is_shared=True` 和知识仍存在。
- 不受知识审核状态限制，因为创建者已明确允许分享任意可编辑状态。

## API

收藏：

```text
POST   /api/v1/knowledge/{id}/favorite
DELETE /api/v1/knowledge/{id}/favorite
GET    /api/v1/knowledge?favorite=1
```

分享：

```text
POST   /api/v1/knowledge/{id}/share
POST   /api/v1/knowledge/{id}/share?refresh=1
DELETE /api/v1/knowledge/{id}/share
GET    /api/v1/knowledge/shared/{token}
```

Markdown：

```text
GET /api/v1/knowledge/{id}/export.md
GET /api/v1/knowledge/shared/{token}/export.md
```

下载响应：

```text
Content-Type: text/markdown; charset=utf-8
Content-Disposition: attachment; filename="knowledge.md"; filename*=UTF-8''...
X-Content-Type-Options: nosniff
```

文件内容为标题、元数据和原始 Markdown 正文。文件名过滤 Windows 非法字符 `<>:"/\|?*`、控制字符和尾部点/空格，限制长度并保留 `.md`。

## 页面

知识列表：

- 卡片右上角增加星标按钮。
- 收藏状态始终可见，避免只能 hover 后发现。
- 筛选区增加「仅收藏」。
- 已分享条目显示链接标识。

知识详情：

- 增加收藏、导出 Markdown。
- 有分享管理权限时显示生成、打开、刷新、撤销操作。

匿名页：

- 新增 `/k/{token}` 和备用 `/knowledge/share/{token}`。
- 显示知识标题、元数据和 Markdown 正文。
- 提供「下载 Markdown」。

## 安全

- API 网关只放行 `/api/v1/knowledge/shared/`。
- View 登录检查只放行 `/k/` 和 `/knowledge/share/`。
- 匿名 payload 使用显式白名单，不能直接调用完整 `to_dict()`。
- Markdown 按下载处理并设置 `nosniff`，不以内联 HTML 页面响应。
- 分享刷新后旧 token 立即失效。

## 测试

- 收藏归属、幂等收藏/取消、仅收藏筛选。
- 创建者、Agent、Agent owner、管理员及无权限用户的分享权限。
- token 生成、刷新、撤销、无效 token。
- 匿名 payload 脱敏。
- 登录态与匿名 Markdown 下载。
- 中文文件名、Windows 非法字符、超长标题和正文原样保留。
- 删除知识清理收藏并使分享失效。
