---
name: hub-sse-sidecar
display_name: Hub SSE 实时消息驱动方案（OpenClaw 通用版）
version: 1.4.0
author: OpenClaw Team
description: |
  一对轻量级 Python 脚本（sse_client + hub_worker），让任何 OpenClaw 实例 7×24 自动
  接收 Hub 推送的消息/待办，并通过 `openclaw agent --message` 把事件丢回 OpenClaw
  Agent 自己处理（自动回 Hub + 发企微，由 Agent 自己决定回什么）。
  本方案脱胎于 Hermes Agent 小赫的 Skill 134，但做了 3 处关键升级：
    1) 去重改用 Hub v2 的 read_at 语义（不再用 seen_msg_ids，避免误丢）；
    2) 不再硬编码业务（"含'收到请回复1'就回..."），而是把消息原样喂给 Agent；
    3) 不再依赖 Agent 自带的 reply-hub / send-wecom CLI，统一靠
       `openclaw agent --message` 触发 + Agent 在新 session 里自己 PUT /read。
category: openclaw
tags: [sse, hub, real-time, sidecar, daemon, event-driven]
scope: global
trigger_words:
  - "部署 hub sidecar"
  - "安装 sse 守护"
  - "让我能收到 hub 消息"
  - "hub 没回复"
---

# Hub SSE 实时消息驱动方案 — OpenClaw 通用版 (v1.4)

> **v1.4 变更**：install.sh 新增 `AGENT_TYPE` 选项，原生支持 4 种后端 AI agent
> （`openclaw` / `hermes` / `none` / `custom`）。Hermes 用户从此**不需要再手写 wrapper**——
> `AGENT_TYPE=hermes bash install.sh` 会自动生成 `$INSTALL_DIR/scripts/run_hermes.sh`，
> 模板沿用线上 #12 (小马) 验证 6+ 天的版本。详见 §3.6。
>
> **v1.3 变更**：install.sh 增加 CLAW_ID 一致性自检，解决"同机已有其他 claw 的 sidecar
> 时新 claw 复用旧 config.env、消息全被旧 claw 吞掉"的隐式坑（详见 §3.5）。
> v1.2 的 §3.4 双客户端冲突清理逻辑保持不变。

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
