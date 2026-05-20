# 测试账号管理 (test-account-manager)

## 简介

本 Skill 用于让 OpenClaw 直接从 Hub 的"测试账号池"领用 / 释放 QQ、微信等测试账号，覆盖：

- 查询账号列表（按平台、状态过滤）
- 领用空闲账号 → 拿到密码 → 登录目标系统
- 完成后**主动释放**，把状态改回空闲
- 异常时（被风控、登录失败）打 abnormal 标签
- 查看某账号最近使用流水（默认最近 10 次）

> 重要：账号池是**全局共享资源**，每个 claw **必须**遵守"领用→释放"闭环。
> 不释放会导致资源被你长时间占用，其他 claw 拿不到。

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar`（SSE 通信链路在线）
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub API: http://clawteam.woa.com:18800/api/v1
认证:    Authorization: Bearer {HUB_API_TOKEN}
```

---

## 权限边界（必读）

| 操作                       | 谁能做                                            |
|----------------------------|---------------------------------------------------|
| 列表 / 单查 / usage-history| 任意已认证 claw 或 web 用户                        |
| acquire（领用）            | 任意已认证 claw 或 web 用户                        |
| release（释放自己占用的）  | 任意已认证 claw 或 web 用户                        |
| release（强制释放别人的）  | 仅 `super_admin` 用户 / `admin` claw（龙虾王）    |
| mark-abnormal              | 任意已认证 claw 或 web 用户                        |
| recover（异常→空闲）       | 仅 `super_admin` 用户 / `admin` claw（龙虾王）    |
| **create / update / delete** | **仅 `super_admin` 用户 / `admin` claw（龙虾王）**|

普通 claw 调 create/delete/recover 会拿到 403，**不要重试**，去找龙虾王或超级管理员处理。

---

## 数据状态机

```
            acquire
   idle  ────────────►  in_use
    ▲                     │
    │  release            │ mark-abnormal (任何状态)
    │                     ▼
    └────────  recover ── abnormal
              (仅 admin)
```

字段：

| 字段              | 含义                                                  |
|-------------------|-------------------------------------------------------|
| `id`              | 账号主键                                              |
| `platform`        | `qq` / `wechat` / `other`                             |
| `account`         | 账号                                                  |
| `password`        | 密码（仅 acquire 成功 或 admin `?include_password=true` 时返回）|
| `status`          | `idle` / `in_use` / `abnormal`                        |
| `current_user`    | 当前占用者名（claw 名 或 web 用户名）                 |
| `current_claw_id` | 当前占用 claw 的 ID（如果是 claw 占用）               |
| `current_purpose` | 当前使用途径/备注（acquire 时上报）                   |
| `last_login_at`   | 最近一次领用时间                                      |
| `notes`           | 账号自身固有备注（创建时填）                          |

---

## API 接口

### 1. 列表查询

```
GET /api/v1/test-accounts?platform=qq&status=idle&limit=50

Header: Authorization: Bearer {HUB_API_TOKEN}

可选参数：
  platform        qq / wechat / other
  status          idle / in_use / abnormal
  keyword         模糊匹配 account / notes / current_user / current_purpose
  include_deleted true|false      默认 false
  include_password true|false     默认 false（仅 admin 生效）
  limit, offset                   默认 limit=100，最大 500
```

返回示例：

```json
{
  "total": 12,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": 7,
      "platform": "qq",
      "account": "1234567",
      "status": "idle",
      "current_user": "",
      "current_claw_id": null,
      "current_purpose": "",
      "last_login_at": "2026-04-21 12:30:00",
      "notes": "广州号-小天专用",
      "password_present": true,
      "created_by": "龙虾王",
      "created_at": "...",
      "updated_at": "...",
      "is_deleted": false
    }
  ]
}
```

### 2. 单查

```
GET /api/v1/test-accounts/{ID}
```

返回单个账号 dict。同样默认不带 password。

### 3. 领用（拿密码登录）

> ⚠️ **领用时必须显式上报 `purpose`（使用途径/备注）。**
> 这是审计线索：日后排查"谁在什么场景下用了哪个号"全靠它。

```
POST /api/v1/test-accounts/{ID}/acquire

Header:
  Authorization: Bearer {HUB_API_TOKEN}
  Content-Type: application/json

请求体：
{
  "purpose": "登录 QQ 飞车手游执行登录链路冒烟",   // 必填，>=2 字
  "force": false                                  // 仅 admin 可加 true 强制抢占
}
```

成功 200，返回带 `password`：

```json
{
  "id": 7,
  "platform": "qq",
  "account": "1234567",
  "password": "...真实密码...",
  "status": "in_use",
  "current_user": "小天",
  "current_claw_id": 6,
  "current_purpose": "登录 QQ 飞车手游执行登录链路冒烟",
  "last_login_at": "2026-04-21 14:02:11",
  "acquired_at": "2026-04-21 14:02:11",
  "notes": "..."
}
```

冲突 409：

- 已被别人占用 → `{"error": "账号正在被使用", "current_user": "..."}`
  → **换一个空闲账号**（先 list `?status=idle`），不要硬抢。
- 状态 abnormal → `{"error": "账号当前为异常状态..."}`
  → 找龙虾王 recover 后再用。

### 4. 释放（用完必须做）

> ✅ **领用闭环**：登录 / 用例 / 链路验证一旦结束，**立刻**调一次 release。
> 哪怕程序异常退出，也要在 try/except 里兜底释放。

```
POST /api/v1/test-accounts/{ID}/release

请求体（可选）：
{
  "summary": "登录冒烟通过，3 步无报错"   // 用途总结，进流水
}
```

规则：

- 只能释放自己当前占用的账号；不是自己的会 403。
- admin（龙虾王）可以无视占用人强制释放。
- 已经 idle 的账号再调 release 不会出错，会返回 `"账号本就是空闲状态"`。

### 5. 标记异常

```
POST /api/v1/test-accounts/{ID}/mark-abnormal

请求体：
{
  "reason": "登录返回需要扫码 / 被风控 / 密码错误",   // 必填，>=2 字
  "release": true                                      // 默认 true，同步清空 current_user
}
```

任何 claw 都可以打异常标签。打完之后状态变 `abnormal`，**别再领用**，等 admin 处理。

### 6. 恢复（仅 admin）

```
POST /api/v1/test-accounts/{ID}/recover

请求体（可选）：
{ "note": "确认已脱风控" }
```

把 `abnormal` 改回 `idle`。普通 claw 调会 403。

### 7. 使用历史（最近 10 次）

```
GET /api/v1/test-accounts/{ID}/usage-history?limit=10
```

返回示例：

```json
{
  "account_id": 7,
  "platform": "qq",
  "account": "1234567",
  "count": 10,
  "items": [
    {
      "id": 234,
      "action": "acquire",
      "actor_type": "claw",
      "actor_name": "小天",
      "actor_claw_id": 6,
      "purpose": "登录冒烟",
      "extra": {},
      "created_at": "2026-04-21 14:02:11"
    },
    {
      "id": 235,
      "action": "release",
      "actor_name": "小天",
      "purpose": "登录冒烟通过",
      "extra": {"summary": "登录冒烟通过"},
      "created_at": "2026-04-21 14:09:43"
    }
  ]
}
```

可选 `?action=acquire|release|mark_abnormal|recover|create|delete` 过滤。

### 8. 创建账号（仅 admin）

```
POST /api/v1/test-accounts

请求体：
{
  "platform": "qq",          // qq / wechat / other
  "account": "1234567",
  "password": "xxxx",
  "notes": "广州号-小天专用"
}
```

普通 claw 调用 → 403。`(platform, account)` 唯一，重复返回 409。

### 9. 更新账号（仅 admin）

```
PUT /api/v1/test-accounts/{ID}

请求体（按需）：
{ "password": "...", "notes": "...", "platform": "qq", "account": "..." }
```

### 10. 删除账号（仅 admin，软删除）

```
DELETE /api/v1/test-accounts/{ID}
```

如果删除时账号还 in_use，会先记一条 release 流水再删，避免日志断裂。

---

## 标准工作流

### 普通 claw 单次任务领用

```bash
HUB="http://clawteam.woa.com:18800/api/v1"
TOKEN="<HUB_API_TOKEN>"

# 1. 找一个空闲的 QQ 号
RESP=$(curl -s -H "Authorization: Bearer $TOKEN" \
  "$HUB/test-accounts?platform=qq&status=idle&limit=1")
ACC_ID=$(echo "$RESP" | python -c "import sys,json; d=json.load(sys.stdin); print(d['items'][0]['id'] if d['items'] else '')")
[ -z "$ACC_ID" ] && echo "没有空闲 QQ 号" && exit 1

# 2. 领用并拿到密码
ACQ=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"purpose":"登录冒烟"}' \
  "$HUB/test-accounts/$ACC_ID/acquire")
ACCOUNT=$(echo "$ACQ" | python -c "import sys,json;print(json.load(sys.stdin)['account'])")
PASSWORD=$(echo "$ACQ" | python -c "import sys,json;print(json.load(sys.stdin)['password'])")

# 3. 用账号执行任务...
echo "用 $ACCOUNT / $PASSWORD 登录..."

# 4. 完成后释放（关键！）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"summary":"登录冒烟通过"}' \
  "$HUB/test-accounts/$ACC_ID/release"
```

### Python 兜底释放模板

```python
import urllib.request, json, os
HUB = os.environ["HUB_URL"].rstrip("/") + "/api/v1"
TOKEN = os.environ["HUB_API_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def call(method, path, body=None):
    req = urllib.request.Request(HUB + path, method=method, headers=HEADERS,
                                 data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

acc_id = None
try:
    idle = call("GET", "/test-accounts?platform=qq&status=idle&limit=1")
    if not idle["items"]:
        raise RuntimeError("无空闲 QQ 号")
    acc_id = idle["items"][0]["id"]
    info = call("POST", f"/test-accounts/{acc_id}/acquire",
                {"purpose": "我是XX任务，登录XX验证"})
    do_login(info["account"], info["password"])  # 你的业务
finally:
    if acc_id:
        try:
            call("POST", f"/test-accounts/{acc_id}/release",
                 {"summary": "任务完成"})
        except Exception:
            pass  # 至少日志里有 acquire 记录，admin 可手动释放
```

---

## 常见错误与排查

| 现象                                   | 原因                                  | 怎么办                                                 |
|---------------------------------------|---------------------------------------|--------------------------------------------------------|
| `409 账号正在被使用`                   | 该号被别的 claw 占用                  | 换一个空闲号；非紧急别加 force                         |
| `409 账号当前为异常状态`               | 之前被人 mark abnormal                | 找龙虾王 `POST /recover`                                |
| `400 purpose ... 至少 2 个字符`        | acquire 没传 purpose 或太短           | acquire 必须带具体场景                                 |
| `403 只能释放自己当前占用的账号`       | 不是占用人想 release                  | 联系当前占用 claw；或找 admin 强制释放                 |
| `403 仅超级管理员或龙虾王可执行此操作` | 普通 claw 想 create/delete/recover    | 别重试。提交申请到龙虾王，或让 admin 直接做            |
| acquire 成功但 password 字段为空       | 服务端密码字段为空（创建时漏填）      | 找 admin PUT 一下 password                             |

---

## 触发词

- "申请测试账号"、"借一个 QQ 号"、"借微信号"、"领账号"
- "释放账号"、"还账号"、"退账号"、"还回去"
- "查空闲账号"、"看哪些账号空"
- "账号被占用"、"账号是谁在用"
- "标记异常"、"账号被风控"、"号没了"
- "新增测试账号"、"删除测试账号"（→ 转给龙虾王）

---

## 设计约束

1. 账号密码以**明文**存于 Hub MySQL（仅供内网测试用），**不要**把密码外发到任何外网服务、群消息或截图上。
2. 一次任务领用 → 一次释放，是**强约束**。如果业务流程长（>30 分钟），考虑加心跳续约（暂未实现，后续如需要再加 `/keepalive`）。
3. 不允许把 `acquire` 接口写进**自动化脚本的 setUpClass 一次性领用、整轮跑完才释放**——会饿死其他 claw。需要长期占用请走 admin 申请专属号 + `notes` 标记。
4. 若任务 crash，下次恢复时**先看 `usage-history`**，如果发现自己上一次 acquire 没配对的 release，先补一次 release 再继续。
