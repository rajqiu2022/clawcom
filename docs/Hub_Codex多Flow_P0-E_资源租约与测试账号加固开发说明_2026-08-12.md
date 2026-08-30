# Hub Codex 多 Flow：P0-E 资源租约与测试账号加固开发说明

日期：2026-08-12  
状态：代码与自动化测试完成，尚未部署

## 1. 调整后的范围

本阶段按“复用现有 Hub 功能”的原则拆为两部分：

1. 通用 `resource_leases` 只管理 Unity、Bridge 和设备；
2. 测试账号继续使用现有 `test-account-manager` Skill、`test_accounts` 表和 `/test-accounts` API，仅加固并发与 TTL。

通用租约明确拒绝 `account:{id}`，避免通用租约和测试账号池各保存一份互相矛盾的账号占用状态。正式用例库 writer 继续由 promotion 的数据库写锁保护，也不进入本阶段通用租约。

本阶段覆盖 AT-08、AT-09。

## 2. 通用资源租约

首批资源键：

- `unity:{project}:{workspace}`
- `bridge:{project}:{workspace}:{channel}`
- `device:{device_serial}`

### 2.1 原子申请

`POST /api/v1/resource-leases/acquire`

```json
{
  "resource_keys": [
    "unity:racinggo:dev2",
    "bridge:racinggo:dev2:ui",
    "device:android-001"
  ],
  "owner_type": "workflow_run",
  "owner_id": "118",
  "controller_run_id": "codex-cycle-20260812-01",
  "priority": 100,
  "ttl_seconds": 900,
  "idempotency_key": "run-118-editor-lease"
}
```

规则：

- 一次最多申请 50 个资源，全部空闲才写入；任何一个冲突则一个都不占。
- TTL 范围 30～3600 秒，默认 900 秒。
- 同一请求幂等重放返回原 lease group。
- `active_slot` 唯一约束负责跨进程并发保护，不仅依赖应用层查询。
- priority 只记录和展示，P0 不强杀、抢占低优先级持有者。
- 冲突返回 owner、controller、priority、expires_at 和 `retry_after_seconds`。

### 2.2 续租和释放

- `POST /api/v1/resource-leases/{lease_id}/renew`
- `POST /api/v1/resource-leases/{lease_id}/release`

lease ID 属于一个原子 group。续租或释放任意一个 ID 时，对整个 group 操作，避免 bundle 退化成部分占用。

仅最初申请的认证主体或管理员可以续租/释放。已过期租约不能续租，必须重新 acquire。

### 2.3 查询和审计

- `GET /api/v1/resource-leases?resource_key=...&status=active`
- `GET /api/v1/resource-leases/{lease_id}/events`

查询、申请和续租会触发到期收敛。到期时整个 group 置为 `expired`，清空唯一 active slot，并追加 `expired` 事件；历史行不会删除。

### 2.4 明确拒绝账号资源

申请 `account:7` 返回：

```json
{
  "code": "USE_TEST_ACCOUNT_MANAGER",
  "message": "account resources must use the existing test-account-manager API"
}
```

## 3. 测试账号 Skill 加固

原接口保持不变：

- `GET /api/v1/test-accounts`
- `POST /api/v1/test-accounts/{id}/acquire`
- `POST /api/v1/test-accounts/{id}/release`
- abnormal/recover/history/create/update/delete 均保留。

### 3.1 原子领用

`acquire` 查询账号时增加数据库行锁。两个 worker 同时看到同一个空闲账号时，只允许一个事务将其变成 `in_use`，另一个返回 409。

### 3.2 TTL 和 Run 追溯

`acquire` 新增可选参数：

```json
{
  "purpose": "登录链路冒烟",
  "ttl_seconds": 1800,
  "workflow_run_id": 118,
  "controller_run_id": "codex-cycle-20260812-01"
}
```

- TTL 范围 60～7200 秒，默认 1800 秒；
- 成功后仍按旧契约返回密码；
- 新增 `lease_expires_at`、`lease_heartbeat_at`、Workflow/Controller 追溯字段；
- 老客户端只传 `purpose` 仍可正常工作。

### 3.3 keepalive 和过期回收

`POST /api/v1/test-accounts/{id}/keepalive`

```json
{ "ttl_seconds": 1800 }
```

- 当前 holder 或管理员可续租；
- 已过期租约不能通过 keepalive 复活；
- list/get/acquire/release/keepalive 会机会式回收已到期账号；
- 自动回收记录 `expire` 流水，续租记录 `renew` 流水；
- release、mark-abnormal、recover 和删除时清理租约上下文。

现有 `openclaw-agent/skills/test-account-manager/SKILL.md` 已同步新字段和 keepalive 用法。

## 4. 数据结构

新增：

- `resource_leases`：每个 group 的每个资源一行；
- `resource_lease_events`：acquired/renewed/released/expired 追加审计。

扩展 `test_accounts`：

- `lease_ttl_seconds`
- `lease_expires_at`
- `lease_heartbeat_at`
- `workflow_run_id`
- `controller_run_id`

扩展 `test_account_usage_logs` ENUM：

- action 新增 `renew`、`expire`；
- actor_type 新增 `system`。

## 5. 部署顺序

1. 备份数据库；
2. 执行：

   ```sql
   ops/migrations/20260812_resource_leases_and_test_account_ttl.sql
   ```

3. 确认资源租约两张表和测试账号五个新增字段存在；
4. 部署 Hub 应用代码；
5. 更新/分发 `test-account-manager` Skill；
6. 先用测试账号验证旧 acquire/release，再验证 keepalive；
7. 用两个测试 owner 验证 Unity 冲突和超时重领；
8. 最后让 Flow12/Codex 控制器接入通用租约。

迁移不会给现有 `in_use` 账号补 `lease_expires_at`。这些历史占用保持旧的无限期语义，避免上线时突然释放；它们下次 release 后重新 acquire 才进入 TTL 管理。

回退应用代码时可保留新增表和字段。旧测试账号代码会忽略 TTL 字段，但已处于 `in_use` 的账号需要继续人工释放。

## 6. 自动化验证

`tests/test_resource_leases_api.py`：

- 多资源全有或全无；
- AT-08：Flow12 持有 Unity 时，低优先级资格验证冲突且不修改 Run；
- 幂等重放；
- group 级续租和释放；
- AT-09：TTL 到期后新 owner 获取资源，旧租约和 expired 事件保留；
- `account:*` 被引导回测试号 Skill。

`tests/test_test_account_leases_api.py`：

- 旧 Skill acquire/password/release 契约不变；
- 账号排他领用；
- keepalive 延期和 Workflow 追溯；
- 超时自动回收并记录 expire；
- 过期租约不能复活；
- 非法 Workflow Run 不会占用账号。

## 7. 下一阶段

P0-F 建议实现 `entity_relations` 的幂等批量写入与双向查询，把候选、来源 commit、资格验证 Run、正式 testcase、使用该用例的 Flow Run 串成可查询链路，推进 AT-11。
