# Secrets Vault — 密钥保险箱

## 使命

让 Agent 安全管理和使用外部 API 凭据（Tavily Key、企微 Webhook、第三方 Token 等），避免明文硬编码在 Skill 内容或配置文件中。

## 核心理念

1. **加密存储**：所有密钥值在 Hub 服务端加密存储，数据库中不存明文
2. **占位符引用**：Skill 模板里只写 `${SECRET:key_name}`，运行时自动替换
3. **Owner 隔离**：每个 OpenClaw 只能读写自己名下的密钥，不可跨 claw 访问
4. **审计可追溯**：记录每次读取时间和次数

## 触发场景

1. **存储密钥**：初次获得外部 API Key 时，存入保险箱
2. **使用密钥**：执行 Skill 前解析占位符获取明文
3. **更新密钥**：Key 轮换时更新值
4. **查看/清理**：列出已有密钥或删除过期的

## Hub API 端点

> BASE = `{{HUB}}/api/v1`

### 密钥管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/secrets` | 列出当前 claw 名下所有密钥（不返回明文） |
| POST | `/secrets` | 创建或更新密钥 |
| GET | `/secrets/{key}` | 获取单条密钥**明文**（记录使用次数） |
| DELETE | `/secrets/{key}` | 删除密钥 |
| POST | `/secrets/resolve` | 批量解析占位符，替换为明文 |

### 认证方式

所有接口必须携带 Bearer Token：

```
Authorization: Bearer {{TOKEN}}
```

`{{TOKEN}}` 为本 OpenClaw 的 API Token，Hub 据此识别 owner_claw_id。

## 工作流

### 1. 存储密钥

当从用户或其他系统获得一个 API Key 时：

```http
POST {{HUB}}/api/v1/secrets
Authorization: Bearer {{TOKEN}}
Content-Type: application/json

{
  "key": "tavily_api_key",
  "value": "tvly-dev-xxxxxxxxxxxxx",
  "description": "Tavily 联网搜索 API Key，用于 search skill"
}
```

**key 命名规范**：
- 仅允许 `[a-zA-Z0-9_-]`，1-120 字符
- 推荐格式：`{服务名}_{用途}_{环境}`，例如 `wecom_webhook_test`、`tavily_api_key`

**响应**（201 Created）：
```json
{
  "id": 1,
  "key": "tavily_api_key",
  "description": "Tavily 联网搜索 API Key，用于 search skill",
  "has_value": true,
  "action": "created"
}
```

如果同名 key 已存在，则更新值（返回 200 + `"action": "updated"`）。

### 2. 在 Skill 中使用占位符

Skill 模板里不要写明文 Key，而是用占位符：

```markdown
## 配置

- Tavily API Key: `${SECRET:tavily_api_key}`
- 企微 Webhook: `${SECRET:wecom_webhook_alerts}`
```

### 3. 运行时解析占位符

执行 Skill 前，调用 resolve 接口批量替换占位符为明文：

```http
POST {{HUB}}/api/v1/secrets/resolve
Authorization: Bearer {{TOKEN}}
Content-Type: application/json

{
  "text": "curl -H 'Authorization: Bearer ${SECRET:tavily_api_key}' https://api.tavily.com/search ..."
}
```

**响应**：
```json
{
  "resolved": "curl -H 'Authorization: Bearer tvly-dev-xxxxxxxxxxxxx' https://api.tavily.com/search ...",
  "placeholders": ["tavily_api_key"],
  "used": ["tavily_api_key"],
  "missing": []
}
```

- `resolved`：占位符已被替换为明文的完整文本
- `used`：成功解析的 key 列表
- `missing`：找不到的 key（保留原占位符不替换）

### 4. 直接获取单条明文

如果只需要一个 Key 的值：

```http
GET {{HUB}}/api/v1/secrets/tavily_api_key
Authorization: Bearer {{TOKEN}}
```

**响应**：
```json
{
  "id": 1,
  "key": "tavily_api_key",
  "value": "tvly-dev-xxxxxxxxxxxxx",
  "description": "...",
  "use_count": 5,
  "last_used_at": "2026-06-02 12:30:00"
}
```

### 5. 列出所有密钥

```http
GET {{HUB}}/api/v1/secrets
Authorization: Bearer {{TOKEN}}
```

返回当前 claw 名下所有密钥元信息（**不含明文**），用于检查是否已存在某个 key。

### 6. 删除密钥

```http
DELETE {{HUB}}/api/v1/secrets/tavily_api_key
Authorization: Bearer {{TOKEN}}
```

## 最佳实践

1. **永远不要在日志中打印明文**——resolve 后立即使用，不要存到变量或日志
2. **Key 命名统一小写下划线**——`tavily_api_key` 而非 `TavilyApiKey`
3. **描述写清用途**——方便 owner 在 Web 页面管理时知道这个 key 是干什么的
4. **定期清理无用 key**——用 `GET /secrets` 查看 `use_count`，长期不用的考虑删除
5. **先检查再创建**——`GET /secrets` 列表看 key 是否已存在，避免重复创建覆盖

## 共享机制

密钥支持三种共享范围（`share_scope`）：

| 范围 | 说明 | 页面可见 | 谁能读 | 谁能改/删 |
|------|------|---------|--------|----------|
| `private` | 默认，仅 owner | ✅ owner | owner | owner |
| `project` | 共享给同项目 | ❌ 他人不可见 | 同 project_id 的 claw（仅 Bearer Token） | owner |
| `public` | 共享给所有人 | ❌ 他人不可见 | 所有 claw（仅 Bearer Token） | owner |

创建时指定共享：

```http
POST {{HUB}}/api/v1/secrets
Authorization: Bearer {{TOKEN}}
Content-Type: application/json

{
  "key": "shared_tavily_key",
  "value": "tvly-xxx",
  "description": "团队共用 Tavily Key",
  "share_scope": "project",
  "share_project_id": 1
}
```

**注意**：
- 被共享的密钥在他人的 Web 页面**不可见**（防止随意查看）
- 他人只能通过 `GET /secrets/{key}` 或 `POST /secrets/resolve`（Bearer Token）读取
- 他人**不能修改或删除**共享密钥，只有 owner 可以
- `resolve` 优先取 owner 自己的同名 key，自己没有时才 fallback 到共享的

## 错误处理

| HTTP 状态码 | 含义 | 处理方式 |
|------------|------|---------|
| 401 | 未认证（Token 无效或缺失） | 检查 Authorization 头 |
| 404 | key 不存在 | 确认 key 名拼写正确，或提示用户先存入 |
| 400 | key 格式非法或 value 缺失 | 检查 key 命名规范 |

## 安全提示

- 明文只在 `GET /secrets/{key}` 和 `POST /secrets/resolve` 的响应中出现
- 列表接口 `GET /secrets` **不返回明文**，只返回元信息
- 每次读取明文都会递增 `use_count` 并记录 `last_used_at`
- Owner 在 Web 管理页面可随时查看使用频率、撤销（删除）密钥


