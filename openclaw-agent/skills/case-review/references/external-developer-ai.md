# 外部 Developer AI 用例评审协议

## 适用输入

收到以下任一内容时使用本协议：

- `/developer-ai/collaborate#invite=<hub_ci_...>&exchange=<path>` 邀请链接；
- `hub_ci_...` 可撤销、带截止时间的邀请凭据；
- `hub_cs_...` 临时访问密钥；
- `case-review-bootstrap.v1` JSON 任务包。

不要向用户索取 Hub 长期 Token。不要把临时密钥写入代码仓库、评审正文、工单或长期日志。

## 发起方：生成对外评审链接

只有已登录的 Hub 用户或已注册 Hub Agent 可以创建邀请。调用方必须对目标项目拥有写权限；临时协作 Token 不能继续创建新邀请。

```http
POST {HUB_BASE}/api/v1/collaboration-sessions
Authorization: Bearer <当前 Hub Agent Token>
Idempotency-Key: case-review-invite-<stable-unique-key>
Content-Type: application/json

{
  "subject_type": "case_review",
  "subject_id": 22,
  "agent_identity": "developer-ai:racinggo:alice-codex",
  "invitation_ttl_minutes": 30,
  "token_ttl_minutes": 120,
  "max_calls": 500
}
```

参数约束：

- `subject_id`：现有 `case_review` Topic ID；
- `agent_identity`：必须区分团队、人员或 AI 实例，不能使用 `developer-ai:case-review` 等通用占位值；
- `invitation_ttl_minutes`、`token_ttl_minutes`：5-4320（最长 72 小时），缺省分别为 30、120；
- `max_calls`：1-5000，缺省 500；
- 重试相同创建请求时复用相同 `Idempotency-Key`，但邀请码明文只在首次创建响应中返回。

成功响应关键字段：

```json
{
  "id": 123,
  "subject": {"type": "case_review", "id": 22},
  "invitation_code": "hub_ci_xxx",
  "invitation_path": "/developer-ai/collaborate",
  "exchange_url": "/api/v1/collaboration-sessions/exchange",
  "invitation_expires_at": "...",
  "max_calls": 500,
  "scopes": ["case_review:read", "case_review:comment", "case_review:mark"]
}
```

使用 Hub 的浏览器入口拼接完整链接，邀请码必须放在 URL fragment 中：

```text
{HUB_WEB_URL}{invitation_path}#invite={urlencode(invitation_code)}&exchange={urlencode(exchange_url)}
```

示例：

```text
https://clawteam.woa.com/developer-ai/collaborate#invite=hub_ci_xxx&exchange=%2Fapi%2Fv1%2Fcollaboration-sessions%2Fexchange
```

只把完整链接发送给目标外部研发或其 AI。不要提前调用 exchange；聊天软件预览不会携带 fragment，也不应消耗邀请码。

## 发起方：查询、延期与撤销协作会话

查询指定评审已经签发的会话：

```http
GET {HUB_BASE}/api/v1/collaboration-sessions?subject_type=case_review&subject_id={topic_id}
Authorization: Bearer <当前 Hub Agent Token>
```

响应包含 `status`、`call_count/max_calls`、邀请和 Token 到期时间。发现链接误发、评审结束或访问异常时立即撤销：

需要延长或调整截止时间时，修改原会话，不要重新签发链接：

```http
PATCH {HUB_BASE}/api/v1/collaboration-sessions/{session_id}/deadline
Authorization: Bearer <当前 Hub Agent Token>
Idempotency-Key: case-review-deadline-<session_id>-<unique-key>
Content-Type: application/json

{"expires_at":"2026-08-23T18:00:00+08:00"}
```

截止时间必须晚于当前时间，且最长只能延至当前时间后 72 小时。会话处于：

- `pending`：更新邀请的 `invitation_expires_at`；
- `active`：同时更新邀请与现有临时密钥的截止时间；
- 有效期已过但底层仍为 `pending` 或 `active`：允许延期恢复；
- `revoked`、`completed` 或调用额度已耗尽：不能通过延期恢复。

成功响应包含 `deadline_field`、`expires_at` 和 `link_rotated=false`。延期不会更换邀请码、分享链接或已兑换的访问 Token，接收方继续使用原链接或原 Token。重试同一次延期必须复用相同 `Idempotency-Key`。

发现链接误发、评审结束或访问异常时立即撤销：

```http
POST {HUB_BASE}/api/v1/collaboration-sessions/{session_id}/revoke
Authorization: Bearer <当前 Hub Agent Token>
Idempotency-Key: case-review-revoke-<session_id>-<unique-key>
Content-Type: application/json

{"reason":"manual_revoke"}
```

撤销后对应 `hub_cs_...` 临时密钥立即失效。不要删除审计记录。

## 从邀请链接兑换任务包

邀请参数位于 URL fragment，不会随页面请求发送给服务器。解析 `invite` 和 `exchange` 后调用：

```http
POST {HUB_BASE}/api/v1/collaboration-sessions/exchange
Content-Type: application/json

{"invitation_code":"hub_ci_xxx"}
```

同一邀请链接在 `link_expires_at` 前可以再次兑换，用于临时 Token 过期、丢失或新的 Agent 回合恢复评审。再次兑换会签发新 `access_token`，返回 `token_rotated=true`、`previous_token_invalidated=true`，并立即废止旧 Token；`bootstrap` 是 `case-review-bootstrap.v1` 任务包。后续请求统一使用：

```http
Authorization: Bearer hub_cs_xxx
Content-Type: application/json
```

优先使用 `bootstrap.endpoints`，不要自行扩展 URL 或访问任务包未列出的 Hub API。

## 标准执行顺序

1. `GET bootstrap.endpoints.context`：读取评审概要、范围、开放轮次、标记图例和 API links。
2. `GET bootstrap.endpoints.reviews`：读取当前已提交的评审记录，先理解已有结论，避免重复提交。
3. 分页 `GET bootstrap.endpoints.cases?page=1&page_size=100`：只评审接口实际返回的用例。
4. 创建或维护自己的评审记录；必要时写节点标记。
5. 输出发现、残余风险和已经写回 Hub 的动作，不输出临时密钥。

所有写请求必须携带唯一 `Idempotency-Key`。重试同一请求时复用原 key；改变请求内容时换新 key。

## 评审记录 API

### 读取当前记录

```http
GET /api/v1/shift-left/case-reviews/{topic_id}/reviews?page=1&page_size=100
```

响应字段：

- `owned_by_me=true`：记录由当前临时协作会话创建；
- `can_modify=true`：当前记录仍允许修改或删除；
- `source_type=developer_ai|claw|user`：记录来源。

读取所有记录，但只能修改或删除 `owned_by_me=true && can_modify=true` 的记录。

### 创建评审记录

```http
POST /api/v1/shift-left/case-reviews/{topic_id}/reviews
Idempotency-Key: <unique-key>

{
  "content": "基于证据的 Markdown 评审意见",
  "score": 8,
  "round_id": 123
}
```

`score` 为 1-10 的整数；`round_id` 使用 context 返回的 `open_round_id`。没有开放轮次或评审已关闭时停止写入并报告原因。

### 修改自己的记录

```http
PATCH /api/v1/shift-left/case-reviews/{topic_id}/reviews/{review_id}
Idempotency-Key: <unique-key>

{
  "content": "更新后的评审意见",
  "score": 9
}
```

`content`、`score` 至少提供一个；`score:null` 表示清除评分。不得修改其他评审人的记录。

### 删除自己的记录

```http
DELETE /api/v1/shift-left/case-reviews/{topic_id}/reviews/{review_id}
Idempotency-Key: <unique-key>
```

删除是软删除：记录会从评审列表、概要统计和正常页面消失，但保留审计证据。不得删除其他评审人的记录。

## 评审边界

- 默认 scopes 为 `case_review:read`、`case_review:comment`、`case_review:mark`。
- 没有 `case_review:decision` 时不得提交 approve/reject。
- 只能读取 `review_case_ids` 或 `review_module_paths` 限定的用例。
- 临时密钥过期或丢失时，仅在原邀请链接仍有效的前提下重新兑换，并停止使用旧 Token。
- 邀请链接到期、会话撤销或调用额度耗尽后立即停止，请邀请人延期或重新授权。
- 403 `CASE_REVIEW_RECORD_NOT_OWNER` 表示记录不属于当前会话；不得规避。
- 409 `CASE_REVIEW_RECORD_LOCKED` 表示评审或轮次已锁定；不得继续写入。
