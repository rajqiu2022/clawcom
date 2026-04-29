#!/usr/bin/env bash
# cleanup_v1.sh — 清理老版 hub-sse-sidecar（v1: sse_client.py + hub_worker.py）
#
# 设计目标：
#   - 把一台机器上所有 v1 sidecar 痕迹彻底清掉，让 v2 上来时不再有"双 SSE 抢消息"
#     "两份 hub_worker 抢任务"等历史问题。
#   - 默认 dry-run（先列再杀），加 --apply 才真正执行。
#   - 跑完打印一份"残留清单"，方便复核。
#
# 用法：
#   bash cleanup_v1.sh                # 仅列出要清理的进程/文件，不动手
#   bash cleanup_v1.sh --apply        # 真正杀进程 + 删 systemd unit + 备份配置
#   bash cleanup_v1.sh --apply --purge   # 连配置/日志一起删（不留备份）
#   bash cleanup_v1.sh --apply --keep-config   # 杀进程但保留 config.env（默认行为，写在这里更明确）
#
# 退出码：
#   0 — 干净，没有残留
#   1 — dry-run 模式发现还有残留（提示用户加 --apply 再跑一次）
#   2 — 加 --apply 后仍有杀不掉的进程（被外部 systemd/cron 守护着）
#   3 — 参数错误
#
# 注意：
#   - 不会动 v2 的进程/文件（它们的目录是 ~/.qclaw/skills/hub-sse-sidecar/sidecar_v2.py，
#     systemd 名是 hub-sse-sidecar-v2.service，路径/名字都不一样）。
#   - sudo 仅在动系统级 systemd unit（/etc/systemd/system/openclaw-sidecar.service）时需要。

set -uo pipefail

APPLY=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --apply)         APPLY=1 ;;
    --purge)         PURGE=1 ;;
    --keep-config)   PURGE=0 ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# //; s/^#//'
      exit 0 ;;
    *)
      echo "❌ 未知参数: $arg" >&2
      exit 3 ;;
  esac
done

mode_label="DRY-RUN"
[ "$APPLY" = "1" ] && mode_label="APPLY"
[ "$APPLY" = "1" ] && [ "$PURGE" = "1" ] && mode_label="APPLY + PURGE"
echo "==== cleanup_v1.sh [$mode_label] ===="
echo

# 工具函数
maybe() { [ "$APPLY" = "1" ] && eval "$@" || echo "  [dry-run] would run: $*"; }
have_systemctl() { command -v systemctl >/dev/null 2>&1; }

# ── 1) 进程清理 ─────────────────────────────────────
echo "[1/5] 扫描 v1 sidecar 进程"

# v1 进程"指纹"。注意：避开 v2 (sidecar_v2.py)。
# 历史上出现过的所有变体一起列：
PATTERNS=(
  "sse_client\.py"
  "sse_client_fixed\.py"
  "hub_worker\.py"
  "manager-hub/scripts/sse_client\.py"
  "manager-hub/scripts/sseclient\.py"
)

ALL_PIDS=""
for pat in "${PATTERNS[@]}"; do
  pids=$(pgrep -af "$pat" 2>/dev/null | grep -v "sidecar_v2\.py" | awk '{print $1}' | sort -u)
  if [ -n "$pids" ]; then
    echo "  匹配 \"$pat\": $(echo $pids | tr '\n' ' ')"
    pgrep -af "$pat" 2>/dev/null | grep -v "sidecar_v2\.py" | sed 's/^/    /'
    ALL_PIDS="$ALL_PIDS $pids"
  fi
done
ALL_PIDS=$(echo "$ALL_PIDS" | tr ' ' '\n' | awk 'NF{print}' | sort -u | tr '\n' ' ')

if [ -z "$ALL_PIDS" ]; then
  echo "  ✓ 未发现 v1 进程"
else
  echo "  ⚠ 共 $(echo $ALL_PIDS | wc -w) 个 v1 进程"
  if [ "$APPLY" = "1" ]; then
    echo "  → 发送 SIGTERM..."
    kill $ALL_PIDS 2>/dev/null || true
    sleep 2
    REMAINING=""
    for pat in "${PATTERNS[@]}"; do
      r=$(pgrep -f "$pat" 2>/dev/null | xargs -r ps -p 2>/dev/null | tail -n +2 | awk '{print $1}' || true)
      [ -n "$r" ] && REMAINING="$REMAINING $r"
    done
    REMAINING=$(echo "$REMAINING" | tr ' ' '\n' | awk 'NF{print}' | sort -u | tr '\n' ' ')
    if [ -n "$REMAINING" ]; then
      echo "  → 还在跑: $REMAINING，发送 SIGKILL..."
      kill -9 $REMAINING 2>/dev/null || true
      sleep 1
    fi
  fi
fi

# ── 2) systemd unit 清理 ────────────────────────────
echo
echo "[2/5] 扫描 v1 systemd unit"

# 历史上出现过的 unit 名（按出现概率排序）
V1_UNITS=(
  "openclaw-sidecar.service"
  "qclaw-sidecar.service"           # 部分用户级残留
  "hub-sse-sidecar.service"         # 早期未带 -v2 后缀的
)

found_unit=0
for unit in "${V1_UNITS[@]}"; do
  if [ "$unit" = "hub-sse-sidecar-v2.service" ]; then
    continue   # 保护 v2，永远不动
  fi
  # 系统级
  for path in "/etc/systemd/system/$unit" "/usr/lib/systemd/system/$unit"; do
    if [ -f "$path" ]; then
      echo "  系统级: $path (active=$(systemctl is-active "$unit" 2>/dev/null || echo unknown))"
      found_unit=1
      if [ "$APPLY" = "1" ]; then
        if [ "$(id -u)" = "0" ]; then
          systemctl stop    "$unit" 2>/dev/null || true
          systemctl disable "$unit" 2>/dev/null || true
          rm -f "$path"
        else
          sudo -n systemctl stop    "$unit" 2>/dev/null || sudo systemctl stop    "$unit" 2>/dev/null || true
          sudo -n systemctl disable "$unit" 2>/dev/null || sudo systemctl disable "$unit" 2>/dev/null || true
          sudo -n rm -f "$path" 2>/dev/null || sudo rm -f "$path" 2>/dev/null || true
        fi
        echo "    → 已 stop + disable + 删除"
      fi
    fi
  done
  # 用户级
  user_path="$HOME/.config/systemd/user/$unit"
  if [ -f "$user_path" ]; then
    echo "  用户级: $user_path (active=$(systemctl --user is-active "$unit" 2>/dev/null || echo unknown))"
    found_unit=1
    if [ "$APPLY" = "1" ]; then
      systemctl --user stop    "$unit" 2>/dev/null || true
      systemctl --user disable "$unit" 2>/dev/null || true
      rm -f "$user_path"
      echo "    → 已 stop + disable + 删除"
    fi
  fi
done
[ "$APPLY" = "1" ] && have_systemctl && {
  systemctl daemon-reload 2>/dev/null || true
  systemctl --user daemon-reload 2>/dev/null || true
}
[ "$found_unit" = "0" ] && echo "  ✓ 未发现 v1 systemd unit"

# ── 3) cron 检查（不清，只警告） ───────────────────
echo
echo "[3/5] 扫描 cron / at 残留（仅提示，不删——避免误伤）"
crontab -l 2>/dev/null | grep -nE "sse_client|hub_worker|hub-sse-sidecar" | sed 's/^/  ⚠ crontab: /' || echo "  ✓ 当前用户 crontab 无残留"
if [ "$(id -u)" = "0" ] && [ -d /etc/cron.d ]; then
  grep -rnE "sse_client|hub_worker|hub-sse-sidecar" /etc/cron.d /etc/crontab 2>/dev/null | sed 's/^/  ⚠ /' || echo "  ✓ /etc/cron.d 无残留"
fi

# ── 4) 关键文件 ─────────────────────────────────────
echo
echo "[4/5] 扫描 v1 安装目录"

# 候选目录（按出现概率排序）：
V1_DIRS=(
  "$HOME/.openclaw-sidecar"
)
# 也扫所有 .openclaw-sidecar-clawNN 风格的多 claw 目录
for d in "$HOME"/.openclaw-sidecar-claw*; do
  [ -d "$d" ] && V1_DIRS+=("$d")
done

found_dir=0
for d in "${V1_DIRS[@]}"; do
  if [ -d "$d" ]; then
    found_dir=1
    echo "  发现目录: $d"
    echo "    └─ 关键文件:"
    for f in config.env logs/sse_client.pid logs/worker.lock logs/sse_client.log logs/hub_worker.log scripts/sse_client.py scripts/hub_worker.py; do
      [ -e "$d/$f" ] && echo "       $d/$f ($(stat -c%s "$d/$f" 2>/dev/null || echo ?) bytes)"
    done
    if [ "$APPLY" = "1" ]; then
      if [ "$PURGE" = "1" ]; then
        rm -rf "$d"
        echo "    → 已彻底删除（--purge）"
      else
        # 备份 config 后只删可执行/锁文件
        TS=$(date +%Y%m%d_%H%M%S)
        if [ -f "$d/config.env" ]; then
          cp "$d/config.env" "$d/config.env.bak.$TS" 2>/dev/null && \
            echo "    → 备份: $d/config.env.bak.$TS"
        fi
        rm -f "$d/scripts/sse_client.py" "$d/scripts/sse_client_fixed.py" "$d/scripts/hub_worker.py" \
              "$d/logs/sse_client.pid" "$d/logs/worker.lock" \
              "$d/logs/task_queue.jsonl" 2>/dev/null
        echo "    → 已删脚本/锁/队列；保留 config 和日志（要全删加 --purge）"
      fi
    fi
  fi
done

# 同时扫 ~/.qclaw/skills/hub-sse-sidecar/scripts/ 下的 v1 脚本
QCLAW_DIR="$HOME/.qclaw/skills/hub-sse-sidecar"
if [ -f "$QCLAW_DIR/scripts/sse_client.py" ] || [ -f "$QCLAW_DIR/scripts/hub_worker.py" ]; then
  found_dir=1
  echo "  发现 v1 脚本残留: $QCLAW_DIR/scripts/{sse_client,hub_worker}.py"
  if [ "$APPLY" = "1" ]; then
    rm -f "$QCLAW_DIR/scripts/sse_client.py" \
          "$QCLAW_DIR/scripts/sse_client_fixed.py" \
          "$QCLAW_DIR/scripts/hub_worker.py" 2>/dev/null
    echo "    → 已删（保留 sidecar_v2.py 不动）"
  fi
fi

[ "$found_dir" = "0" ] && echo "  ✓ 未发现 v1 安装目录"

# ── 5) 二次复核 ─────────────────────────────────────
echo
echo "[5/5] 复核（清理后再扫一次）"
if [ "$APPLY" = "0" ]; then
  echo "  当前是 DRY-RUN，跳过。要真清理：bash $0 --apply"
  # dry-run 模式且发现东西，退出码 1 提醒用户
  if [ -n "$ALL_PIDS" ] || [ "$found_unit" = "1" ] || [ "$found_dir" = "1" ]; then
    echo
    echo "==> 发现 v1 残留，请加 --apply 再跑一次。"
    exit 1
  fi
  echo
  echo "==> 干净，无 v1 残留。"
  exit 0
fi

# APPLY 模式：再扫一遍进程，确认杀干净
sleep 1
LEFT=""
for pat in "${PATTERNS[@]}"; do
  r=$(pgrep -f "$pat" 2>/dev/null | grep -v "sidecar_v2" || true)
  [ -n "$r" ] && LEFT="$LEFT $r"
done
LEFT=$(echo "$LEFT" | tr ' ' '\n' | awk 'NF{print}' | sort -u | tr '\n' ' ')
if [ -n "$LEFT" ]; then
  echo "  ⚠ 仍有 v1 进程: $LEFT"
  echo "    可能原因: systemd/cron 在拉起 → 找出守护源停掉它"
  echo "    诊断："
  for pid in $LEFT; do
    echo "      pid=$pid parent=$(ps -o ppid= -p $pid 2>/dev/null | tr -d ' ') cmd=$(ps -o cmd= -p $pid 2>/dev/null | head -c 200)"
  done
  exit 2
fi

echo "  ✓ v1 进程已清"
echo
echo "==> 清理完成（mode=$mode_label）。下一步可以装 v2："
echo "    HUB_URL=... CLAW_ID=... CLAW_TOKEN=... bash install_v2.sh"
exit 0
