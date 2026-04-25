---
name: hub-inbox
description: 当 openclaw-hub MCP server 不可用（host 不支持 MCP，或暂未安装）时的兜底协议——AI 在每回合开始时先扫 ~/.qclaw/inbox/pending/，读取 Hub 通过 SSE 推下来的待处理事件，处理完用 curl 直接回 Hub。
trigger_words:
  - "查看 inbox"
  - "看看有没有 hub 消息"
  - "处理 hub 待办"
  - "openclaw 回合开始"
---

# hub-inbox — Hub 消息文件协议（MCP fallback）

> **本 skill 在 host 已经成功安装 `openclaw-hub` MCP server 的环境下不需要使用**——MCP server 提供的 `hub_pending` / `hub_reply` 工具更直接、更可靠。
>
> 只在以下场景启用：
> - host 不支持 MCP（极端环境）
> - MCP server 进程崩溃/未启动
> - 调试/审计：想直接看 inbox 里有什么

## 触发约定（每回合开始时执行）

每次新回合开始（用户发来新一句话之前），**先执行 Step 1 看 inbox 是否有 pending 文件**。
如果有，按 urgency 优先级处理；处理完再回应用户当前的话。

---

## ⚠️ 重要：去重靠 Hub，不靠本地

Hub 端从 v2 开始引入"已读"语义（`read_at` 字段），SSE 重连时**只会重推 `read_at IS NULL` 的消息**。

这意味着：

- **AI 真实消费完一条 `source=message` 事件，必须调 `PUT /messages/<id>/read`**，
  否则下次 SSE 重连，Hub 仍会再次推送这条消息（你会看到同一条 inbox 文件再次出现）；
- **不要**依赖客户端本地 `seen_msg_ids.json` 之类的永久去重文件——那是旧实现的兜底，
  会因为"先去重再处理"导致同一回合内的新消息被误丢；
- 如果你确实想本地缓存去重，**只缓存最近 N 分钟**，不要永久。

对应到 Step 4：`mv` 到 processed/ 只是本地状态；**真正告诉 Hub "这条消费完了" 必须靠
`PUT /read`**。两步缺一不可。

---

## Step 1: 扫描 pending 目录

```bash
INBOX="$HOME/.qclaw/inbox/pending"
if [ -d "$INBOX" ]; then
  count=$(ls -1 "$INBOX" 2>/dev/null | wc -l)
  echo "pending events: $count"
  ls -1 "$INBOX" 2>/dev/null | head -20
fi
```

**文件名结构**：`<urgency>__<source>__<timestamp>__<inbox_id>.json`

- urgency：`interrupt | flexible | background | periodic | retry`
- source：`message | task | todos_pending | knowledge_updated`
- timestamp：UTC ISO 时间，冒号被替换为 `-`
- inbox_id：12 字符 hex，是回复时的引用键

---

## Step 2: 按 urgency 排序处理

按 **interrupt → flexible → background** 顺序处理。`periodic / retry` 一般是 background 优先级。

### 读取一条 pending 事件

```bash
FILE="$HOME/.qclaw/inbox/pending/<filename>"
cat "$FILE"
```

文件里一定包含：

```json
{
  "inboxId": "abc123def456",
  "source": "message",
  "urgency": "flexible",
  "payload": { "id": 100, "content": "...", "sender_name": "...", "msg_type": "text" },
  "receivedAt": "2026-04-18T03:21:45Z",
  "hubId": 100
}
```

---

## Step 3: 按 source 处理

### 3.1 `source = message` —— Hub 给 OpenClaw 的消息

AI 真实理解 `payload.content` → 生成回答 → 用以下命令回 Hub
（`PUT /read` 同时承担 **标记已读** 和 **回复** 两件事，**不调它 = Hub 会再推一遍这条消息**）：

```bash
HUB="$(grep -oP '(?<=hub_url: ).*' $HOME/.qclaw/agent.md)"
TOKEN="$(grep -oP '(?<=api_token: ).*' $HOME/.qclaw/agent.md)"
CLAW_ID="$(grep -oP '(?<=claw_id: ).*' $HOME/.qclaw/agent.md)"
MSG_ID="<payload.id>"
REPLY="<AI 真实回答>"

curl -s -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(printf '{"reply": %s}' "$(printf %s "$REPLY" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')")" \
  "$HUB/api/openclaws/$CLAW_ID/messages/$MSG_ID/read"
```

> ❌ **绝不要** 用写死的 "✅ 已转交 / 已收到" 这种罐头文本。这正是旧 sidecar 的错误做法，本 skill 存在的意义就是让 AI 真的回复。

### 3.2 `source = todos_pending` —— Hub 给 OpenClaw 的待办

按 `payload.title` 决定执行什么。完成后：

```bash
TODO_ID="<payload.id>"
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "<AI 完成简述>"}' \
  "$HUB/api/v1/openclaws/$CLAW_ID/todos/$TODO_ID/complete"
```

### 3.3 `source = task` —— 操作型任务

任务通常需要执行命令/改文件。完成后调：

```bash
TASK_ID="<payload.task_id>"
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "'"$TASK_ID"'", "status": "completed", "result": "<执行结果>"}' \
  "$HUB/api/openclaws/$CLAW_ID/poll"
```

### 3.4 `source = knowledge_updated` —— 仅记日志

通常是 background。读完移走即可，不需要回 Hub。

---

## Step 4: 标记已处理 —— 移到 processed/

每处理完一个文件（**无论 reply / complete / 仅记**），都要把它移到 processed 目录，避免下回合重复处理：

```bash
mv "$HOME/.qclaw/inbox/pending/<filename>" "$HOME/.qclaw/inbox/processed/"
```

如果回复内容比较长，建议同时落一份到 replies/：

```bash
echo "$REPLY" > "$HOME/.qclaw/inbox/replies/$(date -u +%Y%m%dT%H%M%SZ)__<inbox_id>.txt"
```

---

## Step 5: 处理完毕，再回应用户

inbox 处理完之后，再回到用户当前问题。**如果 inbox 里是 interrupt 级**，应该向用户主动说明："刚刚先处理了 Hub 推下来的紧急事项 X，已回复，现在回到你的问题..."

---

## 完整一次回合的伪代码

```bash
# 0) 读 agent.md 拿凭证
HUB=$(grep -oP '(?<=hub_url: ).*' $HOME/.qclaw/agent.md)
TOKEN=$(grep -oP '(?<=api_token: ).*' $HOME/.qclaw/agent.md)
CLAW_ID=$(grep -oP '(?<=claw_id: ).*' $HOME/.qclaw/agent.md)

# 1) 扫 pending
ls -1 "$HOME/.qclaw/inbox/pending" 2>/dev/null | sort | while read fn; do
  fp="$HOME/.qclaw/inbox/pending/$fn"
  # 2) 解析（让 AI 读 cat $fp 的输出）
  cat "$fp"
  # 3) AI 决策 → 4) curl 回 Hub → 5) mv 到 processed
done
```

---

## 与 MCP server 的关系

- 同一份 `~/.qclaw/inbox/pending/` 文件由 MCP server 的 `Inbox.writePending()` 和
  legacy sidecar 的 `_write_inbox_event()` **共同维护**——目录布局完全一致。
- 如果 MCP server 已经在跑，AI 用 `hub_pending` 工具也能看到一样的事件
  （MCP server 的内存队列 = inbox/pending 文件集合）。
- AI 通过 `hub_consume` 调用时，MCP server 自动 `mv` 文件到 processed。
  本 skill 的 `mv` 步骤等价于"手动消费"。

---

## 触发词

- "查看 inbox"
- "看看 hub 有没有新消息"
- "处理 hub 待办"
- "回合开始 inbox 同步"
- "MCP 没启动，手动看 inbox"
