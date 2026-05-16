"""注册自部署 Bootstrap 生成器（sidecar v2）。

为 ``GET /api/v1/openclaws/{claw_id}/registration-skill`` 生成给 Agent 阅读的
Markdown；为 ``GET /api/v1/openclaws/{claw_id}/bootstrap.sh`` 生成可直接执行的一键
安装脚本。

目标体验：管理员把一个注册链接/一条命令丢给 Agent，Agent 执行后自动完成：

1. 写入本地 Hub 身份配置；
2. 拉取并安装 #143 ``hub-sse-sidecar-v2``；
3. 启动 SSE 长连接；
4. 回传心跳并调用部署验证接口。
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any


# MCP server 安装根目录（在客户端机器上，仅 JSON 接口使用）
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
# 历史教训 #126b：claw 实际能直连的是 :18800（gunicorn 真身），不是 443 的 lampp/Apache。
# clawteam.woa.com:18800 早就关了；https://clawteam.woa.com 撞 lampp Apache 占位 404。
DEFAULT_MCP_TARBALL_URL = os.environ.get(
    "OPENCLAW_HUB_MCP_TARBALL_URL",
    "https://clawteam.woa.com:18800/static/dist/openclaw-hub-mcp.tar.gz",
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
    """为三种 host 各生成一个 mcp.json 片段（仅 JSON 格式接口使用）。"""
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


def _sh_single_quote(value: str) -> str:
    """返回可安全放入 POSIX shell 单引号里的字面量。"""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _bootstrap_sh_url(hub_url: str, claw_id: int, api_token: str) -> str:
    return (
        f"{hub_url.rstrip('/')}/api/v1/openclaws/{claw_id}/bootstrap.sh"
        f"?token={urllib.parse.quote(api_token)}"
    )


def build_bootstrap_command(hub_url: str, claw_id: int, api_token: str) -> str:
    """生成可直接复制给 Agent 执行的一键命令。"""
    return "bash <(curl -fsSL " + json.dumps(
        _bootstrap_sh_url(hub_url, claw_id, api_token), ensure_ascii=False) + ")"


def build_bootstrap_script(
    *,
    hub_url: str,
    claw_id: int,
    claw_name: str,
    role: str,
    project_name: str,
    api_token: str,
) -> str:
    """生成客户端一键安装脚本。

    脚本只依赖 bash、curl、python3 和 systemd（无 systemd 时 install_v2.sh 会提示手动启动）。
    """
    hub = hub_url.rstrip('/')
    return f"""#!/usr/bin/env bash
set -euo pipefail

HUB_URL={_sh_single_quote(hub)}
CLAW_ID={_sh_single_quote(str(claw_id))}
CLAW_TOKEN={_sh_single_quote(api_token)}
CLAW_NAME={_sh_single_quote(claw_name or ('claw-' + str(claw_id)))}
CLAW_ROLE={_sh_single_quote(role or 'test_member')}
PROJECT_NAME={_sh_single_quote(project_name or '未指定')}
EXPECTED_SIDECAR_VERSION="${{EXPECTED_SIDECAR_VERSION:-2.0.1}}"

log() {{ echo "[bootstrap] $*" >&2; }}
err() {{ echo "[bootstrap][ERROR] $*" >&2; exit 1; }}
need_cmd() {{ command -v "$1" >/dev/null 2>&1 || err "缺少命令: $1"; }}

# ---- Windows / Git Bash 检测 ----
IS_WINDOWS=0
WIN_HOME=""
if [[ "$OSTYPE" == msys* ]] || [[ "$OSTYPE" == mingw* ]] || [[ "$OSTYPE" == cygwin* ]] || [[ -n "${{WINDIR:-}}" ]]; then
    IS_WINDOWS=1
    # Git Bash 中 $HOME 是 /c/Users/xxx，转成 Windows 路径供 .bat 使用
    WIN_HOME="$(cygpath -w "$HOME" 2>/dev/null || echo "$USERPROFILE")"
    log "检测到 Windows 环境 (OSTYPE=$OSTYPE)，将跳过 systemd，生成 .bat 启动脚本"
fi

need_cmd bash
need_cmd curl
need_cmd python3

log "Step 1/5 写入 ~/.qclaw/agent.md"
mkdir -p "$HOME/.qclaw"
umask 077
cat > "$HOME/.qclaw/agent.md" <<AGENT_MD_EOF
---
hub_url: $HUB_URL
claw_id: $CLAW_ID
api_token: $CLAW_TOKEN
---

# $CLAW_NAME

## 角色
$CLAW_ROLE

## 所属项目
$PROJECT_NAME
AGENT_MD_EOF
chmod 600 "$HOME/.qclaw/agent.md"

log "Step 2/5 清理旧通信进程，避免双 SSE 客户端抢消息"
if [ "$IS_WINDOWS" = "1" ]; then
    # Windows: 用 taskkill 杀 python sidecar 进程
    taskkill //F //FI "WINDOWTITLE eq sidecar_v2*" 2>/dev/null || true
    taskkill //F //FI "IMAGENAME eq python*" //FI "WINDOWTITLE eq hub-sse-sidecar*" 2>/dev/null || true
    # 兜底：wmic 按命令行匹配
    wmic process where "CommandLine like '%sidecar_v2.py%'" call terminate 2>/dev/null || true
else
    pkill -f 'manager-hub/scripts/sse_client.py' 2>/dev/null || true
    pkill -f 'manager-hub/scripts/sseclient.py' 2>/dev/null || true
    pkill -f 'openclaw-sidecar/scripts/sse_client.py' 2>/dev/null || true
    pkill -f 'hub-sse-sidecar/scripts/sse_client.py' 2>/dev/null || true
    pkill -f 'hub-sse-sidecar/scripts/hub_worker.py' 2>/dev/null || true
    pkill -f 'sidecar_v2.py' 2>/dev/null || true
fi

TMP_DIR="$(mktemp -d)"
cleanup() {{ rm -rf "$TMP_DIR"; }}
trap cleanup EXIT
mkdir -p "$TMP_DIR/scripts"

log "Step 3/5 拉取 #143 hub-sse-sidecar-v2 安装包"
curl -fsSL -o "$TMP_DIR/install_v2.sh" \
  "$HUB_URL/static/skills/hub-sse-sidecar-v2/install_v2.sh"
curl -fsSL -o "$TMP_DIR/scripts/sidecar_v2.py" \
  "$HUB_URL/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py"
curl -fsSL -o "$TMP_DIR/scripts/cleanup_v1.sh" \
  "$HUB_URL/static/skills/hub-sse-sidecar-v2/scripts/cleanup_v1.sh" || true
chmod +x "$TMP_DIR/install_v2.sh" "$TMP_DIR/scripts/sidecar_v2.py" 2>/dev/null || true

if [ -s "$TMP_DIR/scripts/cleanup_v1.sh" ]; then
  log "Step 4/5 执行 v1 清理脚本"
  bash "$TMP_DIR/scripts/cleanup_v1.sh" || true
else
  log "Step 4/5 未找到 cleanup_v1.sh，已跳过"
fi

log "Step 5/5 安装并启动 sidecar v2"
# Windows 下强制 SKIP_SYSTEMD，由 install_v2.sh 生成 .bat
export SKIP_SYSTEMD="${{SKIP_SYSTEMD:-$IS_WINDOWS}}"
HUB_URL="$HUB_URL" \
CLAW_ID="$CLAW_ID" \
CLAW_TOKEN="$CLAW_TOKEN" \
EXPECTED_SIDECAR_VERSION="$EXPECTED_SIDECAR_VERSION" \
IS_WINDOWS="$IS_WINDOWS" \
WIN_HOME="$WIN_HOME" \
bash "$TMP_DIR/install_v2.sh"

log "最终验证 Hub 心跳和 #143 启用状态"
curl -fsSL -H "Authorization: Bearer $CLAW_TOKEN" \
  "$HUB_URL/api/openclaws/$CLAW_ID/sidecar-deployment-verify?expected_sidecar_version=$EXPECTED_SIDECAR_VERSION&heartbeat_max_age_sec=180&notify=1" \
  | python3 -m json.tool

log "完成：Hub 已能通过 SSE 实时给本 Agent 下发消息。"
"""


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
    # 以下参数仅为兼容旧调用签名
    mcp_entry: str | None = None,        # noqa: ARG001
    mcp_repo: str | None = None,          # noqa: ARG001
    mcp_tarball_url: str | None = None,   # noqa: ARG001
    mcp_install_dir: str | None = None,   # noqa: ARG001
) -> str:
    """生成给 Agent 阅读的 v2 注册自部署 Markdown。"""
    skill_links = skill_links or []
    rule_links = rule_links or []
    skill_link_lines = "\n".join(f"- {u}" for u in skill_links) or "- (无)"
    rule_link_lines = "\n".join(f"- {u}" for u in rule_links) or "- (无)"
    bootstrap_url = _bootstrap_sh_url(hub_url, claw_id, api_token)
    bootstrap_cmd = build_bootstrap_command(hub_url, claw_id, api_token)

    return f"""# OpenClaw 自部署 — {claw_name}（#143 sidecar v2）

> 本文档由 Hub 自动生成，已包含本 OpenClaw 的连接凭证。  
> 目标：把本文档或下面的一键命令丢给 Agent，Agent 执行后自动安装 #143、启动 SSE 长连接，并回传验证结果。

---

## 一键部署命令（推荐）

请在 Agent 所在机器执行：

```bash
{bootstrap_cmd}
```

如果当前 shell 不支持 `<(...)`，使用兼容写法：

```bash
curl -fsSL -o /tmp/openclaw-bootstrap-{claw_id}.sh {json.dumps(bootstrap_url, ensure_ascii=False)}
bash /tmp/openclaw-bootstrap-{claw_id}.sh
```

### Windows 部署（Git Bash）

Windows 上需要先安装 [Git for Windows](https://git-scm.com/download/win)（自带 Git Bash + curl），然后在 **Git Bash** 中执行同一条命令：

```bash
{bootstrap_cmd}
```

脚本会自动检测 Windows 环境，跳过 systemd，生成 `start_sidecar.bat` / `stop_sidecar.bat` 启动脚本。部署完成后：
- **启动**：双击 `~/.qclaw/start_sidecar.bat`
- **停止**：双击 `~/.qclaw/stop_sidecar.bat`，或直接关闭窗口

---

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

## 部署后应该发生什么

1. 本地写入 `~/.qclaw/agent.md`，只保存 `HUB_URL / CLAW_ID / CLAW_TOKEN`。
2. 自动清理旧 `#135 / manager-hub` 通信进程，避免双 SSE 客户端抢消息。
3. 从 Hub 拉取 `#143 hub-sse-sidecar-v2` 的 `install_v2.sh / sidecar_v2.py / cleanup_v1.sh`。
4. 安装并启动 `hub-sse-sidecar-v2` systemd 服务。
5. sidecar 主动连接 `{hub_url}/api/openclaws/{claw_id}/events`，之后 Hub 可以实时推送聊天消息和待办。
6. 脚本最后调用 `sidecar-deployment-verify`，确认 #143 已启用、心跳正常。

---

## 成功判定

部署成功后，Hub 详情页应看到：

- `connection_mode = sse`
- `#143 hub-sse-sidecar-v2` 已安装并启用
- `claw_sidecar_configs.sidecar_version = 2.0.1`
- `last_heartbeat_at` 在 180 秒内
- 通信中心给该 OpenClaw 发消息后，agent 能实时收到并回写 `done / failed`

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `必须设置 CLAW_TOKEN` | 只能用 `CLAW_TOKEN`，不能写成旧的 `API_TOKEN` |
| `connection_mode != sse` | 打开注册链接会自动把该 claw 切到 `sse`；也可在 Hub 详情页确认 |
| `未启用 #143` | 打开注册链接 / bootstrap.sh 会自动启用 #143；若仍失败，到 Skills 页签手动启用 |
| 仍反复处理旧待办 | 杀掉旧进程：`pkill -f 'sse_client.py|hub_worker.py|sidecar_v2.py'` 后重跑一键命令 |
| systemd 不可用 | 脚本会提示手动运行 `sidecar_v2.py`，按提示 source `sidecar.env` 后启动 |
| Windows 上 `pkill` 报错 | 正常现象，已自动 fallback 到 `taskkill`；不影响部署 |
| Windows 双击 `.bat` 闪退 | 检查系统 PATH 中是否有 python；或在 cmd 中手动运行 `start_sidecar.bat` 查看错误输出 |

---

## 已分配资源（备查）

**Skills**：

{skill_link_lines}

**Rules**：

{rule_link_lines}
"""
