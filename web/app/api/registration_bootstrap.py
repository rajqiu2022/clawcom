"""注册自部署 Bootstrap 生成器（sidecar-only v1.2）

为 ``GET /api/v1/openclaws/{claw_id}/registration-skill`` 生成"完整自部署"
Markdown 文档与对应的 MCP 配置片段。

本版本对齐 **DB Skill #135 hub-sse-sidecar v1.2**（2026-04-20 由龙虾王主导更新），
四条核心约定：

  1. 去重靠 Hub v2 的 ``read_at`` 语义，禁止本地 seen_msg_ids 永久去重；
  2. 不要给 sse_client 加外部 watchdog（cron / shell 监控会误杀，已内置 heartbeat）；
  3. 业务零硬编码，所有消息原样喂给 Agent，由 Agent 决定如何回；
  4. **禁止双 SSE 客户端并存**（manager-hub 旧脚本 + sidecar 同跑会消息抢占）。

注册流程是 sidecar-only：仅依赖 ``sse_client.py + hub_worker.py + openclaw agent
--message``，**不依赖** Cursor / Claude Code / CodeBuddy 等 MCP host 配置。
JSON 格式仍提供 ``mcp_config_snippets`` 字段以兼容历史 host 接入需求，但默认
markdown 不再贴 MCP 步骤。
"""

from __future__ import annotations

import json
import os
from typing import Any


# MCP server 安装根目录（在 **客户端机器** 上，仅 JSON 接口使用）
DEFAULT_MCP_INSTALL_DIR = os.environ.get(
    "OPENCLAW_HUB_MCP_INSTALL_DIR",
    "~/.qclaw/mcp",
)

# MCP server 入口路径
DEFAULT_MCP_ENTRY = os.environ.get(
    "OPENCLAW_HUB_MCP_PATH",
    f"{DEFAULT_MCP_INSTALL_DIR}/openclaw-hub/dist/index.js",
)

# MCP server tarball 下载 URL
DEFAULT_MCP_TARBALL_URL = os.environ.get(
    "OPENCLAW_HUB_MCP_TARBALL_URL",
    "http://9.134.11.169:8088/static/dist/openclaw-hub-mcp.tar.gz",
)

# 兼容老变量名（保留以免外部脚本崩）
DEFAULT_MCP_REPO = os.environ.get(
    "OPENCLAW_HUB_MCP_REPO",
    DEFAULT_MCP_TARBALL_URL,
)


def build_mcp_config_snippets(
    hub_url: str,
    claw_id: int,
    api_token: str,
    mcp_entry: str | None = None,
) -> dict[str, dict[str, Any]]:
    """为三种 host 各生成一个 mcp.json 片段（仅 JSON 格式接口使用）

    返回结构::

        {
            "cursor":     {"path": "~/.cursor/mcp.json",  "config": {...}},
            "claude":     {"path": "~/.claude.json",       "config": {...}},
            "codebuddy":  {"path": ".codebuddy/mcp.json",  "config": {...}},
        }
    """
    entry = mcp_entry or DEFAULT_MCP_ENTRY
    common = {
        "command": "node",
        "args": [entry],
        "env": {
            "HUB_URL": hub_url,
            "CLAW_ID": str(claw_id),
            "API_TOKEN": api_token,
        },
    }
    server_block = {"openclaw-hub": common}
    return {
        "cursor": {
            "path": "~/.cursor/mcp.json",
            "config": {"mcpServers": server_block},
        },
        "claude": {
            "path": "~/.claude.json",
            "config": {"mcpServers": server_block},
        },
        "codebuddy": {
            "path": ".codebuddy/mcp.json",
            "config": {"mcpServers": server_block},
        },
    }


def build_bootstrap_markdown(
    *,
    hub_url: str,
    claw_id: int,
    claw_name: str,
    role: str,
    project_name: str,
    api_token: str,
    skill_links: list[str] | None = None,
    rule_links: list[str] | None = None,
    # 以下参数已弃用（sidecar-only 不再写 MCP 步骤），保留仅为函数签名兼容
    mcp_entry: str | None = None,        # noqa: ARG001
    mcp_repo: str | None = None,          # noqa: ARG001
    mcp_tarball_url: str | None = None,   # noqa: ARG001
    mcp_install_dir: str | None = None,   # noqa: ARG001
) -> str:
    """生成 sidecar-only v1.2 注册自部署 Markdown 文档。

    内容对齐 DB Skill #135 hub-sse-sidecar v1.2。
    """
    skill_links = skill_links or []
    rule_links = rule_links or []
    skill_link_lines = "\n".join(f"- {u}" for u in skill_links) or "- (无)"
    rule_link_lines = "\n".join(f"- {u}" for u in rule_links) or "- (无)"

    return f"""# OpenClaw 自部署 — {claw_name}（sidecar-only v1.2）

> 本文档由 Hub 自动生成，已包含完整的连接凭证。
> **流程对齐 [Skill #135 hub-sse-sidecar v1.2]({hub_url}/skills/135)**，10 分钟内完成注册。
> 把整份文档交给你的 AI 客户端（Cursor / Claude Code / CodeBuddy / qclaw / Hermes / 龙虾王 …），
> 它会按顺序跑完所有步骤，最终让你能在 Web 通信中心给本 OpenClaw 发消息并得到 AI 真实回复。

---

## 身份信息（已由 Hub 填好）

| 字段 | 值 |
|---|---|
| Hub 地址 | `{hub_url}` |
| Claw ID | `{claw_id}` |
| Claw 名称 | `{claw_name}` |
| 角色 | `{role}` |
| 所属项目 | `{project_name}` |
| **API Token** | `{api_token}` |

> ⚠️ Token **只在本 URL 中返回一次**，关闭页面将不可恢复。AI 必须在 Step 1 把它写入 `~/.qclaw/agent.md` 并 `chmod 600`。

---

## ⚠️ Step 0：四条核心约定（开工前必读，对齐 Skill #135 §3）

### 0.1 去重靠 Hub `read_at`，**禁用**本地 seen_msg_ids

Hub v2 起每条 `to_claw` 消息有 `read_at` 字段，SSE 重连**只重推 `read_at IS NULL` 的消息**。所以：

- Agent 处理完一条消息**必须** `PUT /api/openclaws/{claw_id}/messages/<msg_id>/read`（可附 `{{"reply": "..."}}` 一次完成）
- **不要**在 `~/.qclaw/` 下放 `seen_msg_ids.json` 之类的永久去重文件，会因"先去重再处理"误丢同回合新消息
- MCP 模式下 `hub_reply` 工具会自动调 `/read`，不用关心；sidecar/inbox 模式必须显式调

### 0.2 **不要**给 sse_client 加外部 watchdog

`sse_client.py` 内部已有 heartbeat watchdog（`HEARTBEAT_TIMEOUT` 默认 120s）+ 指数退避重连。外部 cron / shell 脚本无法判断 SSE 真实连接状态（无新事件时日志不更新），**一定会误杀进程**。

### 0.3 业务零硬编码

worker **不允许**写 `if "收到请回复1" in content:` 这种条件分支。所有 chat 消息原样塞进 prompt 喂给 Agent，由 Agent 自己决定回什么。

### 0.4 **禁止**双 SSE 客户端并存（容易踩的雷）

`manager-hub` 旧版 `sse_client.py` 与 `~/.openclaw-sidecar/scripts/sse_client.py` **不能并存**。并存会导致消息抢占、延迟、掉线后"看似在线实则不消费"。安装 / 重启前**必须**先清理：

```bash
pkill -f "manager-hub/scripts/sse_client.py" 2>/dev/null || true
pkill -f "manager-hub/scripts/sseclient.py"  2>/dev/null || true
pgrep -af "manager-hub/scripts/(sse_client|sseclient)\\.py" \\
  || echo "✅ manager-hub 旧版 sse_client 已清理"
```

---

## Step 1：一次性自部署（推荐直接复制）

```bash
set -e
HUB="{hub_url}"
CLAW_ID="{claw_id}"
TOKEN="{api_token}"

# 1.1 写 agent.md（含 token，0600 权限）
mkdir -p "$HOME/.qclaw"
cat > "$HOME/.qclaw/agent.md" <<'AGENT_MD_EOF'
---
hub_url: {hub_url}
claw_id: {claw_id}
api_token: {api_token}
---

# {claw_name}

## 角色
{role}

## 所属项目
{project_name}
AGENT_MD_EOF
chmod 600 "$HOME/.qclaw/agent.md"
echo "✅ agent.md 已写入"

# 1.2 创建 inbox 骨架（MCP fallback / 审计兜底用）
mkdir -p "$HOME/.qclaw/inbox/pending" "$HOME/.qclaw/inbox/processed" "$HOME/.qclaw/inbox/replies"

# 1.3 拉取已分配的 skills 和 rules 到本地（含 hub-sse-sidecar 完整 SKILL）
mkdir -p "$HOME/.qclaw/skills" "$HOME/.qclaw/rules"
curl -s -H "Authorization: Bearer $TOKEN" \\
  "$HUB/api/v1/openclaws/$CLAW_ID/assigned-skills" \\
  | python3 - <<'SKILL_DOWNLOAD_EOF'
import json, os, sys, urllib.request
HUB   = os.environ.get('HUB',   '{hub_url}')
TOKEN = os.environ.get('TOKEN', '{api_token}')
def _get(url):
    req = urllib.request.Request(url, headers={{'Authorization': f'Bearer {{TOKEN}}'}})
    return urllib.request.urlopen(req, timeout=30)
data = json.load(sys.stdin)
for s in data.get('skills', []):
    name = s.get('name')
    sd = os.path.expanduser(f'~/.qclaw/skills/{{name}}')
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write(s.get('template_content') or '')
    sid = s.get('id')
    if sid:
        try:
            files = json.load(_get(f'{{HUB}}/api/v1/skills/{{sid}}/files')).get('files', [])
            for fd in files:
                fn = fd['filename']
                if fn == 'SKILL.md':
                    continue
                payload = _get(f'{{HUB}}/api/v1/skills/{{sid}}/files/{{fn}}').read()
                with open(os.path.join(sd, fn), 'wb') as f:
                    f.write(payload)
        except Exception as e:
            print(f'  pack skip {{name}}: {{e}}', file=sys.stderr)
    print(f'✅ skill: {{name}}')
SKILL_DOWNLOAD_EOF

curl -s -H "Authorization: Bearer $TOKEN" \\
  "$HUB/api/v1/openclaws/$CLAW_ID/assigned-rules" \\
  | python3 - <<'RULE_DOWNLOAD_EOF'
import json, os, sys
data = json.load(sys.stdin)
for r in data.get('rules', []):
    name = r.get('name')
    p = os.path.expanduser(f'~/.qclaw/rules/{{name}}.md')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(r.get('content_template') or '')
    print(f'✅ rule: {{name}}')
RULE_DOWNLOAD_EOF

# 1.4 清理冲突的旧 SSE 客户端（必做，对齐 Step 0.4）
pkill -f "manager-hub/scripts/sse_client.py"        2>/dev/null || true
pkill -f "manager-hub/scripts/sseclient.py"         2>/dev/null || true
pkill -f "openclaw-sidecar/scripts/sse_client.py"   2>/dev/null || true
sleep 1
pgrep -af "manager-hub/scripts/(sse_client|sseclient)\\.py" \\
  || echo "✅ 无旧 sse_client 残留"

# 1.5 启动 hub-sse-sidecar 守护进程
SIDECAR_DIR="$HOME/.qclaw/skills/hub-sse-sidecar"
if [ -d "$SIDECAR_DIR" ] && [ -f "$SIDECAR_DIR/install.sh" ]; then
  echo "==> 用本地 SKILL 自带 install.sh 启动 sidecar"
  CLAW_ID="$CLAW_ID" API_TOKEN="$TOKEN" HUB_URL="$HUB" bash "$SIDECAR_DIR/install.sh"
else
  echo "==> 本地未找到 hub-sse-sidecar SKILL，从 Hub 拉一键脚本"
  curl -fsSL -o /tmp/hub-sidecar-install.sh "$HUB/static/skills/hub-sse-sidecar/install.sh"
  CLAW_ID="$CLAW_ID" API_TOKEN="$TOKEN" HUB_URL="$HUB" bash /tmp/hub-sidecar-install.sh
fi

# 1.6 等 3s 让 sidecar 完成首次连接
sleep 3
pgrep -f "openclaw-sidecar/scripts/sse_client.py" >/dev/null \\
  && echo "✅ sidecar 已起" \\
  || {{ echo "❌ sidecar 未运行，请看 ~/.openclaw-sidecar/logs/sse_client.bootstrap.log"; exit 1; }}

tail -n 5 "$HOME/.openclaw-sidecar/logs/sse_client.log" 2>/dev/null
```

---

## Step 2：注册成功判定（init 任务必须全过）

在 Hub 的 `init` 任务里，以下 5 项必须最终为 `approved`（仅 `submitted` 不算）：

| # | verification_target | 说明 |
|---|---|---|
| 1 | `sidecar-online` | 进程在跑 + `sse_client.log` 有 `✅ 已连接 Hub` |
| 2 | `chat-closure` | 收到测试消息后 `read_at` 落库 + `from_claw` 自然语义回复 |
| 3 | `todo-closure` | 收到 todo → Agent 处理 → `complete` 接口闭环 |
| 4 | `registration-report` | 提交首份注册日报作为证据 |
| 5 | `dragonking-approval` | 龙虾王最终人工审核通过 |

> 详细规约见本地 `~/.qclaw/skills/hub-sse-sidecar/SKILL.md` 的 §5「工作流验证」与 §5.1「企微通知规范」。

---

## Step 3：自检脚本（含 read_at 闭环验证）

```bash
HUB="{hub_url}"
CLAW_ID="{claw_id}"
TOKEN="{api_token}"
AUTH="Authorization: Bearer $TOKEN"

echo "=== 3.1 sidecar 进程 ==="
pgrep -af "openclaw-sidecar/scripts/sse_client.py" || echo "❌ sidecar 不在跑"

echo "=== 3.2 sidecar 最近日志 ==="
tail -n 15 "$HOME/.openclaw-sidecar/logs/sse_client.log" 2>/dev/null
tail -n 5  "$HOME/.openclaw-sidecar/logs/sse_client.bootstrap.log" 2>/dev/null

echo "=== 3.3 worker 最近日志（若已被消息唤醒）==="
tail -n 15 "$HOME/.openclaw-sidecar/logs/hub_worker.log" 2>/dev/null \\
  || echo "(暂无 worker 日志，等收到消息后才会生成)"

echo "=== 3.4 配置接口连通性 ==="
curl -s -w "\\nHTTP %{{http_code}}\\n" -H "$AUTH" "$HUB/api/v1/openclaws/$CLAW_ID/config" \\
  | python3 -m json.tool 2>/dev/null | head -n 30

echo "=== 3.5 read_at 闭环：未读 to_claw 应尽快归零 ==="
curl -s -H "$AUTH" "$HUB/api/openclaws/$CLAW_ID/messages?unread=true&direction=to_claw&limit=20" \\
  | python3 -m json.tool | head -n 80

echo "=== 3.6 init 任务进度 ==="
curl -s -H "$AUTH" "$HUB/api/v1/openclaws/$CLAW_ID/todos?category=init" \\
  | python3 -m json.tool | head -n 160
```

---

## Step 4：真验证（让管理员发一条测试消息）

部署完成后，请 Hub 管理员到 Web 通信中心给本 OpenClaw 发：

> "你能听见我说话吗？请用一句话回复。"

**验收硬指标**（推荐作为上线门槛）：

- [ ] 该 `to_claw/chat` 消息在 60 秒内 `read_at` 落库（不再为 NULL）
- [ ] 至少有一条 `from_claw` 回复的 `reply_to == 当前 msg_id`
- [ ] 回复内容是**自然语义**（不是"我已收到/根据协议"模板罐头）

---

## Step 5：企微通知最低规范（摘自 Skill #135 §5.1）

> 完整规范见本地 `~/.qclaw/skills/hub-sse-sidecar/SKILL.md` §5.1「企微通知规范（定版）」。

**触发范围**：
- ✅ **必须**：`todos` 任务进入「已完成提交」或「阻塞需人工介入」时
- 🟡 **可选**：`chat` 消息处理完成后，按 Agent 自身规则决定
- ❌ **禁止**：重复状态 / 无增量信息 / 仅内部重试过程

**最低内容**（每条至少含 4 项）：
1. 任务标识（`todo_id` 或可追踪标题）
2. 当前状态（完成/阻塞）
3. 结果摘要（1 句）
4. 下一步动作（若阻塞**必须**写）

**执行约束**：**先 Hub 闭环（`/read` 或 `/todos/<id>/complete`），再企微通知**；企微失败**不**回滚 Hub 闭环；同 `todo_id` 同状态短时间不重复推。

---

## Step 6：故障排查 mini 表（完整版见本地 SKILL）

| 现象 | 排查 / 修法 |
|---|---|
| `pgrep -f sse_client.py` 无结果 | 看 `~/.openclaw-sidecar/logs/sse_client.bootstrap.log`；最常见 `API_TOKEN` 错或 `HUB_URL` 不通 |
| log 反复 `❌ 连接异常: HTTP 401` | Token 失效，重新打开 `/registration-skill` 拿新 token，更新 `~/.openclaw-sidecar/config.env` |
| 进程在跑但长时间收不到事件 | **Step 0.4** 双客户端冲突；执行 `pkill -f "manager-hub/scripts/(sse_client\\|sseclient)\\.py"` |
| log 有 `事件[message]` 但 Web 看不到回复 | worker 调 `openclaw agent` 失败，看 `hub_worker.log`；常见 `openclaw` 不在 PATH |
| 同一条消息**反复**出现 | Agent 没调 `PUT /messages/<id>/read`；看 worker 是否走"60s 兜底"；检查是否还有旧 `seen_msg_ids.json`，**有则删** |
| Web 看到回复仍是"已转交" | 老 sidecar 还在；`kill $(cat ~/.qclaw/agent.pid 2>/dev/null)` 后清理冲突 |
| 调 `/read` 但 Web 仍"待发送" | 服务端 SSE loop 长事务问题；确认 `agent_client.py` 含 `db.session.rollback()`；`systemctl restart openclaw-web` |

---

## 已分配资源（备查）

**Skills**：

{skill_link_lines}

**Rules**：

{rule_link_lines}

---

## 触发词

- "连接 Hub"
- "注册 OpenClaw"
- "自部署 hub"
- "init 自检"
- "sidecar 没回"
"""
