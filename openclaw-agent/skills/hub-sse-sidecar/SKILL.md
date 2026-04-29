---
name: hub-sse-sidecar
display_name: Hub SSE 实时消息驱动方案（OpenClaw 通用版）
version: 2.1.0
author: OpenClaw Team
description: |
  让任何 OpenClaw 实例 7×24 自动接收 Hub 推送的消息/待办的 sidecar。
  v2 是单文件 Python 守护进程（sidecar_v2.py，~330 行），事件驱动、显式状态机、
  配置中心化（运行时参数全部从 Hub 拉），不再有任何"伪造兜底"行为：
    - 收到 message → PUT /processing → 调 LLM → PUT /done 或 PUT /failed
    - todo 由 LLM 自己跑全流程并 complete + 发企微，sidecar 不再 force_complete
    - 兜底全部交给 Hub 的 timeout_watcher（5 分钟超时告警 owner）
  v1（sse_client + hub_worker 双脚本）作为兼容文档保留，新部署一律走 v2。
category: openclaw
tags: [sse, hub, real-time, sidecar, daemon, event-driven]
scope: global
trigger_words:
  - "部署 hub sidecar"
  - "安装 sse 守护"
  - "让我能收到 hub 消息"
  - "hub 没回复"
  - "sidecar v2"
---

# Hub SSE 实时消息驱动方案 — OpenClaw 通用版 (v2.1)

## 0. v2 是什么 — 一分钟读完

**新部署直接看这一节，跳过 §3 以下的 v1 历史包袋。**

### 0.1 一行命令安装

```bash
HUB_URL=http://9.134.11.169:8088 \
CLAW_ID=<你的 claw id> \
CLAW_TOKEN=<注册 claw 时 Hub 返回的明文 token> \
bash install_v2.sh
```

脚本自动：
1. 探测 `openclaw` 二进制（找不到时支持 `AGENT_TYPE=hermes HERMES_HOME=/path` 切换）
2. 部署 `sidecar_v2.py` 到 `~/.qclaw/skills/hub-sse-sidecar/`
3. 写 `sidecar.env`（HUB_URL / CLAW_ID / CLAW_TOKEN，权限 600）
4. 自检：调 `GET /api/openclaws/<id>/sidecar-config` 验证 token + 注册首次配置
5. 注册 systemd 服务（系统级或用户级自动选择），`Restart=always`

### 0.2 v2 vs v1 关键差异

| 维度 | v1（hub_worker.py） | v2（sidecar_v2.py） |
|---|---|---|
| 脚本数 | 2（sse_client + hub_worker） | 1（sidecar_v2.py） |
| 中转方式 | 文件队列 task_queue.jsonl | 直接 SSE → 线程 → subprocess |
| 1.5s 自动 mark read 兜底 | **有**（worker 60s 后用空 reply PUT /read） | **没有** — 只有 LLM 真返回才 `PUT /done` |
| LLM 失败的处理 | 静默重试或丢弃 | 显式 `PUT /messages/<id>/failed`，body 含 `failed_reason` |
| todo 处理 | worker 可能 force_complete | LLM 自己跑流程，没回 complete 就让 Hub watcher 告警 |
| 配置 | 本地 sidecar.env 手改 | Hub `claw_sidecar_configs` 表，60s 自动拉新版本 |
| 运维入口 | `pgrep -af`、改 .env 重启 | `systemctl status hub-sse-sidecar-v2` |

### 0.3 v2 必需的 Hub 端接口（已上线）

部署 sidecar v2 之前，Hub 必须 **≥ B+ 通信稳定化版本**，包含这些接口：

| 接口 | 用途 |
|---|---|
| `GET /api/openclaws/<id>/sidecar-config` | sidecar 启动 / 60s 拉一次配置 + 心跳 |
| `PUT /api/openclaws/<id>/messages/<msg_id>/processing` | 开始调 LLM |
| `PUT /api/openclaws/<id>/messages/<msg_id>/done` | LLM 处理完成（body 可选 `llm_response`） |
| `PUT /api/openclaws/<id>/messages/<msg_id>/failed` | LLM 失败（body 必填 `failed_reason`） |
| `POST /api/openclaws/<id>/todos/<todo_id>/complete` | 待办完成（body 含 `notified=true` 表示 agent 已自己发企微） |
| `POST /api/openclaws/<id>/messages` | **claw → Hub**：主动发消息到通信中心（v2.1+） |
| `POST /api/openclaws/<id>/send-to-claw` | **claw → claw**：给其他 OpenClaw 发消息（v2.1+） |

### 0.3a sidecar 主动通信能力（v2.1+）

sidecar_v2.py 内置两个主动发消息函数，供 LLM 或脚本直接调用：

```python
# 1. OpenClaw → Hub：向通信中心发消息（如启动汇报、状态通知）
post_message_to_hub(content="sidecar 已启动", msg_type="system")

# 2. OpenClaw → OpenClaw：给指定 claw 发消息
send_to_claw(target_claw_ids=[1, 2], content="你好", msg_type="text")
```

- `msg_type` 可选：`text` | `chat` | `task_delegate` | `knowledge_share` | `system`
- 启动成功后 sidecar 会自动调用 `post_message_to_hub` 发一条系统汇报消息
- 这些函数使用 `CLAW_TOKEN` 自动认证，不需要额外配置

### 0.4 出问题怎么排查（v2）

```bash
# 1. 看服务在不在
systemctl --user status hub-sse-sidecar-v2          # 或 sudo systemctl status
# 2. 看日志
journalctl --user -u hub-sse-sidecar-v2 -f -n 200
# 3. 手动跑一次确认 token / Hub 连通
set -a && source ~/.qclaw/skills/hub-sse-sidecar/sidecar.env && set +a
python3 ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py
# 4. 看 Hub 端是否真收到状态变更
mysql> SELECT id, status, processing_at, done_at, failed_reason FROM claw_messages WHERE claw_id=<id> ORDER BY id DESC LIMIT 10;
# 5. 查 SSE 连接数（>1 就有问题，详见 §0.6）
pgrep -af sidecar_v2.py | wc -l
# 6. 查 to_claw 消息是否堆积（delivered 超过 24h 的属于异常，详见 §0.7）
mysql> SELECT COUNT(*) FROM claw_messages WHERE claw_id=<id> AND direction='to_claw' AND status='delivered' AND created_at < DATE_SUB(NOW(), INTERVAL 24 HOUR);
```

**常见症状**：

| 症状 | 原因 | 处理 |
|---|---|---|
| 日志一直 `[config] 拉取失败 code=0` | Hub 不可达 / 防火墙 | 检查 HUB_URL，curl 一次 `/api/openclaws/<id>/sidecar-config` |
| `code=401/403` | token 无效 | 到 Hub Web 后台重置 token 后重新跑 install_v2.sh |
| 日志反复 `LLM 处理失败 timeout` | LLM 调用真的超时 | 改 Hub 配置中心 `agent_timeout`（默认 300s），sidecar 60s 自动拉新值 |
| message 状态卡 `processing` | sidecar 进程被 kill 或机器重启 | systemd 会自动重启；老的卡住消息会被 Hub timeout_watcher 5 分钟后标 failed |
| `pgrep -af sidecar_v2.py` 结果 > 1 | 多实例并存（nohup + systemd 等） | 杀掉多余进程，统一用 systemd 管理（详见 §0.6） |
| Hub 端 `QueuePool limit reached` | SSE 长连接占满连接池 | 重启 openclaw-web + 排查多连接源（§0.6），Hub 端调大 POOL_SIZE |
| to_claw 消息长期卡 `delivered` | sidecar 没消费到消息 | 检查 sidecar 是否在运行 + SSE 是否连通（§0.7） |

### 0.5 从 v1 升级到 v2 — 老进程清理（重要！）

**历史教训**：以往升级最容易出的问题是"老进程没杀干净"——v1 的 `sse_client.py` + `hub_worker.py`
和 v2 的 `sidecar_v2.py` 同时在跑，结果 SSE 连接抢消息、双 worker 重复 LLM 调用、token 双倍消耗。

`install_v2.sh` 已经在 **Step 0** 自动调 `scripts/cleanup_v1.sh --apply`，正常情况下零额外操作。

#### 0.5.1 已装老 sidecar 的 claw 想"只清不升"（不动 #135 skill）

每台 claw 主机 ssh 进去，**一条命令搞定**（脚本由 Hub static 直接 serve，不需要本地有 skill 文件）：

```bash
# 1. 先 dry-run（看会清什么，不动手）
curl -fsSL http://9.134.11.169:8088/static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh | bash

# 2. 真清理（杀进程 + 删 systemd unit + 备份 config.env）
curl -fsSL http://9.134.11.169:8088/static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh | bash -s -- --apply

# 3. 终极清理（连 ~/.openclaw-sidecar 整个目录都 rm -rf，不留备份）
curl -fsSL http://9.134.11.169:8088/static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh | bash -s -- --apply --purge
```

#### 0.5.2 已装本 skill 的 claw（本地已有脚本副本）

```bash
bash ~/.qclaw/skills/hub-sse-sidecar/scripts/cleanup_v1.sh                     # dry-run
bash ~/.qclaw/skills/hub-sse-sidecar/scripts/cleanup_v1.sh --apply             # 清进程，留备份
bash ~/.qclaw/skills/hub-sse-sidecar/scripts/cleanup_v1.sh --apply --purge     # 全删
```

#### 0.5.3 一键升级到 v2（清 v1 + 装 v2 + 起 systemd 一气呵成）

```bash
HUB_URL=http://9.134.11.169:8088 \
CLAW_ID=<你的 claw id> \
CLAW_TOKEN=<明文 token> \
bash <(curl -fsSL http://9.134.11.169:8088/static/skills/hub-sse-sidecar-v2/install_v2.sh)
```

> ⚠️ 注意：`bash <(...)` 进程替换语法需要 Bash 4+；老的 sh / dash 不支持，改用：
> ```bash
> curl -fsSL .../install_v2.sh -o /tmp/install_v2.sh && \
>   HUB_URL=... CLAW_ID=... CLAW_TOKEN=... bash /tmp/install_v2.sh
> ```

**清理脚本会扫的所有"v1 指纹"**（这是历史踩坑积累的清单）：

| 类别 | 痕迹 |
|---|---|
| 进程 | `sse_client.py`、`sse_client_fixed.py`、`hub_worker.py`、`manager-hub/scripts/sseclient.py`（最老一代） |
| systemd 系统级 | `/etc/systemd/system/openclaw-sidecar.service`、`/etc/systemd/system/qclaw-sidecar.service` |
| systemd 用户级 | `~/.config/systemd/user/openclaw-sidecar.service` |
| 安装目录 | `~/.openclaw-sidecar/`、`~/.openclaw-sidecar-claw<ID>/`（多 claw 同机模式） |
| 关键文件 | `config.env`（含旧 CLAW_ID/TOKEN）、`logs/sse_client.pid`、`logs/worker.lock`、`logs/task_queue.jsonl` |
| 残留脚本 | `~/.qclaw/skills/hub-sse-sidecar/scripts/sse_client.py`、`hub_worker.py` |
| crontab（仅警告不删） | 当前用户 crontab、`/etc/cron.d/*` 含 `sse_client/hub_worker/hub-sse-sidecar` 的条目 |

**杀不掉怎么办**：cleanup 脚本退出码 = 2 时说明杀完又被外部守护拉起了（最常见是 cron 或 systemd
unit 不在我们扫描清单里）。脚本会打印每个残留进程的 `parent pid` + `cmd`，直接顺藤摸瓜：

```bash
# 找父进程
ps -o ppid= -p <pid>
# 父 pid 是 1 → systemd 在拉，systemctl list-units --all | grep -i sidecar 找出来
# 父 pid 是 cron pid → 当前用户 crontab -l 或 sudo cat /etc/crontab
```

**保护机制**：cleanup 脚本**永远不会动 v2 的进程/文件**——它对 `sidecar_v2.py` 字符串和
`hub-sse-sidecar-v2.service` 服务名做了黑白名单过滤，不会误伤新版。

### 0.6 多 SSE 连接防范（v2.1 新增 — 小天实战踩坑）

> ⚠️ **这是目前最大的资源浪费源**：小天 claw_id=6 曾同时存在 5-6 个 SSE 长连接
> （loop 序号 271/2281/9451/12031/14491 同时活跃），每个连接都占用 Hub 端一个线程
> + 一个 SQLAlchemy 连接池位置，严重时可导致 Hub 端 `QueuePool limit reached` 全站 500。

**多连接的常见成因**：

| 原因 | 表现 | 解法 |
|---|---|---|
| sidecar 进程被 kill -9 或机器突然重启，旧 SSE 连接在 Hub 端未关闭 | Hub 日志看到同一 claw_id 多个 `/events` 长连接 | Hub 端需要实现 **连接排他**：同一 claw_id 新连接到来时，服务端主动关闭旧连接 |
| systemd Restart=always + sidecar 启动崩溃循环 | 每次重启创建新连接，旧连接因 TCP keepalive 未生效仍挂着 | 在 systemd unit 加 `RestartSec=30`（至少 30s），给旧连接超时关闭的窗口 |
| 多种启动方式并存（nohup + systemd + cron 都在拉） | 多个 sidecar 实例同时运行 | 统一用 systemd，禁止 cron + nohup 混用 |
| v1 进程未清理就装了 v2 | v1 的 sse_client.py 和 v2 的 sidecar_v2.py 同时连 SSE | 先跑 `cleanup_v1.sh --apply`（见 §0.5） |

**sidecar v2 自身的防范措施**（已内置）：

1. **启动时 PID 文件互斥**：写 `sidecar.pid`，启动前检查旧 PID 是否存活，存活则退出
2. **SSE 连接带 `X-Sidecar-Instance-Id` 头**：Hub 端可据此做连接排他（需 Hub 支持）
3. **断线重连退避**：指数退避 5s→10s→20s→30s→60s，避免瞬间创建大量连接

**排查命令**：

```bash
# 查当前 claw 有几个 SSE 连接（Hub 端）
# 方法 1：看 Hub 日志
journalctl -u openclaw-web --no-pager -n 500 | grep "claw_id=6" | grep "events" | tail -20

# 方法 2：直接数 sidecar 进程（claw 本机）
pgrep -af sidecar_v2.py | wc -l    # 应该是 1，>1 就有问题

# 方法 3：Hub 端查 SSE 连接数（如果有实现 /api/openclaws/<id>/sse-connections 接口）
curl -s http://HUB_URL/api/openclaws/6/sse-connections
```

**Hub 端推荐改进**（尚未实现）：

- 同一 claw_id 的 SSE 连接做排他：新连接到来时，向旧连接发 `event: replaced` 后关闭
- SSE 端点加 `idle_timeout`（如 10 分钟无事件也无心跳则服务端断开）
- Dashboard 展示每个 claw 的活跃 SSE 连接数

### 0.7 delivered 消息堆积清理（v2.1 新增）

> v2 架构中，`from_claw`（claw 主动发出的消息，如求助、日报通知）永远停在 `delivered`
> 状态，不会被任何人 processing/done。这是 **设计如此**，不是 bug。
> 但 `to_claw` 方向的消息如果长期卡在 `delivered`，说明 sidecar 没有正确消费。

**消息方向与状态对照**：

| 方向 | 正常终态 | 异常 |
|---|---|---|
| `to_claw`（Hub→Agent） | `done`（处理成功） 或 `failed`（处理失败） | 长期卡 `delivered` = sidecar 没收到/没处理 |
| `from_claw`（Agent→Hub） | `delivered`（已投递） | — 本身就是终态 |

**清理建议**（管理员操作）：

```sql
-- 查看超过 24h 仍为 delivered 的 to_claw 消息（异常）
SELECT id, claw_id, LEFT(content,50), status, created_at
FROM claw_messages
WHERE direction='to_claw' AND status='delivered'
  AND created_at < DATE_SUB(NOW(), INTERVAL 24 HOUR)
ORDER BY id DESC LIMIT 20;

-- 对于确认不需要重处理的消息，可手动标记为 failed
UPDATE claw_messages SET status='failed', failed_reason='manual cleanup: stale delivered'
WHERE direction='to_claw' AND status='delivered'
  AND created_at < DATE_SUB(NOW(), INTERVAL 48 HOUR);
```

### 0.8 待办闭环校验（v2.1 新增 — 小天实战验证通过）

> 小天（claw_id=6）最近的待办处理已经完全闭环，每个待办都走了
> `complete + notified=true (agent_self)` 全流程。以下是已验证的闭环标准。

**待办完整闭环 = 4 步全做**：

```
1. LLM 处理任务 → 产出结果
2. POST /api/openclaws/<id>/todos/<todo_id>/complete  (body: {notified: true, result_summary: "..."})
3. Hub 端 claw_todo_logs 记录 completed_at + notified_at + notified_strategy=agent_self
4. 发企微通知给 owner（可选但推荐）
```

**sidecar 的角色**：sidecar **不参与** todo 闭环流程——它只负责收到 `todos_pending` 事件后
唤醒 LLM，LLM 自己调 Hub API 完成闭环。如果 LLM 没有 complete，Hub 的
`timeout_watcher` 会在 5 分钟后告警 owner。

**验证待办是否正确闭环**：

```bash
# 查看最近的 todo_logs（替换 claw_id）
mysql> SELECT l.id, l.todo_id, LEFT(t.title,30), l.status, l.completed_at, 
       l.notified_at, l.notified_strategy, l.result_summary IS NOT NULL as has_summary
       FROM claw_todo_logs l LEFT JOIN claw_todos t ON l.todo_id=t.id 
       WHERE l.openclaw_id=6 ORDER BY l.id DESC LIMIT 10;

# 正确的闭环记录应该是：
# status=submitted, completed_at 有值, notified_at 有值, notified_strategy=agent_self
```

**常见闭环失败原因**：

| 症状 | 原因 | 修法 |
|---|---|---|
| todo 一直 pending，没有 submitted | LLM 没调 complete API | 检查 Agent 的 prompt 是否包含 hub-connect skill 的 todo complete 指令 |
| submitted 但 notified_strategy 为空 | LLM 调了 complete 但没传 `notified=true` | 更新 prompt，明确要求 `{notified: true}` |
| completed_at 有值但 notified_at 为空 | 企微通知发送失败 | 检查 Agent 的企微通道配置 |

---

---

# 以下为 v1 兼容文档（保留原文，新部署不必读）


## 1. 这个 SKILL 解决什么

让 OpenClaw 实例满足 Hub 系统的核心 SLA：

- **机器开着就能收消息**：不依赖 Cursor / CodeBuddy / Claude Code 等 host 是否在前台。
- **待办立刻通知**：Hub 上新建 todo / 卡片上新建 todo → 下次心跳就到本地。
- **Hub 上能直接看到 AI 回复**：聊天消息的回复出现在 Hub 通信中心对话框里。
- **企微能收到通知**：进度同步到用户企微（Agent 自己用它已有的通道发）。

## 2. 架构（与小赫 v1.2 同骨架）

```
                Hub SSE
                   ↓
   ┌──────── sse_client.py (nohup 守护) ────────┐
   │ - 保 SSE 长连接 + heartbeat watchdog         │
   │ - 收到 message/todos_pending → 写队列文件   │
   │ - 唤醒 hub_worker.py（带 worker.lock 防并发）│
   └─────────────────┬───────────────────────────┘
                     │
              task_queue.jsonl
                     │
   ┌──────── hub_worker.py (按需启动) ────────┐
   │ - 读队列                                   │
   │ - 把每条任务构造成 prompt                  │
   │ - subprocess 调:                           │
   │     openclaw agent --message "<prompt>"    │
   │ - Agent 在新 session 里：                  │
   │     · 用自己的 LLM + skills 处理消息       │
   │     · PUT /messages/<id>/read?reply=...    │
   │     · 通过 --channel wecom 发企微通知      │
   └────────────────────────────────────────────┘
```

**为什么这么分层**：

- sidecar 极轻量，**不调 LLM、不写业务逻辑**，只做"路由 + 唤醒"。
- 业务能力 100% 复用 OpenClaw Agent 自身（已有的 skills / rules / LLM 配置）。
- 每条消息一个独立 session 处理，互不干扰，session 结束即释放资源。

## 3. ⚠️ 关键约定（必读 — 跟小赫方案的区别）

### 3.1 去重靠 Hub `read_at`，不要本地 `seen_msg_ids`

Hub 从 v2 起对每条 `to_claw` 消息有 `read_at` 字段，SSE 重连**只重推
`read_at IS NULL` 的消息**。所以：

- **Agent 处理完一条消息，必须调 `PUT /api/openclaws/<claw_id>/messages/<msg_id>/read`**
  （可附带 `{"reply": "..."}` 一次完成回复 + 标记已读）。
- 我们的 `hub_worker.py` 在 prompt 里**明确告诉 Agent 这件事**。
- worker **兜底**：如果 Agent 在 60s 内没自己 PUT /read，worker 会用空 reply 调 PUT /read
  把消息标记为"已派发但 Agent 未回复"，避免下次 SSE 重连无限重推。

> 为什么不抄小赫的 `seen_msg_ids.json`：那个文件先去重再处理，
> 同一回合内的新消息会被误丢（用户在 Q1 实战中已踩坑确认）。

### 3.2 不要外部 watchdog 监控 sse_client

> ⚠️ **绝对不要**用 crontab + shell 脚本（如 `sse_watchdog.sh`）监控 `sse_client.py`。
> sse_client.py 内部已经有 heartbeat watchdog（`HEARTBEAT_TIMEOUT`，默认 120s）
> + 指数退避重连。
> 外部脚本无法判断 SSE 真实连接状态（无新事件时日志不更新），会频繁误杀进程。
> 这是小赫踩过的坑，原话："这很可能是导致其他 OpenClaw 失败的根本原因"。

### 3.3 业务零硬编码

worker **不会**有 `if "收到请回复1" in content:` 这种逻辑。
所有 chat 消息都被原样塞进 prompt 喂给 Agent，由 Agent 自己决定回什么——
这才是 Agent 该做的事。

### 3.4 禁止双 SSE 客户端并存（重点）

> ⚠️ `manager-hub` 旧版 `sse_client.py` 与本 skill 不能并存。并存会导致消息抢占、
> 延迟、掉线后“看似在线实则不消费”等问题。

安装/重启前先执行：

```bash
pkill -f "manager-hub/scripts/sse_client.py" || true
pkill -f "manager-hub/scripts/sseclient.py" || true
pgrep -af "manager-hub/scripts/sse_client.py|manager-hub/scripts/sseclient.py" || echo "manager-hub sse_client 已清理"
```

### 3.5 同机已有其他 claw 的 sidecar（同机多 claw 场景）

> ⚠️ 一台机器以前给 **claw A** 装过 sidecar，现在又要给 **claw B** 装。
> 如果不处理 `~/.openclaw-sidecar/config.env`，新 sidecar 会**继续用 claw A 的 CLAW_ID 和 token** 连接 Hub，
> 消息/待办会全部投递到 claw A，claw B 永远收不到。
> 这是 v1.2 之前 install.sh "配置已存在则跳过" 的静默坑（用户实战已踩过两次）。

**v1.3 起 install.sh 自动检测并报错**——你不需要先 `rm`，直接跑 install.sh 就行：

| 检测结果 | install.sh 行为 |
|---|---|
| `config.env` 不存在 | 直接生成 |
| 已存在且 `CLAW_ID` 与本次一致 | 跳过（保留正在跑的 agent，不破坏 token 等手动调整） |
| 已存在但 `CLAW_ID` 与本次**不一致** | ❌ **明确报错退出**，提示三种修复方式 |

**遇到不一致报错时，三种修复方式（脚本会原样打印）**：

```bash
# 方式 A（推荐）— 让本 claw 接管，旧 claw 在本机的 sidecar 将停止工作
FORCE_RECONFIG=1 CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh
# 或
bash install.sh --reconfigure              # 等价 FORCE_RECONFIG=1
# 旧配置会自动备份为 ~/.openclaw-sidecar/config.env.bak.<timestamp>

# 方式 B — 完全卸载后重装
bash install.sh --uninstall
rm ~/.openclaw-sidecar/config.env
CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh

# 方式 C — 同机并行跑多个 claw 的 sidecar（进阶，不推荐；nohup 模式直接可用）
INSTALL_DIR=$HOME/.openclaw-sidecar-claw12 \
CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh

# 如果还要开机自启，systemd unit 也得换名（否则会和已有的 openclaw-sidecar.service 冲突）
INSTALL_DIR=$HOME/.openclaw-sidecar-claw12 \
SYSTEMD_UNIT_NAME=openclaw-sidecar-claw12.service \
CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh --systemd
```

> 如果你是 OpenClaw / Hermes Agent 在帮人部署，遇到这个报错**不要尝试用 `rm` 绕过**，
> 用 `--reconfigure` 才是正解（自动备份旧配置，便于事后追查）。

### 3.6 后端 AI agent 类型（AGENT_TYPE）

> v1.4 起原生支持 4 种后端 AI agent，不再需要手写 wrapper。

| AGENT_TYPE | install.sh 行为 | 适用场景 |
|---|---|---|
| `openclaw`（默认） | `OPENCLAW_BIN=openclaw`，要求 `openclaw agent --message` 在 PATH | 标准 OpenClaw CLI 用户 |
| `hermes` | 自动生成 `$INSTALL_DIR/scripts/run_hermes.sh` wrapper（模板=线上 #12 6+ 天验证版），`OPENCLAW_BIN` 指向它；启动时校验 `$HERMES_HOME/venv/bin/python` 与 `hermes_cli.main` 模块可用 | 小赫 / 任何 Hermes Agent 用户 |
| `none` | `OPENCLAW_BIN=/nonexistent/agent-disabled`，sidecar 收消息后调用会 `FileNotFoundError`，事件落到 inbox | MCP host 接管场景（如 Cursor 自己读 inbox） |
| `custom` | `OPENCLAW_BIN=$CUSTOM_AGENT_BIN`，自带 CLI 必须支持 `<bin> agent --message X --timeout N` | 其他 AI agent 形态 |

**示例**：

```bash
# 默认 openclaw（行为同 v1.3）
CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh

# Hermes（HERMES_HOME 默认 /root/hermes-agent）
AGENT_TYPE=hermes CLAW_ID=10 API_TOKEN=oc_tk_xxx bash install.sh

# Hermes 装在别处
AGENT_TYPE=hermes HERMES_HOME=/opt/hermes \
    CLAW_ID=10 API_TOKEN=oc_tk_xxx bash install.sh

# 命令行写法等价
bash install.sh --agent-type=hermes

# 仅写 inbox，不调 CLI
AGENT_TYPE=none CLAW_ID=12 API_TOKEN=oc_tk_xxx bash install.sh
```

**优先级**：用户显式传 `OPENCLAW_BIN=` 永远覆盖 AGENT_TYPE 自动推导，便于特殊场景手工指路径。

**切换 AGENT_TYPE 须 `--reconfigure`**：v1.3 的"已存在 config.env 且 CLAW_ID 一致 → 跳过"逻辑会**保留旧的 OPENCLAW_BIN**。如果你想从 `openclaw` 切到 `hermes`（或反过来），install.sh 检测到 OPENCLAW_BIN 不一致会打印 warn，但不会自动改写 config.env——必须加 `--reconfigure` 强制重写。

## 4. 部署步骤

### Step 1：创建目录

```bash
mkdir -p ~/.openclaw-sidecar/scripts
mkdir -p ~/.openclaw-sidecar/logs
```

### Step 2：拉取脚本

```bash
SKILL_BASE="http://9.134.11.169:8088/static/skills/hub-sse-sidecar"
curl -fsSL -o ~/.openclaw-sidecar/scripts/sse_client.py "$SKILL_BASE/scripts/sse_client.py"
curl -fsSL -o ~/.openclaw-sidecar/scripts/hub_worker.py "$SKILL_BASE/scripts/hub_worker.py"
chmod +x ~/.openclaw-sidecar/scripts/*.py
```

### Step 3：写配置文件

```bash
cat > ~/.openclaw-sidecar/config.env <<EOF
HUB_URL=http://9.134.11.169:8088
CLAW_ID=<你的 claw id，比如 6>
API_TOKEN=<你的 Hub API token，从 /registration-skill 拿>

# Agent 调用相关
OPENCLAW_BIN=openclaw                  # 默认 openclaw 在 PATH 里
AGENT_NAME=main                        # openclaw agent --agent <name>
WECOM_CHANNEL=wecom                    # openclaw agent --channel <name>，留空则不指定
AGENT_TIMEOUT=120                      # 单条消息处理超时（秒）
HEARTBEAT_TIMEOUT=120                  # SSE 心跳超时（秒，建议 90-180）
TODOS_VERIFY_DELAY=3                   # 待办回写后校验等待秒数
TODOS_FORCE_COMPLETE_FALLBACK=1        # 1=二次催办后仍未回写则兜底 complete
EOF
chmod 600 ~/.openclaw-sidecar/config.env
```

### Step 4：（可选）确认 `openclaw agent` 命令可用

```bash
openclaw agent --help | head -20
# 期望看到 --message / --agent / --channel / --timeout 这些参数
```

如果没装 `openclaw` CLI 或参数不一样，回到 §6"故障排查"。

### Step 5：启动守护进程

```bash
nohup python3 ~/.openclaw-sidecar/scripts/sse_client.py \
  > ~/.openclaw-sidecar/logs/sse_client.log 2>&1 &

# 记录 PID
echo $! > ~/.openclaw-sidecar/logs/sse_client.pid
```

### Step 6：自检

```bash
# 1) 进程在跑
pgrep -f sse_client.py

# 2) SSE 已连上
tail -f ~/.openclaw-sidecar/logs/sse_client.log
# 期望看到: "✅ 已连接 Hub" + 后续每 30s 一次 heartbeat

# 3) 让 Hub 给你发条消息（在 Web 通信中心发）
#    应该 1-2s 内看到 "📨 事件[message]" 入队 + worker 启动 +
#    Hub UI 看到 from_claw 回复
```

### Step 7：（可选）开机自启 — 用 systemd

```ini
# /etc/systemd/system/openclaw-sidecar.service
[Unit]
Description=OpenClaw Hub SSE Sidecar
After=network.target

[Service]
Type=simple
EnvironmentFile=/root/.openclaw-sidecar/config.env
ExecStart=/usr/bin/python3 /root/.openclaw-sidecar/scripts/sse_client.py
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now openclaw-sidecar
```

> systemd 比 nohup 更稳——它内置进程监控，不需要任何外部 watchdog。

## 5. 工作流验证

| 用户行为 | 预期链路 | 看哪儿确认 |
|---|---|---|
| Hub 通信中心发"测试1" | sse_client 收到 → worker 调 openclaw agent → Agent 回复 → Hub UI 出现回复气泡 | `~/.openclaw-sidecar/logs/sse_client.log` + `hub_worker.log` + Hub UI |
| Hub 卡片上新建一条 todo | sse_client 收到 todos_pending → worker 调 openclaw agent → Agent 处理 → 完成时通过 hub_complete_todo 闭环 | `hub_worker.log` + Hub 卡片 todo 状态 |
| 拔网线 60s 再插上 | sse_client watchdog（`HEARTBEAT_TIMEOUT`）主动断流 → 指数退避 5s/10s/20s/30s/60s 重连 | `sse_client.log` 看 "watchdog: ... 无心跳" + "SSE EOF（连接已关闭）" + "✅ 已连接" |

## 5.1 企微通知规范（定版）

> 本规范用于统一“待办进展通知”质量，避免漏发、重复发、模板化机械发。

### 触发范围

- **必须通知**：`todos` 任务进入“已完成提交”或“阻塞需人工介入”时。
- **可选通知**：`chat` 消息处理完成后，按 Agent 自身规则决定是否通知。
- **禁止通知**：重复状态、无增量信息、仅内部重试过程。

### 最低内容要求

每条企微通知至少包含：

1. 任务标识（`todo_id` 或可追踪标题）
2. 当前状态（完成/阻塞）
3. 结果摘要（1 句）
4. 下一步动作（若阻塞必须写）

建议格式（示例）：

```text
[OpenClaw][{{AGENT_NAME}}] todo#{{TODO_ID}} 已完成
结果：{{SUMMARY}}
下一步：{{NEXT_ACTION}}
```

### 执行约束

- **先 Hub 闭环，再企微通知**：先确保 `/read` 或 `/todos/<id>/complete` 成功，再发企微。
- **失败不反向阻塞 Hub 闭环**：企微发送失败时，只记录日志，不回滚 Hub 闭环。
- **同状态节流**：同一 `todo_id`、同一状态短时间内避免重复推送。

### Prompt 强化建议（给 Agent）

在 `todos` prompt 里保留如下硬性要求（可复用）：

```text
【必做】完成待办提交后，立即给用户发送一条企微进展通知（包含: todo_id/状态/结果摘要/下一步）。
如果企微发送失败，记录失败原因，但不要影响 Hub 闭环提交。
```

## 6. 故障排查

| 现象 | 原因 | 修法 |
|---|---|---|
| `pgrep -f sse_client.py` 没结果 | 守护没起来 | 看 `sse_client.log` 末尾报错；最常见是 TOKEN 无效 / HUB_URL 不通 |
| log 里反复 "❌ 连接异常: HTTP 401" | Token 失效 | 重新跑 `/registration-skill` 拿新 token，更新 `config.env` |
| 进程在跑但长时间收不到新事件 | 双客户端冲突（manager-hub + sidecar）或历史僵尸连接 | 先执行 `pkill -f "manager-hub/scripts/sse_client.py"` 和 `pkill -f "manager-hub/scripts/sseclient.py"`，确保仅保留 `~/.openclaw-sidecar/scripts/sse_client.py` 单实例 |
| log 里有 "📨 事件" 但 Hub UI 看不到回复 | worker 调 `openclaw agent` 失败 | 看 `hub_worker.log`；最常见是 `openclaw` 不在 PATH，或 --agent 名字错 |
| 同一条消息被反复推 | Agent 没调 PUT /read | 看 worker 是否走到了"60s 兜底" 分支；如果走到，要么 Agent 没装 hub-connect skill，要么 prompt 没被它认真读 |
| worker 启动飞快但 Agent 处理太慢，下条消息又入队了 | 正常，worker.lock 会让新 task 等下一轮 | 不需要修；如果 backlog 长期堆积，加大 `AGENT_TIMEOUT` 或起多个 worker（高级） |

## 7. 文件清单

```
~/.openclaw-sidecar/
├── config.env                 # 你的配置（chmod 600）
├── scripts/
│   ├── sse_client.py          # SSE 守护进程
│   └── hub_worker.py          # 任务处理工作进程
└── logs/
    ├── sse_client.log         # 守护日志
    ├── sse_client.pid         # 守护 PID
    ├── hub_worker.log         # 工作进程日志
    ├── worker.lock            # 防并发锁（含当前 worker PID）
    └── task_queue.jsonl       # 任务队列（worker 处理完即清空）
```

## 8. 运行边界（重要）

当前标准方案是 **sidecar-only**：

- 只依赖 `sse_client.py + hub_worker.py + openclaw agent --message`
- 不要求任何前台宿主配置
- 不需要额外配置文件也能完成消息/待办闭环

如果现场还残留 MCP 或旧 `manager-hub` 链路，请优先清理旧链路，保证 sidecar 单实例运行。

## 9. 致谢

骨架抄自 Hermes Agent 小赫的 [Skill 134 hub-sse-message-driven](http://9.134.11.169:8088/skills/134)。
本 skill 在他的 v1.2 基础上做了 §3 列出的 3 处升级，并吸收了龙虾王实战中的企微通知规范。
