#!/usr/bin/env bash
# 通用 sidecar 升级到 v2.4.1（Hub 能力索引 + 笔记索引注入）
# 用法：在 agent 实际运行机器上执行
#   curl -fsSL http://clawteam.woa.com:18800/static/skills/hub-sse-sidecar-v2/apply_sidecar_v241.sh -o /tmp/apply_sidecar_v241.sh
#   bash /tmp/apply_sidecar_v241.sh
set -euo pipefail
HUB_URL="${HUB_URL:-http://clawteam.woa.com:18800}"
SIDECAR_URL="${SIDECAR_URL:-$HUB_URL/static/skills/hub-sse-sidecar-v2/scripts/sidecar_v2.py}"
INSTALL_URL="${INSTALL_URL:-$HUB_URL/static/skills/hub-sse-sidecar-v2/install_v2.sh}"
TARGET_VER='2.4.1'

log() { echo "[upgrade-$TARGET_VER] $*"; }

# 自动发现 sidecar 目录：优先正在运行的进程路径
RUNNING=$(ps -ef 2>/dev/null | grep -E '[s]idecar_v2\.py' | awk '{for(i=1;i<=NF;i++) if($i ~ /sidecar_v2\.py/) print $i}' | head -1 || true)
if [[ -n "$RUNNING" ]]; then
  MAIN_DIR="$(dirname "$RUNNING")"
  log "发现运行中 sidecar: $RUNNING"
else
  for d in \
    "$HOME/.qclaw/skills/hub-sse-sidecar" \
    "$HOME/.qclaw/skills/hub-sse-sidecar-xiaoan" \
    "/opt/openclaw-agents/claw-"*"/data/scripts"; do
    [[ -f "$d/sidecar.env" || -f "$d/sidecar_v2.py" ]] && MAIN_DIR="$d" && break || true
  done
fi
MAIN_DIR="${MAIN_DIR:-$HOME/.qclaw/skills/hub-sse-sidecar}"
ENV_FILE="$MAIN_DIR/sidecar.env"
log "目标目录: $MAIN_DIR"

mkdir -p "$MAIN_DIR/logs"
if [[ -f "$MAIN_DIR/sidecar_v2.py" ]]; then
  cp -f "$MAIN_DIR/sidecar_v2.py" "$MAIN_DIR/sidecar_v2.py.bak.$(date +%Y%m%d%H%M%S)"
fi
curl -fsSL "$SIDECAR_URL" -o "$MAIN_DIR/sidecar_v2.py"
python3 -m py_compile "$MAIN_DIR/sidecar_v2.py"
chmod 755 "$MAIN_DIR/sidecar_v2.py"
grep -m1 "SIDECAR_VERSION = '$TARGET_VER'" "$MAIN_DIR/sidecar_v2.py" >/dev/null
log "sidecar_v2.py 已更新到 $TARGET_VER"

# 若有 sidecar.env，尝试用 install_v2.sh 重装 systemd（Linux）
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
  if [[ -n "${CLAW_ID:-}" && -n "${CLAW_TOKEN:-}" ]]; then
    curl -fsSL "$INSTALL_URL" -o /tmp/install_v2.sh
    chmod +x /tmp/install_v2.sh
    EXPECTED_SIDECAR_VERSION="$TARGET_VER" AGENT_TYPE="${AGENT_TYPE:-hermes}" bash /tmp/install_v2.sh || log "install_v2.sh 非致命失败，继续手动重启"
  fi
fi

pkill -f "$MAIN_DIR/sidecar_v2.py" 2>/dev/null || true
pkill -f 'sidecar_v2.py' 2>/dev/null || true
sleep 1
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
  nohup python3 "$MAIN_DIR/sidecar_v2.py" > "$MAIN_DIR/logs/sidecar.log" 2>&1 &
  log "已启动 pid=$!"
  sleep 3
  tail -n 20 "$MAIN_DIR/logs/sidecar.log" || true
else
  log "无 sidecar.env，仅更新脚本；请手动重启 sidecar"
fi
log "完成。请确认 Hub 收到 sidecar v$TARGET_VER 启动消息。"
