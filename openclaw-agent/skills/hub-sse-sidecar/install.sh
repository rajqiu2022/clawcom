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
#   OPENCLAW_BIN      — 默认 openclaw
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

set -euo pipefail

# ── 解析命令行 ────────────────────────────────────
USE_SYSTEMD="${USE_SYSTEMD:-0}"
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --systemd)   USE_SYSTEMD=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      grep -E '^# ' "$0" | head -n 35 | sed 's/^# //; s/^#//'
      exit 0 ;;
  esac
done

# ── 必填校验 ──────────────────────────────────────
: "${CLAW_ID:?需要设置 CLAW_ID 环境变量（你的 claw id）}"
: "${API_TOKEN:?需要设置 API_TOKEN 环境变量（Hub API token）}"

HUB_URL="${HUB_URL:-http://9.134.11.169:8088}"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
AGENT_NAME="${AGENT_NAME:-main}"
WECOM_CHANNEL="${WECOM_CHANNEL:-wecom}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-120}"
HEARTBEAT_TIMEOUT="${HEARTBEAT_TIMEOUT:-120}"
TODOS_VERIFY_DELAY="${TODOS_VERIFY_DELAY:-3}"
TODOS_FORCE_COMPLETE_FALLBACK="${TODOS_FORCE_COMPLETE_FALLBACK:-1}"
SKILL_DIR="${SKILL_DIR:-$HOME/.qclaw/skills/hub-sse-sidecar}"
INSTALL_DIR="$HOME/.openclaw-sidecar"
SKILL_REMOTE_BASE="${HUB_URL}/static/skills/hub-sse-sidecar"
SYSTEMD_UNIT_NAME="openclaw-sidecar.service"
SYSTEMD_UNIT_PATH="/etc/systemd/system/$SYSTEMD_UNIT_NAME"

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
  PIDS=$(pgrep -f "openclaw-sidecar/scripts/sse_client.py" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    kill $PIDS 2>/dev/null || true
    sleep 1
    PIDS=$(pgrep -f "openclaw-sidecar/scripts/sse_client.py" 2>/dev/null || true)
    [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null || true
    echo "    [ok] sse_client 已停"
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

# ── 3) 写配置（已存在则不覆盖） ───────────────────
CFG="$INSTALL_DIR/config.env"
if [ -f "$CFG" ]; then
  echo "==> 配置已存在，跳过：$CFG"
  echo "    （要重生成请先 rm $CFG）"
else
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

# ── 4) 检查 openclaw CLI ──────────────────────────
echo "==> 检查 $OPENCLAW_BIN agent 命令"
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
PIDS=$(pgrep -f "openclaw-sidecar/scripts/sse_client.py" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "    停掉 nohup 进程：$PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 2
  PIDS=$(pgrep -f "openclaw-sidecar/scripts/sse_client.py" 2>/dev/null || true)
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

if pgrep -f "openclaw-sidecar/scripts/sse_client.py" > /dev/null 2>&1; then
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
