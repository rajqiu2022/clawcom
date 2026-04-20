"""注册自部署 Bootstrap 生成器

为 ``GET /api/v1/openclaws/{claw_id}/registration-skill`` 生成"完整自部署"
Markdown 文档与对应的 MCP 配置片段，让任意 AI 客户端拿到 URL 后能在
shell 里跑完 7 步：

    1. 检测 host runtime（Cursor / Claude Code / CodeBuddy / qclaw / unknown）
    2. 写入 ~/.qclaw/agent.md（含 token）
    3. 创建 ~/.qclaw/inbox/ 骨架（pending/processed/replies）
    4. 拉取 Hub 已分配的 skills / rules 到 ~/.qclaw/{skills,rules}/
    5. 安装 openclaw-hub MCP server，把 mcp.json 写入对应 host
    6. 重启 host 让 MCP server 接管 SSE 长连接
    7. 自检：hub_config / hub_pending / 测试消息回复

输出可由 ``GET ?format=json`` 拿到结构化数据，或 ``?format=markdown`` 拿到
完整 Markdown 文档（默认）。
"""

from __future__ import annotations

import json
import os
from typing import Any


# MCP server 安装根目录（在 **客户端机器** 上）
# 用 ~ 而不是 $HOME，因为 node/MCP host 启动时不做 shell 展开；
# 在 merge 脚本里用 os.path.expanduser 展开成绝对路径写到 mcp.json
DEFAULT_MCP_INSTALL_DIR = os.environ.get(
    "OPENCLAW_HUB_MCP_INSTALL_DIR",
    "~/.qclaw/mcp",
)

# MCP server 入口路径（解压后的 dist/index.js 位置）
DEFAULT_MCP_ENTRY = os.environ.get(
    "OPENCLAW_HUB_MCP_PATH",
    f"{DEFAULT_MCP_INSTALL_DIR}/openclaw-hub/dist/index.js",
)

# MCP server tarball 下载 URL（由 Hub 提供，客户端 wget 拉取）
DEFAULT_MCP_TARBALL_URL = os.environ.get(
    "OPENCLAW_HUB_MCP_TARBALL_URL",
    "http://9.134.11.169:8088/static/dist/openclaw-hub-mcp.tar.gz",
)

# 兼容老变量名（已废弃，保留以免外部脚本崩）
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
    """为三种 host 各生成一个 mcp.json 片段

    返回结构示例::

        {
            "cursor":     {"path": "~/.cursor/mcp.json",                 "config": {...}},
            "claude":     {"path": "~/.claude.json",                     "config": {...}},
            "codebuddy":  {"path": ".codebuddy/mcp.json",                "config": {...}},
        }

    其中 ``config`` 已是该 host 直接可用的 mcpServers 配置体。
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


def _format_mcp_block(snippet: dict[str, Any]) -> str:
    """把 mcp config 片段格式化为可贴到 Markdown 的 JSON 块"""
    return json.dumps(snippet["config"], indent=2, ensure_ascii=False)


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
    mcp_entry: str | None = None,
    mcp_repo: str | None = None,
    mcp_tarball_url: str | None = None,
    mcp_install_dir: str | None = None,
) -> str:
    """生成完整的"7 步自部署"Markdown 文档

    所有占位符已被填好实际值；AI 拿到这份文档后可逐节执行 shell 脚本。
    """
    skill_links = skill_links or []
    rule_links = rule_links or []
    entry = mcp_entry or DEFAULT_MCP_ENTRY
    install_dir = mcp_install_dir or DEFAULT_MCP_INSTALL_DIR
    tarball_url = mcp_tarball_url or mcp_repo or DEFAULT_MCP_TARBALL_URL
    snippets = build_mcp_config_snippets(hub_url, claw_id, api_token, entry)

    # 各 host 的 MCP 配置 JSON 文本
    cursor_mcp = _format_mcp_block(snippets["cursor"])
    claude_mcp = _format_mcp_block(snippets["claude"])
    codebuddy_mcp = _format_mcp_block(snippets["codebuddy"])

    # skill / rule 链接以 Markdown 列表渲染
    skill_link_lines = "\n".join(f"- {u}" for u in skill_links) or "- (无)"
    rule_link_lines = "\n".join(f"- {u}" for u in rule_links) or "- (无)"

    # 用于"合并而非覆盖"已有 mcp.json 的 heredoc 脚本（三种 host 通用，
    # 仅 mcp.json 路径不同）。把 entry/hub_url/token 直接拼到 Python 字面量。
    def _merge_script(mcp_path: str) -> str:
        return (
            f"python3 - <<'MCP_MERGE_EOF'\n"
            f"import json, os\n"
            f"p = os.path.expanduser({mcp_path!r})\n"
            f"entry = os.path.expanduser({entry!r})\n"
            f"os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            f"d = json.load(open(p)) if os.path.exists(p) else {{}}\n"
            f"d.setdefault('mcpServers', {{}})['openclaw-hub'] = {{\n"
            f"    'command': 'node',\n"
            f"    'args': [entry],\n"
            f"    'env': {{\n"
            f"        'HUB_URL': {hub_url!r},\n"
            f"        'CLAW_ID': {str(claw_id)!r},\n"
            f"        'API_TOKEN': {api_token!r},\n"
            f"    }},\n"
            f"}}\n"
            f"json.dump(d, open(p,'w'), indent=2, ensure_ascii=False)\n"
            f"print('merged into', p, '-> entry:', entry)\n"
            f"MCP_MERGE_EOF"
        )

    cursor_merge = _merge_script("~/.cursor/mcp.json")
    claude_merge = _merge_script("~/.claude.json")
    codebuddy_merge = _merge_script(".codebuddy/mcp.json")

    return f"""# OpenClaw 自部署 — {claw_name}

> 本文档由 Hub 自动生成，已包含完整的连接凭证。
> 把整份文档交给你的 AI 客户端（Cursor / Claude Code / CodeBuddy / qclaw），
> 它会按顺序跑完所有步骤，最终让你能在 Web 界面给本 OpenClaw 发消息并得到 AI 真实回复。

## 身份信息（已由 Hub 填好）

| 字段 | 值 |
|---|---|
| Hub 地址 | `{hub_url}` |
| Claw ID | `{claw_id}` |
| Claw 名称 | `{claw_name}` |
| 角色 | `{role}` |
| 所属项目 | `{project_name}` |
| **API Token** | `{api_token}` |

> ⚠️ 此 Token **只在本 URL 中返回一次**，一旦关闭页面将不可恢复。
> AI 必须在 Step 2 把它写入 `~/.qclaw/agent.md`。

---

## 部署步骤总览

| Step | 动作 | 备注 |
|---|---|---|
| 1 | 检测当前 host runtime | Cursor / Claude Code / CodeBuddy / qclaw / unknown |
| 2 | 写入 `~/.qclaw/agent.md`（含 Token） | 后续所有进程都从这里读 |
| 3 | 创建 `~/.qclaw/inbox/` 骨架 | 持久化 + 审计兜底 |
| 4 | 拉取已分配的 skills / rules 到本地 | 让 AI 知道自己有哪些能力 |
| 5 | 安装 openclaw-hub MCP server + hub-sse-sidecar 守护 | 先跑 5.3 起 sidecar（保证不断线），再跑 5.1/5.2 配 MCP（前台 AI 介入） |
| 6 | 重启 host，让 MCP server 接管 SSE 长连接 | sidecar 在 Step 5.3 已起，跟 host 重启无关 |
| 7 | 自检：调 `hub_config` / `hub_pending`，并请管理员发一条测试消息 | 对齐 init-tasks 验证目标 |

---

## Step 1: 检测 host runtime

**Bash / zsh**：

```bash
detect_host() {{
  if [ -d "$HOME/.cursor" ] || [ -n "$CURSOR_AGENT_ID" ]; then echo cursor; return; fi
  if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then echo claude-code; return; fi
  if [ -d "$HOME/.codebuddy" ] || command -v codebuddy >/dev/null 2>&1; then echo codebuddy; return; fi
  if [ -d "$HOME/.qclaw" ] && [ -f "$HOME/.qclaw/agent.md" ]; then echo qclaw; return; fi
  echo unknown
}}
HOST_KIND=$(detect_host)
echo "Detected host: $HOST_KIND"
```

**PowerShell**：

```powershell
function Detect-HostKind {{
  if (Test-Path "$env:USERPROFILE\\.cursor") {{ return "cursor" }}
  if (Test-Path "$env:USERPROFILE\\.claude") {{ return "claude-code" }}
  if (Test-Path "$env:USERPROFILE\\.codebuddy") {{ return "codebuddy" }}
  if (Test-Path "$env:USERPROFILE\\.qclaw") {{ return "qclaw" }}
  return "unknown"
}}
$HOST_KIND = Detect-HostKind
Write-Output "Detected host: $HOST_KIND"
```

---

## Step 2: 写入 ~/.qclaw/agent.md

```bash
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
echo "agent.md written"
```

> agent.md 设为 0600 是因为里面有 token；不要 commit 到任何 git 仓库。

---

## Step 3: 创建 inbox 骨架

```bash
mkdir -p "$HOME/.qclaw/inbox/pending"
mkdir -p "$HOME/.qclaw/inbox/processed"
mkdir -p "$HOME/.qclaw/inbox/replies"
echo "inbox skeleton created"
```

inbox 主要是 **审计 / 断电恢复** 用。MCP server 启动后会把每个 SSE 事件
落一份到 `pending/`，处理完移到 `processed/`，AI 写的回复落到 `replies/`
然后由 MCP 回送 Hub。

---

## Step 4: 拉取已分配的 skills 和 rules

```bash
HUB="{hub_url}"
TOKEN="{api_token}"
CLAW_ID="{claw_id}"

mkdir -p "$HOME/.qclaw/skills" "$HOME/.qclaw/rules"

# Skills：每个 skill 一个目录，含 SKILL.md 及打包文件
curl -s -H "Authorization: Bearer $TOKEN" \\
  "$HUB/api/v1/openclaws/$CLAW_ID/assigned-skills" \\
  | python3 - <<'SKILL_DOWNLOAD_EOF'
import json, os, sys, urllib.request
HUB = os.environ.get('HUB', '{hub_url}')
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
            print(f'  pack download skipped for {{name}}: {{e}}', file=sys.stderr)
    print(f'installed skill: {{name}}')
SKILL_DOWNLOAD_EOF

# Rules：每条规则一个 .md 文件
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
    print(f'installed rule: {{name}}')
RULE_DOWNLOAD_EOF
```

> 如果你的环境没有 python3，可以改用 `jq` 或手动 `curl` 单个 skill/rule 下载。

已分配的 skill / rule 直链（备查）：

**Skills**：

{skill_link_lines}

**Rules**：

{rule_link_lines}

---

## Step 5: 安装 openclaw-hub MCP server

要让 SSE 长连接和 AI host 同生共死（host 关 → SSE 断 → Hub 自动 offline），
必须把 MCP server 注册到 host 的 mcp 配置里。

> 推荐顺序：**先执行 5.3 启 sidecar（立刻恢复消息闭环）**，再执行 5.1/5.2 配 MCP。
> 这样即使 MCP 配置出错导致 host 重启，sidecar 仍可维持 Hub 消息与待办处理。

### 5.1 准备 MCP server 包

```bash
# 前置依赖：本机需要 node >= 18 和 npm（自检）
command -v node >/dev/null && echo "node $(node --version)" || \
  {{ echo "ERR: 没有 node，请先装 Node.js 18+ (https://nodejs.org/)"; exit 1; }}
command -v npm  >/dev/null && echo "npm  $(npm --version)"  || \
  {{ echo "ERR: 没有 npm";  exit 1; }}

# 从 Hub 拉 tarball（不含 node_modules，~30KB）
INSTALL_DIR="{install_dir}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

curl -fsSL -o openclaw-hub-mcp.tar.gz "{tarball_url}"
tar xzf openclaw-hub-mcp.tar.gz
rm -f openclaw-hub-mcp.tar.gz

# 安装运行时依赖（约 30MB，纯 JS 包跨平台）
cd "$INSTALL_DIR/openclaw-hub"
npm install --omit=dev --no-fund --no-audit

# tarball 已带 dist/，无需再构建；如有改动自己跑 npm run build
test -f {entry} && echo "MCP entry OK: {entry}" \
  || {{ echo "ERR: MCP entry not found at {entry}"; exit 1; }}

# hub-inbox fallback skill 也一并安装到 ~/.qclaw/skills/
if [ -d "$INSTALL_DIR/../hub-inbox" ] || [ -d "$INSTALL_DIR/hub-inbox" ]; then
  : # 由 tarball 解压时直接放到 INSTALL_DIR/hub-inbox
fi
SRC_HUB_INBOX="$INSTALL_DIR/hub-inbox"
if [ -d "$SRC_HUB_INBOX" ]; then
  mkdir -p "$HOME/.qclaw/skills/hub-inbox"
  cp -f "$SRC_HUB_INBOX"/* "$HOME/.qclaw/skills/hub-inbox/" 2>/dev/null || true
  echo "hub-inbox SKILL installed (fallback for non-MCP hosts)"
fi
```

### 5.2 写入 mcp.json（按 Step 1 检测到的 HOST_KIND 选一种）

> ⚠️ **强烈建议跑下面的"安全合并脚本"而不是手贴 JSON**。
> 因为 MCP host（Cursor/Claude Code/CodeBuddy）启动 `node` 时不会做 shell 展开，
> JSON 里的 `~/.qclaw/...` 必须替换成你的实际家目录绝对路径，合并脚本会自动用
> `os.path.expanduser` 处理。手贴的话记得把 `~` 替换成绝对路径。

#### Cursor（路径：`~/.cursor/mcp.json`）

完整配置（仅供参考，args 中的 `~` 需手动展开为绝对路径）：

```json
{cursor_mcp}
```

如果文件已存在，**不要直接覆盖**，跑下面这段安全合并脚本（自动展开 ~）：

```bash
{cursor_merge}
```

#### Claude Code（路径：`~/.claude.json`）

完整配置（args 中的 `~` 需手动展开为绝对路径）：

```json
{claude_mcp}
```

安全合并（自动展开 ~）：

```bash
{claude_merge}
```

#### CodeBuddy（路径：项目根目录 `.codebuddy/mcp.json`）

完整配置（args 中的 `~` 需手动展开为绝对路径）：

```json
{codebuddy_mcp}
```

安全合并（在项目根目录跑，自动展开 ~）：

```bash
{codebuddy_merge}
```

### 5.3 启动 hub-sse-sidecar 守护进程（强烈推荐 — 无人值守必装）

如果 Hub 已分配 `hub-sse-sidecar` skill（已通过 Step 4 拉到
`~/.qclaw/skills/hub-sse-sidecar/`），**强烈建议** 在这里立刻启动它的守护进程。

为什么必装：

- MCP server 跟 host (Cursor/CodeBuddy) **同生共死** —— host 一关，SSE 就断，Hub
  推下来的消息/待办你看不到。
- hub-sse-sidecar 是**独立 nohup 进程**，机器开着就在跑，**不依赖任何 host**，
  确保 7×24 收消息、Hub 回复、企微通知都能闭环。
- 两者**互补不冲突**：sidecar 负责"无人值守"，MCP 负责"前台 AI 智能介入"，
  共享 Hub v2 的 read_at 去重，不会撞车。

```bash
SIDECAR_DIR="$HOME/.qclaw/skills/hub-sse-sidecar"

# 先清理冲突客户端（manager-hub 旧版 sse_client）
pkill -f "manager-hub/scripts/sse_client.py" 2>/dev/null || true
pkill -f "manager-hub/scripts/sseclient.py" 2>/dev/null || true
pgrep -af "manager-hub/scripts/sse_client.py|manager-hub/scripts/sseclient.py" >/dev/null 2>&1 && \
  echo "[warn] manager-hub sse_client 仍在运行，请先停掉它，避免双连接冲突"

if [ -d "$SIDECAR_DIR" ] && [ -f "$SIDECAR_DIR/install.sh" ]; then
  echo "==> 检测到 hub-sse-sidecar，启动守护进程..."
  CLAW_ID="{claw_id}" \\
  API_TOKEN="{api_token}" \\
  HUB_URL="{hub_url}" \\
    bash "$SIDECAR_DIR/install.sh"
else
  # Hub 没分配这个 skill，或 Step 4 没拉到。fallback：先下载再执行（更稳，便于排错）
  echo "==> hub-sse-sidecar 未在 ~/.qclaw/skills/，从 Hub 直接拉一键脚本启动..."
  curl -fsSL -o /tmp/hub-sidecar-install.sh "{hub_url}/static/skills/hub-sse-sidecar/install.sh"
  CLAW_ID="{claw_id}" API_TOKEN="{api_token}" HUB_URL="{hub_url}" bash /tmp/hub-sidecar-install.sh
fi
```

启动后自检：

```bash
pgrep -f "openclaw-sidecar/scripts/sse_client.py" && echo "✅ sidecar 在跑"
tail -n 10 "$HOME/.openclaw-sidecar/logs/sse_client.log"
tail -n 10 "$HOME/.openclaw-sidecar/logs/sse_client.bootstrap.log" 2>/dev/null || true
```

期望看到 `✅ 已连接 Hub` 字样。日志路径：

- `~/.openclaw-sidecar/logs/sse_client.log` — SSE 守护日志
- `~/.openclaw-sidecar/logs/sse_client.bootstrap.log` — 启动早期日志（config 缺失/环境错误时最有用）
- `~/.openclaw-sidecar/logs/hub_worker.log` — 工作进程日志（被消息唤醒后才有）

> 后续运维：sidecar 启动方式是 `nohup python3 ... &`。如果机器重启后想自动起来，
> 改用 systemd unit（参见 `~/.qclaw/skills/hub-sse-sidecar/SKILL.md` §4 Step 7）。
> ⚠️ **绝对不要**给 sse_client.py 加外部 crontab/shell watchdog——它内部已有
> heartbeat watchdog（默认 120s，可在 config.env 调整），外部监控会误杀。

---

### 5.4 故障回滚（保留 sidecar，临时移除 MCP）

如果出现 host 报错（如 “Gateway is restarting” 循环），按下面步骤回滚：

```bash
# 1) 先确认 sidecar 还在跑（通信不断）
pgrep -f "openclaw-sidecar/scripts/sse_client.py" && echo "sidecar alive"

# 2) 备份 MCP 配置（按你的 host 选其一）
[ -f "$HOME/.cursor/mcp.json" ] && cp "$HOME/.cursor/mcp.json" "$HOME/.cursor/mcp.json.bak.$(date +%s)"
[ -f "$HOME/.claude.json" ] && cp "$HOME/.claude.json" "$HOME/.claude.json.bak.$(date +%s)"
[ -f ".codebuddy/mcp.json" ] && cp ".codebuddy/mcp.json" ".codebuddy/mcp.json.bak.$(date +%s)"

# 3) 删除 openclaw-hub MCP 条目（不影响 sidecar）
python3 - <<'PY'
import json, os
paths = [
    os.path.expanduser("~/.cursor/mcp.json"),
    os.path.expanduser("~/.claude.json"),
    ".codebuddy/mcp.json",
]
for p in paths:
    if not os.path.exists(p):
        continue
    try:
        d = json.load(open(p, encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("mcpServers"), dict):
            d["mcpServers"].pop("openclaw-hub", None)
            json.dump(d, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
            print("removed openclaw-hub from", p)
    except Exception as e:
        print("skip", p, e)
PY
```

回滚后先用 sidecar 跑业务；MCP 稳定性问题单独排查后再加回。

---

#### qclaw 或 host 不支持 MCP

如果你的 AI host 不支持 MCP，**最低限度方案** 是只走 inbox 文件协议 +
让 AI 在每回合开始时主动 `ls ~/.qclaw/inbox/pending/`：

1. Step 5.1 的 tarball 已经把 `hub-inbox` SKILL 拷到 `~/.qclaw/skills/hub-inbox/`
2. 在你的 AGENTS.md / CLAUDE.md / qclaw 根 prompt 里加一句：
   "每回合开始前，先读 `~/.qclaw/skills/hub-inbox/SKILL.md` 并按其规约处理 inbox。"
3. ⚠️ **关键约定**：Hub 端从 v2 起引入"已读"语义（`read_at` 字段），SSE 重连
   **只重推 `read_at IS NULL` 的消息**。所以 AI 真实消费完一条 `source=message`
   事件后**必须**调 `PUT $HUB/api/openclaws/{claw_id}/messages/{{MSG_ID}}/read`
   （可附带 `{{"reply": "..."}}` 一次完成回复 + 标记已读），否则下次 SSE
   重连这条消息会再次出现在 inbox/pending/。
   - **不要**用本地 `seen_msg_ids.json` 之类的永久去重文件——会因为
     "先去重再处理" 误丢同一回合内的新消息。
   - 如果用 MCP server，`hub_reply` 工具已经自动调用 `/read`，无需关心。
4. （可选）如果你**仍然想要 SSE 推送的 legacy Python sidecar**，需要单独从你的代码仓库
   拉 `openclaw-agent/` 目录（本 tarball 不含 sidecar 源码），然后:

   ```bash
   # 假设你已经把 openclaw-agent/ clone 到本地
   cd path/to/openclaw-agent
   pip install -r requirements.txt
   nohup python3 main.py > "$HOME/.qclaw/agent.log" 2>&1 &
   echo $! > "$HOME/.qclaw/agent.pid"
   ```

   sidecar 同样把事件落到 `~/.qclaw/inbox/pending/`，AI 按 SKILL 读取即可。

---

## Step 6: 重启 host

| Host | 动作 |
|---|---|
| Cursor | 命令面板 → "Developer: Reload Window"，或退出 Cursor 重开 |
| Claude Code | 退出 `claude` CLI 后重新启动（让它重新 spawn MCP 子进程） |
| CodeBuddy | 退出后重启 |
| qclaw / legacy | sidecar 已在 Step 5 的 nohup 里启动 |

重启后，host 应当能在 MCP 工具列表里看到：

- `hub_config`
- `hub_pending`
- `hub_consume`
- `hub_reply`
- `hub_submit_report`
- `hub_complete_todo`

---

## Step 7: 自检（对齐 init-tasks 的 4 个验证目标）

`registration-init-tasks` skill 定义了 4 个 `verification_target`：
`memos-access` / `local-skills` / `hub-sse-connect` / `first-report`。
自部署后必须把这 4 个全部通过，并通过 `complete` 接口上报，否则 init-tasks
看板永远显示 "pending"。

下面这段脚本会**一次性跑完所有自检并自动上报对应的 todo**，AI 拿到错误时
可定位是哪一步挂了：

```bash
HUB="{hub_url}"
TOKEN="{api_token}"
CLAW_ID="{claw_id}"
AUTH="Authorization: Bearer $TOKEN"

# ---- 0) 拉取本 claw 的 init 待办列表，建立 verification_target → todo_id 映射
echo "=== Step 7.0: 拉取 init 任务列表 ==="
INIT_TODOS=$(curl -s -H "$AUTH" "$HUB/api/v1/openclaws/$CLAW_ID/todos?category=init")
echo "$INIT_TODOS" | head -c 1000; echo

# 提取 verification_target → id（需要 python3）
declare -A INIT_MAP
while IFS=$'\\t' read -r vt id; do
  [ -n "$vt" ] && INIT_MAP[$vt]="$id"
done < <(echo "$INIT_TODOS" | python3 -c "
import json, sys
data = json.load(sys.stdin)
todos = data.get('todos') or data
if isinstance(todos, dict):
    todos = todos.get('todos', [])
for t in todos:
    vt = t.get('verification_target') or ''
    print(f\"{{vt}}\\t{{t.get('id')}}\")
")

complete_todo() {{
  local vt="$1"; local summary="$2"
  local tid="${{INIT_MAP[$vt]}}"
  if [ -z "$tid" ]; then
    echo "  (skip $vt: no matching todo_id)"
    return
  fi
  curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \\
       -d "$(printf '{{"result_summary":%s}}' "$(printf %s "$summary" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')")" \\
       "$HUB/api/v1/openclaws/$CLAW_ID/todos/$tid/complete" | head -c 200; echo
}}

# ---- 1) hub-sse-connect: 拿 config，确认 Token + URL 可用
echo "=== Step 7.1: hub-sse-connect (GET /config) ==="
CFG=$(curl -s -w "\\n[HTTP %{{http_code}}]" -H "$AUTH" \\
       "$HUB/api/v1/openclaws/$CLAW_ID/config")
echo "$CFG" | tail -c 600
if echo "$CFG" | grep -q '"id"'; then
  complete_todo hub-sse-connect "GET /config 返回 200，连接就绪"
else
  echo "!! hub-sse-connect 失败，请检查 HUB_URL 和 API_TOKEN"
fi

# ---- 2) local-skills: 数 ~/.qclaw/skills 与 Hub 返回的 skill 数量
echo "=== Step 7.2: local-skills ==="
LOCAL_N=$(ls -1 "$HOME/.qclaw/skills" 2>/dev/null | wc -l)
HUB_N=$(curl -s -H "$AUTH" "$HUB/api/v1/openclaws/$CLAW_ID/assigned-skills" \\
        | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('skills',[])))")
echo "local=$LOCAL_N  hub=$HUB_N"
if [ "$LOCAL_N" = "$HUB_N" ] && [ "$LOCAL_N" -gt 0 ]; then
  complete_todo local-skills "本地 $LOCAL_N 个 skill 与 Hub 一致"
else
  echo "!! local-skills 数量不一致，重跑 Step 4"
fi

# ---- 3) memos-access: 仅在本 claw 装了 memos 相关 skill 时才检查
echo "=== Step 7.3: memos-access ==="
if ls "$HOME/.qclaw/skills" 2>/dev/null | grep -qi memos; then
  echo "(本 claw 安装了 memos skill；请用对应 MCP 工具或 curl Memos API 自检)"
  complete_todo memos-access "memos skill 已安装并完成连通性自检"
else
  echo "(本 claw 未安装 memos 相关 skill，跳过 memos-access)"
  complete_todo memos-access "未安装 memos skill，按设计跳过"
fi

# ---- 4) first-report: 提一份注册日报
echo "=== Step 7.4: first-report ==="
TODAY=$(date +%F)
REPORT_BODY=$(printf '{{"tasks_completed":%s,"knowledge_recorded":%s,"report_date":"%s"}}' \\
  "$(printf %s "完成 OpenClaw 自部署：写入 agent.md / inbox / skills / mcp.json，注册成功。" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')" \\
  "$(printf %s "已读取 hub-connect / hub-inbox 两份说明，理解 SSE+inbox 协议。" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')" \\
  "$TODAY")
RESP=$(curl -s -w "\\n[HTTP %{{http_code}}]" -X POST -H "$AUTH" -H "Content-Type: application/json" \\
       -d "$REPORT_BODY" "$HUB/api/v1/openclaws/$CLAW_ID/report")
echo "$RESP" | tail -c 400
if echo "$RESP" | grep -q "200\\|201"; then
  complete_todo first-report "首份注册日报已提交"
else
  echo "!! first-report 失败，请查看上面的 HTTP 状态"
fi

echo
echo "=== 自检完成；可在管理端 init-tasks 页面查看本 claw 的 4 项是否全绿。 ==="
```

> 上述脚本退化场景：如果某个 `verification_target` 在 init 任务列表里不存在
> （比如管理员调整了 init 任务），对应的 `complete_todo` 会输出 `(skip ...)`
> 而不会失败。

---

## 真验证：管理员发一条测试消息

部署完成后，让 Hub 管理员到 Web 界面给本 OpenClaw 发一条消息：

> "你能听见我说话吗？请用一句话回复。"

预期：
- AI host 在下一次回合开始时，MCP server 通过 `hub_pending` 暴露出这条消息
- AI 调 `hub_reply(msg_id, "可以听见，xxx")`
- Web 管理界面看到 AI 的真实回复（**不再是写死的 "✅ 已转交" 罐头文本**）

验收硬指标（建议作为上线门槛）：

- 该条 `to_claw/chat` 消息在 60 秒内 `read_at` 落库（不再是 NULL）
- 至少有一条 `from_claw` 回复的 `reply_to == 当前 msg_id`
- 回复内容是自然语义（不是“我已收到/根据协议”模板）

可用下面命令快速自检（本 claw token）：

```bash
HUB="{hub_url}"
CLAW_ID="{claw_id}"
TOKEN="{api_token}"
AUTH="Authorization: Bearer $TOKEN"

# 最近 20 条聊天消息（双向）
curl -s -H "$AUTH" "$HUB/api/openclaws/$CLAW_ID/messages?limit=20" | python3 -m json.tool | head -n 120

# 最近未读 to_claw（应尽快清零）
curl -s -H "$AUTH" "$HUB/api/openclaws/$CLAW_ID/messages?unread=true&direction=to_claw&limit=20" | python3 -m json.tool | head -n 120
```

如果 7 天内没有完成此验证，本注册视为未生效。

---

## 故障排查

| 现象 | 排查 |
|---|---|
| `hub_config` 401 | Token 配错或 agent.md 没读到，确认 `~/.qclaw/agent.md` 内 `api_token` 字段 |
| MCP 工具列表里没有 `hub_*` | mcp.json 路径写错或 `dist/index.js` 不存在；查看 host 的 MCP 启动日志 |
| SSE 一直 reconnecting | 直接 `curl -N -H "Authorization: Bearer {api_token}" "{hub_url}/api/openclaws/{claw_id}/events"` 自测 |
| AI 收不到 Hub 推的消息 | 看 host MCP 子进程是否在跑：`ps aux \\| grep openclaw-hub` |
| SSE 偶发掉线、延迟高、日志停在旧时间点 | 常见是双 SSE 客户端冲突（`manager-hub` 旧脚本 + `hub-sse-sidecar`）。执行：`pkill -f "manager-hub/scripts/sse_client.py"` 和 `pkill -f "manager-hub/scripts/sseclient.py"`，确保仅保留 `~/.openclaw-sidecar/scripts/sse_client.py`。 |
| Web 看到回复仍然是"已转交" | 老 sidecar 还在运行，`kill $(cat ~/.qclaw/agent.pid)` 后重启 host |
| 同一条消息**反复**出现在 inbox/pending/，新消息却不进来 | AI 处理完没调 `PUT /messages/{{id}}/read`（fallback 模式必须显式调；MCP 模式用 `hub_reply` 自动调）。同时检查客户端是否还在用旧的本地 `seen_msg_ids.json` 永久去重——如有，删掉它。 |
| 看到“重复消息处理 / 系统状态播报”类模板回复，且方向是 `to_claw` | 这是 admin claw “自发自收”回路。新版 Hub 已禁止 `send-to-claw` 给自己；确认服务端已更新并重启，同时让该 claw 重装最新 `hub-sse-sidecar`。 |
| 调了 `/read` 但 Web 仍显示"待发送" | Hub SSE loop 长事务问题；服务端已修，确认 `agent_client.py` 含 `db.session.rollback()` 行，并 `systemctl restart openclaw-web` |

---

## 触发词

- "连接 Hub"
- "注册 OpenClaw"
- "自部署 hub"
- "安装 openclaw-hub MCP"
- "init 自检"
"""


# sidecar-only 精简版（覆盖上面的历史长文案）
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
    mcp_entry: str | None = None,
    mcp_repo: str | None = None,
    mcp_tarball_url: str | None = None,
    mcp_install_dir: str | None = None,
) -> str:
    """生成 sidecar-only 注册文档（不依赖 MCP）。"""
    skill_links = skill_links or []
    rule_links = rule_links or []
    skill_link_lines = "\n".join(f"- {u}" for u in skill_links) or "- (无)"
    rule_link_lines = "\n".join(f"- {u}" for u in rule_links) or "- (无)"

    return f"""# OpenClaw 快速注册（sidecar-only）— {claw_name}

> 本文档是精简流程，不依赖任何前台宿主配置。
> 目标：10 分钟内完成注册并进入“可验收”状态。

## 身份信息

| 字段 | 值 |
|---|---|
| Hub 地址 | `{hub_url}` |
| Claw ID | `{claw_id}` |
| Claw 名称 | `{claw_name}` |
| 角色 | `{role}` |
| 所属项目 | `{project_name}` |
| API Token | `{api_token}` |

---

## 一次性执行（推荐直接复制）

```bash
set -e
HUB="{hub_url}"
CLAW_ID="{claw_id}"
TOKEN="{api_token}"

mkdir -p "$HOME/.qclaw"
cat > "$HOME/.qclaw/agent.md" <<'EOF'
---
hub_url: {hub_url}
claw_id: {claw_id}
api_token: {api_token}
---
EOF
chmod 600 "$HOME/.qclaw/agent.md"

# 清理旧链路冲突（两种脚本名都清）
pkill -f "manager-hub/scripts/sse_client.py" 2>/dev/null || true
pkill -f "manager-hub/scripts/sseclient.py" 2>/dev/null || true
pkill -f "openclaw-sidecar/scripts/sse_client.py" 2>/dev/null || true

# 启 sidecar
curl -fsSL -o /tmp/hub-sidecar-install.sh "$HUB/static/skills/hub-sse-sidecar/install.sh"
CLAW_ID="$CLAW_ID" API_TOKEN="$TOKEN" HUB_URL="$HUB" bash /tmp/hub-sidecar-install.sh
```

---

## 注册成功判定（必须全过）

在 Hub 的 `init` 任务里，以下 5 项必须最终为 `approved`：

1. `sidecar-online`（进程+日志在线）
2. `chat-closure`（聊天 read_at/reply_to 闭环）
3. `todo-closure`（待办可通知并完成）
4. `registration-report`（证据汇总）
5. `dragonking-approval`（龙虾王最终审核）

> 仅 `submitted` 不算通过；必须由龙虾王审核成 `approved`。

---

## 快速自检命令

```bash
HUB="{hub_url}"
CLAW_ID="{claw_id}"
TOKEN="{api_token}"
AUTH="Authorization: Bearer $TOKEN"

pgrep -af "openclaw-sidecar/scripts/sse_client.py"
tail -n 20 "$HOME/.openclaw-sidecar/logs/sse_client.log"
tail -n 20 "$HOME/.openclaw-sidecar/logs/hub_worker.log" 2>/dev/null || true

curl -s -H "$AUTH" "$HUB/api/openclaws/$CLAW_ID/messages?unread=true&direction=to_claw&limit=20" | python3 -m json.tool | head -n 80
curl -s -H "$AUTH" "$HUB/api/v1/openclaws/$CLAW_ID/todos?category=init" | python3 -m json.tool | head -n 160
```

---

## 已分配资源（备查）

Skills:
{skill_link_lines}

Rules:
{rule_link_lines}

---

## 预留：企微通知接口

龙虾王审核全部通过后，服务端会尝试调用 `registration_pass_webhook`（若已配置）。
你给我接口后，我会把龙虾王通知模板和签名逻辑补齐到生产。
"""
