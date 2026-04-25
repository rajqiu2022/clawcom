#!/usr/bin/env bash
# hub-sse-sidecar 一键安装/重启脚本
#
# 用法 1（Hub install_skill 流程已把文件拉到 ~/.qclaw/skills/hub-sse-sidecar/）：
#   CLAW_ID=6 API_TOKEN=oc_tk_xxx bash ~/.qclaw/skills/hub-sse-sidecar/install.sh
#
# 用法 2（不通过 Hub install_skill，直接 curl + 一键）：
#   curl -fsSL http://9.134.11.169:8088/static/skills/hub-sse-sidecar/install.sh \
#     | CLAW_ID=6 API_TOKEN=oc_tk_xxx bash
#
# 必填环境变量：
#   CLAW_ID           — 你的 claw id（小天=6）
#   API_TOKEN         — Hub API token（从 /registration-skill 拿）
#
# 可选环境变量（有默认值）：
#   HUB_URL           — 默认 http://9.134.11.169:8088
#   AGENT_TYPE        — 后端 AI agent 类型，默认 openclaw
#                       openclaw → sidecar 调 PATH 里的 `openclaw agent --message ...`
#                       hermes   → 自动生成 $INSTALL_DIR/scripts/run_hermes.sh wrapper，
#                                  把 OPENCLAW_BIN 指向它（需要 HERMES_HOME 下的 venv）
#                       none     → sidecar 仅写 inbox，不调 CLI 自动回（MCP host 接管场景）
#                       custom   → 用 CUSTOM_AGENT_BIN，签名要兼容
#                                  `<bin> agent --message X --timeout N`
#   HERMES_HOME       — Hermes 安装根目录，默认 /root/hermes-agent
#   CUSTOM_AGENT_BIN  — AGENT_TYPE=custom 时必填
#   OPENCLAW_BIN      — 默认 openclaw（如果你显式传了 OPENCLAW_BIN，
#                       会覆盖 AGENT_TYPE 自动推导出来的值）
#   AGENT_NAME        — 默认 main
#   WECOM_CHANNEL     — 默认 wecom（留空字符串则不发企微）
#   AGENT_TIMEOUT     — 默认 120
#   SKILL_DIR         — 默认 $HOME/.qclaw/skills/hub-sse-sidecar
#                       （Hub install_skill 会把脚本拉到这）
#   USE_SYSTEMD=1     — 安装并启用 systemd unit（开机自启 + crash 自动重启）
#                       要求 root 权限或可 sudo 到 root；默认走 nohup 模式
#
# 命令行选项：
#   --systemd         — 等价 USE_SYSTEMD=1
#   --uninstall       — 停 sidecar、删 systemd unit、保留配置和日志
#   --reconfigure     — 等价 FORCE_RECONFIG=1（强制覆盖 config.env，会自动备份旧文件）
#   --agent-type=TYPE — 等价 AGENT_TYPE=TYPE（openclaw / hermes / none / custom）
#
# 同机多 claw 注意：
#   ~/.openclaw-sidecar/config.env 默认被保护不会被覆盖。
#   如果本机已经被另一个 claw 配置过（CLAW_ID 不同），脚本会主动报错并退出。
#   要让本 claw 接管，加 --reconfigure 或 FORCE_RECONFIG=1，旧 config 会自动备份为
#   config.env.bak.<timestamp>。

set -euo pipefail

# ── 解析命令行 ────────────────────────────────────
USE_SYSTEMD="${USE_SYSTEMD:-0}"
UNINSTALL=0
FORCE_RECONFIG="${FORCE_RECONFIG:-0}"
AGENT_TYPE_OVERRIDE=""
for arg in "$@"; do
  case "$arg" in
    --systemd)        USE_SYSTEMD=1 ;;
    --uninstall)      UNINSTALL=1 ;;
    --reconfigure)    FORCE_RECONFIG=1 ;;
    --agent-type=*)   AGENT_TYPE_OVERRIDE="${arg#--agent-type=}" ;;
    -h|--help)
      grep -E '^# ' "$0" | head -n 50 | sed 's/^# //; s/^#//'
      exit 0 ;;
  esac
done

# ── 必填校验 ──────────────────────────────────────
: "${CLAW_ID:?需要设置 CLAW_ID 环境变量（你的 claw id）}"
: "${API_TOKEN:?需要设置 API_TOKEN 环境变量（Hub API token）}"

HUB_URL="${HUB_URL:-http://9.134.11.169:8088}"
AGENT_TYPE="${AGENT_TYPE_OVERRIDE:-${AGENT_TYPE:-openclaw}}"
HERMES_HOME="${HERMES_HOME:-/root/hermes-agent}"
CUSTOM_AGENT_BIN="${CUSTOM_AGENT_BIN:-}"
# OPENCLAW_BIN 留空，等下文按 AGENT_TYPE 推导（用户显式传则覆盖）
OPENCLAW_BIN_OVERRIDE="${OPENCLAW_BIN:-}"
AGENT_NAME="${AGENT_NAME:-main}"
WECOM_CHANNEL="${WECOM_CHANNEL:-wecom}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-120}"
HEARTBEAT_TIMEOUT="${HEARTBEAT_TIMEOUT:-120}"
TODOS_VERIFY_DELAY="${TODOS_VERIFY_DELAY:-3}"
TODOS_FORCE_COMPLETE_FALLBACK="${TODOS_FORCE_COMPLETE_FALLBACK:-1}"
SKILL_DIR="${SKILL_DIR:-$HOME/.qclaw/skills/hub-sse-sidecar}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.openclaw-sidecar}"
SKILL_REMOTE_BASE="${HUB_URL}/static/skills/hub-sse-sidecar"
# systemd unit 名字默认带 CLAW_ID 后缀，避免同机多 claw 互覆盖
SYSTEMD_UNIT_NAME="${SYSTEMD_UNIT_NAME:-openclaw-sidecar.service}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/$SYSTEMD_UNIT_NAME"

case "$AGENT_TYPE" in
  openclaw|hermes|none|custom) ;;
  *) echo "❌ AGENT_TYPE 必须是 openclaw / hermes / none / custom 之一，当前: $AGENT_TYPE" >&2; exit 1 ;;
esac
if [ "$AGENT_TYPE" = "custom" ] && [ -z "$CUSTOM_AGENT_BIN" ] && [ -z "$OPENCLAW_BIN_OVERRIDE" ]; then
  echo "❌ AGENT_TYPE=custom 时必须设置 CUSTOM_AGENT_BIN（或显式 OPENCLAW_BIN）" >&2
  exit 1
fi

# 卸载分支（其他都跳过）
if [ "$UNINSTALL" = "1" ]; then
  echo "==> uninstall hub-sse-sidecar"
  if [ -f "$SYSTEMD_UNIT_PATH" ]; then
    if [ "$(id -u)" = "0" ]; then
      systemctl stop  "$SYSTEMD_UNIT_NAME" 2>/dev/null || true
      systemctl disable "$SYSTEMD_UNIT_NAME" 2>/dev/null || true
      rm -f "$SYSTEMD_UNIT_PATH"
      systemctl daemon-reload
      echo "    [ok] systemd unit 已删除"
    else
      sudo systemctl stop "$SYSTEMD_UNIT_NAME" 2>/dev/null || true
      sudo systemctl disable "$SYSTEMD_UNIT_NAME" 2>/dev/null || true
      sudo rm -f "$SYSTEMD_UNIT_PATH"
      sudo systemctl daemon-reload
      echo "    [ok] systemd unit 已删除（用 sudo）"
    fi
  fi
  # ⚠️ 用绝对路径精确匹配，避免误杀同机其他 INSTALL_DIR 的 sidecar
  SSE_PATTERN="$INSTALL_DIR/scripts/sse_client.py"
  PIDS=$(pgrep -f "$SSE_PATTERN" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    kill $PIDS 2>/dev/null || true
    sleep 1
    PIDS=$(pgrep -f "$SSE_PATTERN" 2>/dev/null || true)
    [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null || true
    echo "    [ok] sse_client 已停（$SSE_PATTERN）"
  fi
  echo "    （配置 $INSTALL_DIR/config.env 与日志保留，要全删请手动 rm -rf $INSTALL_DIR）"
  exit 0
fi

echo "==> hub-sse-sidecar install"
echo "    HUB_URL       = $HUB_URL"
echo "    CLAW_ID       = $CLAW_ID"
echo "    API_TOKEN     = ${API_TOKEN:0:8}***"
echo "    INSTALL_DIR   = $INSTALL_DIR"
echo "    SKILL_DIR     = $SKILL_DIR"
echo "    USE_SYSTEMD   = $USE_SYSTEMD"

# ── 0) 清理冲突客户端（manager-hub 旧链路） ─────────
stop_conflicting_manager_hub() {
  local conflict_pids
  conflict_pids="$(
    {
      pgrep -f "manager-hub/scripts/sse_client.py" 2>/dev/null || true
      pgrep -f "manager-hub/scripts/sseclient.py" 2>/dev/null || true
    } | tr ' ' '\n' | awk 'NF{print}' | sort -u | tr '\n' ' '
  )"
  if [ -z "$conflict_pids" ]; then
    echo "==> 冲突检查：未发现 manager-hub sse_client"
    return
  fi
  echo "==> 发现冲突客户端（manager-hub sse_client）：$conflict_pids"
  echo "    停止冲突进程，避免双 SSE 客户端抢占消息..."
  kill $conflict_pids 2>/dev/null || true
  sleep 1
  conflict_pids="$(
    {
      pgrep -f "manager-hub/scripts/sse_client.py" 2>/dev/null || true
      pgrep -f "manager-hub/scripts/sseclient.py" 2>/dev/null || true
    } | tr ' ' '\n' | awk 'NF{print}' | sort -u | tr '\n' ' '
  )"
  if [ -n "$conflict_pids" ]; then
    echo "    [warn] 仍在运行，强杀：$conflict_pids"
    kill -9 $conflict_pids 2>/dev/null || true
  fi
  if pgrep -f "manager-hub/scripts/sse_client.py|manager-hub/scripts/sseclient.py" > /dev/null 2>&1; then
    echo "    [warn] manager-hub sse_client 仍可能被外部守护拉起。"
    echo "           请检查 systemd/crontab 并禁用该旧链路。"
  else
    echo "    [ok] 冲突客户端已清理"
  fi
}

# ── 1) 准备目录 ───────────────────────────────────
mkdir -p "$INSTALL_DIR/scripts" "$INSTALL_DIR/logs"

# ── 1.5) 按 AGENT_TYPE 决定 OPENCLAW_BIN ─────────
# 用户显式传 OPENCLAW_BIN 永远优先；否则按 AGENT_TYPE 推导。
if [ -n "$OPENCLAW_BIN_OVERRIDE" ]; then
  OPENCLAW_BIN="$OPENCLAW_BIN_OVERRIDE"
  echo "==> AGENT_TYPE=$AGENT_TYPE，但用户显式 OPENCLAW_BIN=$OPENCLAW_BIN，按用户为准"
else
  case "$AGENT_TYPE" in
    openclaw)
      OPENCLAW_BIN="openclaw"
      ;;
    hermes)
      WRAPPER="$INSTALL_DIR/scripts/run_hermes.sh"
      OPENCLAW_BIN="$WRAPPER"
      echo "==> 生成 Hermes wrapper：$WRAPPER（HERMES_HOME=$HERMES_HOME）"
      cat > "$WRAPPER" <<WRAPEOF
#!/usr/bin/env bash
# Hermes wrapper — 由 hub-sse-sidecar install.sh 生成
# 把 \`<bin> agent --message X --agent Y --channel Z --timeout N\` 转成
# \`hermes_cli.main chat -q X -Q --yolo --source hub_worker\` 形式。
# 模板沿用线上 #12 (小马) 验证 6+ 天的版本。

HERMES_HOME="$HERMES_HOME"
cd "\$HERMES_HOME" || exit 1
export PYTHONPATH="\$HERMES_HOME:\$HERMES_HOME/venv/lib/python3.11/site-packages"

MESSAGE=""
TIMEOUT=300

while [[ \$# -gt 0 ]]; do
    case "\$1" in
        --message) MESSAGE="\$2"; shift 2 ;;
        --timeout) TIMEOUT="\$2"; shift 2 ;;
        *)         shift ;;
    esac
done

if [[ -z "\$MESSAGE" ]]; then
    echo "ERROR: --message is required" >&2
    exit 1
fi

exec "\$HERMES_HOME/venv/bin/python" -m hermes_cli.main chat \\
    -q "\$MESSAGE" \\
    -Q \\
    --yolo \\
    --source hub_worker
WRAPEOF
      chmod 755 "$WRAPPER"
      ;;
    none)
      OPENCLAW_BIN="/nonexistent/agent-disabled"
      echo "==> AGENT_TYPE=none，sidecar 将仅写 inbox，不调任何 CLI 自动回"
      ;;
    custom)
      OPENCLAW_BIN="$CUSTOM_AGENT_BIN"
      ;;
  esac
fi

# ── 2) 拷贝/下载脚本 ──────────────────────────────
copy_or_fetch() {
  local fname="$1"
  local dst="$INSTALL_DIR/scripts/$fname"
  if [ -f "$SKILL_DIR/scripts/$fname" ]; then
    cp "$SKILL_DIR/scripts/$fname" "$dst"
    echo "    [copy] $SKILL_DIR/scripts/$fname → $dst"
  else
    curl -fsSL -o "$dst" "$SKILL_REMOTE_BASE/scripts/$fname"
    echo "    [curl] $SKILL_REMOTE_BASE/scripts/$fname → $dst"
  fi
  chmod 755 "$dst"
}

echo "==> 拷贝脚本"
copy_or_fetch sse_client.py
copy_or_fetch hub_worker.py
python3 -m py_compile "$INSTALL_DIR/scripts/sse_client.py" "$INSTALL_DIR/scripts/hub_worker.py"
echo "    [ok] python 语法检查通过"

# ── 3) 写配置（CLAW_ID 一致性自检） ───────────────
# 设计意图：
#   · config.env 不存在 → 直接生成
#   · 已存在且 CLAW_ID 一致 → 跳过（保留正在跑的 agent，不破坏 token 等手动调整）
#   · 已存在但 CLAW_ID 不同 → ❌ 报错退出，避免新 claw 复用别人的旧配置
#                              （可加 --reconfigure / FORCE_RECONFIG=1 强制覆盖）
CFG="$INSTALL_DIR/config.env"
if [ -f "$CFG" ]; then
  # 健壮提取：取第一行 ^CLAW_ID=，剥离 = 后第一个空白前的内容（防止脏数据/多行混入）
  EXISTING_CLAW_ID="$(grep -E '^CLAW_ID=' "$CFG" 2>/dev/null | head -n1 | sed 's/^CLAW_ID=//' | awk '{print $1}' | tr -d '[:space:]' || true)"
  if [ -z "$EXISTING_CLAW_ID" ]; then
    echo "==> 旧配置存在但找不到 CLAW_ID，视作损坏 → 重新生成（已备份）"
    cp "$CFG" "$CFG.bak.$(date +%Y%m%d_%H%M%S)" || true
    rm -f "$CFG"
  elif [ "$EXISTING_CLAW_ID" = "$CLAW_ID" ]; then
    if [ "$FORCE_RECONFIG" = "1" ]; then
      BACKUP="$CFG.bak.$(date +%Y%m%d_%H%M%S)"
      cp "$CFG" "$BACKUP"
      rm -f "$CFG"
      echo "==> 配置已存在且 CLAW_ID 一致，但 FORCE_RECONFIG=1 已生效 → 重写 $CFG"
      echo "    旧配置备份到：$BACKUP"
    else
      echo "==> 配置已存在且 CLAW_ID 匹配（=$CLAW_ID），跳过：$CFG"
      echo "    （要重生成请加 --reconfigure 或 FORCE_RECONFIG=1）"
      EXISTING_BIN="$(grep -E '^OPENCLAW_BIN=' "$CFG" 2>/dev/null | head -n1 | sed 's/^OPENCLAW_BIN=//' | tr -d '[:space:]' || true)"
      if [ -n "$EXISTING_BIN" ] && [ "$EXISTING_BIN" != "$OPENCLAW_BIN" ]; then
        echo "    [warn] config.env 里 OPENCLAW_BIN=$EXISTING_BIN，与本次推导的 $OPENCLAW_BIN 不一致。"
        echo "           本次跳过没改它；如需切换 AGENT_TYPE 生效，请加 --reconfigure 重写 config.env。"
      fi
    fi
  else
    if [ "$FORCE_RECONFIG" = "1" ]; then
      BACKUP="$CFG.bak.$(date +%Y%m%d_%H%M%S)"
      cp "$CFG" "$BACKUP"
      rm -f "$CFG"
      echo "==> 检测到旧 CLAW_ID=$EXISTING_CLAW_ID ≠ 本次 CLAW_ID=$CLAW_ID"
      echo "    FORCE_RECONFIG=1 已生效，旧配置备份到：$BACKUP"
    else
      cat <<MSG >&2
❌ 检测到 $CFG 里的 CLAW_ID=$EXISTING_CLAW_ID，与本次安装的 CLAW_ID=$CLAW_ID 不一致。
   这通常意味着本机之前给另一个 claw（id=$EXISTING_CLAW_ID）装过 sidecar，
   如果直接复用旧配置，新 sidecar 会用 CLAW_ID=$EXISTING_CLAW_ID 连接 Hub，
   消息/待办都会被错误投递到对方 claw。

修复方法（任选其一）：

  方式 A — 让本 claw 接管（旧 claw 在本机的 sidecar 将停止工作）：
      bash $0 --reconfigure        # 等价 FORCE_RECONFIG=1
      或
      FORCE_RECONFIG=1 CLAW_ID=$CLAW_ID API_TOKEN=*** bash $0

  方式 B — 手动清理后重装：
      bash $0 --uninstall          # 先停旧 sidecar
      rm $CFG                      # 删旧配置
      CLAW_ID=$CLAW_ID API_TOKEN=*** bash $0

  方式 C — 同机要并行跑多个 claw 的 sidecar（不推荐）：
      给本次安装换一个独立目录，例如：
      INSTALL_DIR=\$HOME/.openclaw-sidecar-claw$CLAW_ID CLAW_ID=$CLAW_ID API_TOKEN=*** bash $0
      （需要同步改 systemd unit / 日志路径，进阶用法）
MSG
      exit 1
    fi
  fi
fi

if [ ! -f "$CFG" ]; then
  echo "==> 生成配置：$CFG"
  cat > "$CFG" <<EOF
# hub-sse-sidecar config — generated $(date +%Y-%m-%d_%H:%M:%S)
HUB_URL=$HUB_URL
CLAW_ID=$CLAW_ID
API_TOKEN=$API_TOKEN

OPENCLAW_BIN=$OPENCLAW_BIN
AGENT_NAME=$AGENT_NAME
WECOM_CHANNEL=$WECOM_CHANNEL
AGENT_TIMEOUT=$AGENT_TIMEOUT
HEARTBEAT_TIMEOUT=$HEARTBEAT_TIMEOUT
TODOS_VERIFY_DELAY=$TODOS_VERIFY_DELAY
TODOS_FORCE_COMPLETE_FALLBACK=$TODOS_FORCE_COMPLETE_FALLBACK
EOF
  chmod 600 "$CFG"
fi

# ── 4) 检查后端 agent 调用链 ──────────────────────
echo "==> 检查 OPENCLAW_BIN（AGENT_TYPE=$AGENT_TYPE）"
case "$AGENT_TYPE" in
  openclaw)
    if ! command -v "$OPENCLAW_BIN" > /dev/null 2>&1; then
      echo "    [warn] $OPENCLAW_BIN 不在 PATH 里。"
      echo "           sidecar 仍可启动，但收到消息后无法调起 Agent。"
      echo "           请把 OPENCLAW_BIN 改成绝对路径，或把 openclaw CLI 加到 PATH。"
    else
      if "$OPENCLAW_BIN" agent --help 2>&1 | grep -q -- '--message'; then
        echo "    [ok] $OPENCLAW_BIN agent --message 可用"
      else
        echo "    [warn] $OPENCLAW_BIN agent 命令的参数可能跟我们假设的不一样。"
        echo "           请运行 '$OPENCLAW_BIN agent --help' 确认 --message/--agent/--channel 名字。"
      fi
    fi
    ;;
  hermes)
    HERMES_PY="$HERMES_HOME/venv/bin/python"
    if [ ! -x "$HERMES_PY" ]; then
      echo "    [warn] $HERMES_PY 不存在或不可执行；wrapper 已生成但运行时会失败。"
      echo "           请确认 Hermes Agent 已装好；或通过 HERMES_HOME=/path/to/hermes-agent 重装本 skill。"
    else
      echo "    [ok] Hermes venv: $HERMES_PY"
      if "$HERMES_PY" -c "import hermes_cli.main" 2>/dev/null; then
        echo "    [ok] hermes_cli.main 可导入"
      else
        echo "    [warn] hermes_cli.main 不可导入；wrapper 调用时会失败。"
      fi
    fi
    [ -x "$OPENCLAW_BIN" ] && echo "    [ok] wrapper 可执行: $OPENCLAW_BIN"
    ;;
  none)
    echo "    [info] AGENT_TYPE=none，跳过 CLI 检测（OPENCLAW_BIN=$OPENCLAW_BIN 仅占位）"
    ;;
  custom)
    if [ -x "$OPENCLAW_BIN" ] || command -v "$OPENCLAW_BIN" > /dev/null 2>&1; then
      echo "    [ok] custom CLI: $OPENCLAW_BIN"
    else
      echo "    [warn] $OPENCLAW_BIN 不存在或不可执行；sidecar 启动后调用会失败。"
    fi
    ;;
esac

# ── 5) 停掉旧的 sse_client（systemd / nohup 都覆盖） ─
echo "==> 停掉旧的 sidecar 实例（如果在跑）"
stop_conflicting_manager_hub
if systemctl is-active --quiet "$SYSTEMD_UNIT_NAME" 2>/dev/null; then
  if [ "$(id -u)" = "0" ]; then
    systemctl stop "$SYSTEMD_UNIT_NAME" || true
  else
    sudo systemctl stop "$SYSTEMD_UNIT_NAME" 2>/dev/null || true
  fi
  echo "    [ok] systemd unit 已停"
fi
# ⚠️ 用绝对路径精确匹配，避免误杀同机其他 INSTALL_DIR 的 sidecar
SSE_PATTERN="$INSTALL_DIR/scripts/sse_client.py"
PIDS=$(pgrep -f "$SSE_PATTERN" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "    停掉 nohup 进程（$SSE_PATTERN）：$PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 2
  PIDS=$(pgrep -f "$SSE_PATTERN" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "    [warn] 还在跑，强杀：$PIDS"
    kill -9 $PIDS 2>/dev/null || true
  fi
fi

# 清掉残留 worker.lock
rm -f "$INSTALL_DIR/logs/worker.lock"

# ── 6) 启动 sidecar：systemd 优先，否则 nohup ─────
if [ "$USE_SYSTEMD" = "1" ]; then
  if ! command -v systemctl > /dev/null 2>&1; then
    echo "❌ 当前系统没有 systemctl，无法用 --systemd 模式。"
    echo "   去掉 --systemd 退回 nohup 模式：bash install.sh"
    exit 1
  fi

  TPL=""
  if [ -f "$SKILL_DIR/systemd/openclaw-sidecar.service.tpl" ]; then
    TPL="$SKILL_DIR/systemd/openclaw-sidecar.service.tpl"
  else
    TPL="$INSTALL_DIR/openclaw-sidecar.service.tpl"
    curl -fsSL -o "$TPL" "$SKILL_REMOTE_BASE/systemd/openclaw-sidecar.service.tpl"
  fi

  RENDERED="$INSTALL_DIR/openclaw-sidecar.service"
  CURRENT_USER="$(id -un)"
  CURRENT_HOME="$HOME"
  CURRENT_PATH="$PATH"
  sed \
    -e "s#__CLAW_ID__#$CLAW_ID#g" \
    -e "s#__USER__#$CURRENT_USER#g" \
    -e "s#__HOME__#$CURRENT_HOME#g" \
    -e "s#__PATH__#$CURRENT_PATH#g" \
    "$TPL" > "$RENDERED"

  echo "==> 安装 systemd unit → $SYSTEMD_UNIT_PATH"
  if [ "$(id -u)" = "0" ]; then
    cp "$RENDERED" "$SYSTEMD_UNIT_PATH"
    systemctl daemon-reload
    systemctl enable  "$SYSTEMD_UNIT_NAME"
    systemctl restart "$SYSTEMD_UNIT_NAME"
  else
    if ! command -v sudo > /dev/null 2>&1; then
      echo "❌ 当前不是 root 也没装 sudo，无法写 $SYSTEMD_UNIT_PATH"
      exit 1
    fi
    sudo cp "$RENDERED" "$SYSTEMD_UNIT_PATH"
    sudo systemctl daemon-reload
    sudo systemctl enable  "$SYSTEMD_UNIT_NAME"
    sudo systemctl restart "$SYSTEMD_UNIT_NAME"
  fi
  sleep 3
  echo "==> systemd 状态："
  systemctl status "$SYSTEMD_UNIT_NAME" --no-pager -l | head -n 12 | sed 's/^/    /'
  if ! systemctl is-active --quiet "$SYSTEMD_UNIT_NAME"; then
    echo "❌ systemd 启动失败，打印最近日志："
    journalctl -u "$SYSTEMD_UNIT_NAME" -n 80 --no-pager | sed 's/^/    /' || true
    echo "==> 额外 bootstrap 日志（最早期退出）:"
    tail -n 40 "$INSTALL_DIR/logs/sse_client.bootstrap.log" 2>/dev/null | sed 's/^/    /' || true
    exit 1
  fi
else
  echo "==> nohup 启动 sse_client.py"
  nohup python3 -u "$INSTALL_DIR/scripts/sse_client.py" \
    > "$INSTALL_DIR/logs/sse_client.log" 2>&1 &
  NEW_PID=$!
  echo "$NEW_PID" > "$INSTALL_DIR/logs/sse_client.pid"
  echo "    [ok] PID=$NEW_PID"
  sleep 3
fi

# ── 7) 自检 ───────────────────────────────────────
echo "==> 自检（看日志末尾）："
tail -n 15 "$INSTALL_DIR/logs/sse_client.log" 2>/dev/null | sed 's/^/    /' || \
  echo "    [warn] 还没产生日志"

if pgrep -f "$SSE_PATTERN" > /dev/null 2>&1; then
  echo
  echo "✅ hub-sse-sidecar 已启动。后续操作："
  echo "   - 看日志:        tail -f $INSTALL_DIR/logs/sse_client.log"
  echo "   - 看 worker 日志: tail -f $INSTALL_DIR/logs/hub_worker.log"
  if [ "$USE_SYSTEMD" = "1" ]; then
    echo "   - 看 systemd:    systemctl status $SYSTEMD_UNIT_NAME"
    echo "   - 重启:          systemctl restart $SYSTEMD_UNIT_NAME"
    echo "   - 卸载:          bash $0 --uninstall"
  fi
  echo "   - 在 Hub 通信中心给 claw_id=$CLAW_ID 发条消息测试"
else
  echo
  echo "❌ sse_client.py 启动后立即退出，看日志找原因："
  echo "   cat $INSTALL_DIR/logs/sse_client.log"
  exit 1
fi
