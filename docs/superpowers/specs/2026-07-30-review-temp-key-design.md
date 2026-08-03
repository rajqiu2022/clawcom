# 用例评审临时密钥设计（免 Hub Token 参与评审）

日期：2026-07-30
状态：已确认，待实现

## 背景与目标

完全公开（`visibility=public_all`）的用例评审课题，目前仍要求调用方持有 Hub 侧凭证：
要么是 Web session，要么是注册过的 OpenClaw Bearer Token。没在 Hub 注册过的外部 Agent
无法参与评审。

本设计给这类课题增加**限时临时密钥**：Agent 带上密钥即可免 Hub Token 读取评审信息、
发表评论与评审意见、修改自己发的那条。密钥绑定单个课题、有明确有效期、可随时吊销。

## 已确认的决策

| 决策项 | 结论 |
| --- | --- |
| 权限边界 | 读用例/脑图 + 发讨论回复 + 发评审意见 + 改自己发的评审意见 |
| 署名 | 调用方在请求里自带名字，自由署名 |
| 可签发范围 | 仅 `visibility=public_all` 的 `board=case_review` 课题 |
| 有效期 | 签发时传小时数，缺省 7 天（168 小时），上限 30 天（720 小时） |
| 存储方式 | 明文存，页面可随时复制、可发给多个 Agent |
| 评审结论 | 密钥提交意见时 `verdict` 强制为 `comment`，禁止 approve/reject |
| 冒名处理 | 名字原样存，靠 `author_key_id` 推断并输出 `is_external`，前端挂"外部"徽标 |
| 现有缺口 | `_do_add_review_comment` 对普通登录用户缺可见性校验，本次**不改**其行为，只约束密钥身份 |
| 管理界面 | 课题详情页提供签发/复制/查看使用情况/吊销 |

## 数据模型

### 新表 `topic_review_keys`

```
id             INT PK
topic_id       INT NOT NULL          -- 绑定单个课题
key_token      VARCHAR(64) UNIQUE    -- 明文，secrets.token_urlsafe(24)
name           VARCHAR(100)          -- 用途备注（如"外部 XX 团队"），仅 Hub 侧识别，不作署名
expires_at     DATETIME NOT NULL     -- 过期时刻
revoked_at     DATETIME NULL         -- 非空即已吊销
created_by     VARCHAR(100)          -- 签发人 username/claw_name
created_at     DATETIME
last_used_at   DATETIME NULL         -- 审计
use_count      INT DEFAULT 0         -- 审计
```

`expires_at` 设为 NOT NULL 而不复用 `TopicGrant` 那种"空=长期有效"的语义：临时密钥
不存在长期有效的场景，用类型约束把它排除掉，比靠代码纪律更可靠。

### 两张评论表增列

`case_review_comments` 与 `topic_replies` 各增一列：

```
author_key_id  INT NULL   -- 非空表示这条由临时密钥发出，值为 topic_review_keys.id
```

这一列同时承担两个职责：**编辑权归属判定**和**外部身份标识**。不新增 `is_external`
字段，避免同一事实存两份而失去同步。

## 密钥有效性判定

每次请求实时判定，四个条件全部满足才算有效：

1. `key_token` 精确命中
2. `revoked_at IS NULL`
3. `expires_at > now()`
4. 所属课题当前仍是 `board=case_review` 且 `visibility=public_all`

第 4 条是关键：课题可见性一旦收紧，名下所有密钥立即失效，不需要额外清理动作，也不会
出现"课题已转为项目内可见、外部密钥还能读"的窗口。

判定逻辑集中在 `web/app/services/review_keys.py`，与 Flask 请求上下文解耦，便于单测。

## 密钥传递

优先请求头 `X-Review-Key: <token>`；兼容查询参数 `?review_key=<token>`。

请求头是推荐姿势（不进 access log、不随 Referer 外泄）；查询参数保留是为了 curl 与
浏览器能直接试，与现有测试报告分享链接的使用习惯一致。

## 身份注入与鉴权链路

### 注入点

`topics.py` 的 `_get_caller_info()` 增加第三条分支，位于 session 与 Bearer 之后：

```python
{
    'username': <清洗后的调用方自带名字，缺省用密钥备注名>,
    'user_id': None,
    'claw_id': None,
    'role': 'user',
    'is_admin': False,
    'project_ids': [],
    'review_key_id': <key.id>,
    'review_key_topic_id': <key.topic_id>,
}
```

选这个注入点的原因：`topics.py` 全部评审端点的身份都来自这一个函数，且它与全局
`g._auth_claw` 解耦。注入之后，`can_view_topic` 对 `public_all` 本就直接放行，
**可见性服务 `review_visibility.py` 的判定逻辑一行都不用改**。

### 课题绑定校验

`_get_caller_info()` 拿不到路由里的 `topic_id`，所以"密钥只对签发它的课题有效"这条
约束放在 `_ensure_topic_viewable` 与 `_ensure_topic_commentable` 两个统一入口：调用者
带 `review_key_id` 时，若 `review_key_topic_id != topic.id` 直接 403。

这两个函数覆盖所有评审端点，因此不存在漏网的路径。

### 全局门禁放行

`web/app/api/__init__.py` 的 `require_auth` 在 Bearer 分支之后、返回 401 之前增加密钥
分支：携带有效密钥且路径命中白名单则放行。白名单由
`review_keys.is_review_key_allowed_path(path, method)` 判定，写法沿用
`is_project_exempt_review_path` 的精确后缀 + 数字段前缀两种匹配。

允许的路径（`/api/v1/topics/<数字 id>` 之下）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `` | 课题详情（讨论回复随详情一起返回，无独立列表端点） |
| GET | `/review-mindmap` | 目录脑图（含 `?module_path=` 子树） |
| GET | `/review-cases/<数字>` | 单条用例的前提/步骤/预期 |
| GET | `/review-marks` | 读节点标记 |
| GET | `/review-rounds` | 评审轮次与已有意见 |
| POST | `/replies` | 发讨论回复 |
| POST | `/review-comments` | 发评审意见（自动落到当前开放轮次） |
| POST | `/review-rounds/<数字>/comments` | 发评审意见（指定轮次） |
| PUT | `/review-rounds/<数字>/comments/<数字>` | 改自己发的评审意见 |

明确**不放行**：`PUT /review-marks`（打标记权本次未开放）、`/review-status`、
`/review-summary`、`/grants`、`/close`、任何 DELETE、以及 `/api/v1/topics` 之外的
全部接口。密钥拿去访问白名单外的路径，等同未认证。

## 写入侧约束

### 署名清洗

调用方自带的 `author_name` 需：去首尾空白、剔除换行、截断到 50 字符（`topic_replies.author_name`
是 `VARCHAR(50)`，取两张表的下限）。清洗后为空则回落到密钥备注名；备注名也为空则用
`外部评审者`。

### 结论降级

密钥身份提交评审意见时，`verdict` 一律写 `comment`。请求里传 `approve`/`reject` 不报错、
直接按 `comment` 处理——因为 approve/reject 会改写轮次状态并回写用例库评审状态，属于决策
权，不交给匿名密钥。`score` 仍允许提交。

### 编辑权归属

`edit_review_comment` 的作者判定增加第三条分支：

```python
(caller.get('review_key_id') and caller['review_key_id'] == comment.author_key_id)
```

语义是同一把密钥的持有者之间可以互改（共享密钥本就等于共享身份），不同密钥互相隔离，
密钥改不了 Hub 用户的评论、Hub 用户也改不了密钥的评论（管理员除外，沿用现有规则）。
"每条仅 1 次修改机会"的 `is_edited` 限制照旧生效。

### 密钥身份的可见性校验

`_do_add_review_comment` 里只对密钥身份补一道 `_ensure_topic_commentable`；普通登录用户
路径保持现状不变，避免影响存量行为。

### 审计

密钥每次通过判定即更新 `last_used_at` 与 `use_count`。写操作照旧走 `log_action`，
operator 记为 `<署名>(外部密钥#<key_id>)`，便于事后追溯到具体哪把密钥。

## API 清单

### 管理侧（需登录，且为课题发起人或管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/topics/<id>/review-keys` | 签发。body：`{"name": "外部XX团队", "hours": 168}`。课题须为 public_all 的 case_review，否则 400 |
| GET | `/api/v1/topics/<id>/review-keys` | 列表，含明文 token、过期时间、使用次数、是否已失效 |
| DELETE | `/api/v1/topics/<id>/review-keys/<key_id>` | 吊销（写 `revoked_at`，不删行，保留审计） |

签发响应额外返回 `usage` 字段：一段可直接复制给 Agent 的调用示例（含 curl 与请求头写法）。

### 消费侧

不新增端点，复用上表白名单里的既有接口。密钥身份与 Hub 身份走同一套 URL，Agent 侧只需
多带一个请求头。

## 前端

课题详情页在 `visibility=public_all` 且 `board=case_review` 时，对发起人/管理员显示
「外部评审密钥」区块：

- 签发表单：备注名 + 有效期（1 天 / 7 天 / 30 天 / 自定义小时）
- 列表：明文 token（带复制按钮）、备注名、过期时间、使用次数、最近使用时间、状态、吊销按钮
- 已过期或已吊销的行置灰

评论与评审意见的展示：`author_key_id` 非空时在作者名后挂一个「外部」徽标。

## 安全边界（明确不做的事）

- 不做请求限流。内网场景，且密钥有 TTL 与吊销，先不引入限流复杂度。
- 不支持密钥打标记、下评审结论、改评审状态、删除任何内容。
- 不支持一把密钥跨多个课题。
- 不做密钥哈希存储。已确认明文存以便重复分发，风险由 TTL、吊销、审计与"仅 public_all"
  三重约束兜住。
- 不改动 `review_visibility.py` 既有判定逻辑，避免波及 Hub 用户与 Agent 的现有权限。

## 测试计划

`tests/test_review_keys.py`（纯函数，importlib 直载，无 DB）：

- 有效期计算：缺省 168 小时、上限截断到 720、非法输入回落
- 白名单判定：表中每条路径命中；`PUT /review-marks`、`/review-status`、DELETE、
  非数字 id、非 topics 前缀全部不命中
- 署名清洗：超长截断、空白回落、换行剔除

`tests/test_review_key_api.py`（HTTP 端到端，内存 SQLite）：

- 有效密钥可读课题详情、脑图、用例详情、轮次
- 过期 / 已吊销 / 课题非 public_all / token 不存在 → 各自被拒
- A 课题的密钥读 B 课题 → 403
- 密钥访问白名单外路径（`PUT /review-marks`、`/review-status`）→ 被拒
- 密钥发讨论回复与评审意见成功，`author_key_id` 落库、`is_external` 输出为真
- 密钥传 `verdict=approve` → 落库为 `comment`，轮次状态不变、用例库评审状态不变
- 密钥改自己发的意见成功；改另一把密钥发的 → 403；改 Hub 用户发的 → 403
- 同一条意见改第二次 → 400（沿用 is_edited）
- 签发接口：非发起人 → 403；课题非 public_all → 400；吊销后立即失效
- 回归：普通登录用户发评审意见的行为未变

## 部署与回滚

迁移脚本 `ops/migrations/20260730_review_temp_keys.sql`：建 `topic_review_keys` 表，
两张评论表 `ADD COLUMN author_key_id`。两者都写成可重复执行。

回滚策略：不删表不删列。把课题 `visibility` 改回 `public` 即可让全部密钥失效，
等同于功能下线。

部署沿用 `_deploy_case_review_scope_mindmap.py` 的套路：远端漂移预检 → SQL 迁移 →
备份 → 上传 → `py_compile` → 重启 → 路由与守护符号校验。
