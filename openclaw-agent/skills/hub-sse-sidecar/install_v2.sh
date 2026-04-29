#!/usr/bin/env bash
# hub-sse-sidecar v2 ???????B+ ??????
#
# ?????
#   - ????????? + ???? + ? systemd + ?? + ??
#   - ??????????????? Python / systemd
#
# ???
#   HUB_URL=http://9.134.11.169:8088 CLAW_ID=5 CLAW_TOKEN=xxxxx \
#     bash install_v2.sh
#
# ???????
#   AGENT_TYPE       openclaw / hermes / custom  ???????????????
#   OPENCLAW_BIN     openclaw CLI ???? ??? which openclaw?
#   HERMES_HOME      hermes ???? ?? AGENT_TYPE=hermes ???
#   AGENT_NAME       openclaw agent --agent <name>??? main
#   AGENT_TIMEOUT    ?? LLM ?????????? 300
#   INSTALL_DIR      sidecar ????????? ~/.qclaw/skills/hub-sse-sidecar
#   SERVICE_NAME     systemd ?????? hub-sse-sidecar-v2
#   SKIP_SYSTEMD     1 = ?? systemd?????????

set -euo pipefail

# =============== ?????? ===============
: "${HUB_URL:????? HUB_URL??? http://9.134.11.169:8088}"
: "${CLAW_ID:????? CLAW_ID?? Hub Web ??? claw ???}"
: "${CLAW_TOKEN:????? CLAW_TOKEN??? claw ? Hub ????? token}"

HUB_URL="${HUB_URL%/}"  # ??????
AGENT_TYPE="${AGENT_TYPE:-}"
OPENCLAW_BIN_OVERRIDE="${OPENCLAW_BIN:-}"
HERMES_HOME="${HERMES_HOME:-}"
AGENT_NAME="${AGENT_NAME:-main}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-300}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.qclaw/skills/hub-sse-sidecar}"
SERVICE_NAME="${SERVICE_NAME:-hub-sse-sidecar-v2}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"
EXPECTED_SIDECAR_VERSION="${EXPECTED_SIDECAR_VERSION:-2.0.1}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"
VERIFY_SLEEP_SEC="${VERIFY_SLEEP_SEC:-8}"

SCRIPT_PATH="$INSTALL_DIR/sidecar_v2.py"
ENV_FILE="$INSTALL_DIR/sidecar.env"
LOG_DIR="$INSTALL_DIR/logs"
LOG_FILE="$LOG_DIR/sidecar.log"

log() { echo "[install] $*" >&2; }
err() { echo "[install][ERROR] $*" >&2; exit 1; }

# =============== Step 1: Python ?? ===============
log "Step 1/6: ?? Python 3.6+"
PY="$(command -v python3 || true)"
[ -z "$PY" ] && err "??? python3?????"
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
log "  Python: $PY ($PY_VER)"

# =============== Step 2: AGENT_TYPE ???? ===============
log "Step 2/6: ?? agent ??"
auto_detect_openclaw() {
    if [ -n "$OPENCLAW_BIN_OVERRIDE" ]; then
        echo "$OPENCLAW_BIN_OVERRIDE"
        return 0
    fi
    # ??????
    for cand in \
        "$(command -v openclaw 2>/dev/null || true)" \
        "$HOME/.local/bin/openclaw" \
        "/usr/local/bin/openclaw" \
        "/usr/bin/openclaw"; do
        if [ -n "$cand" ] && [ -x "$cand" ]; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

OPENCLAW_BIN=""
if [ -z "$AGENT_TYPE" ] || [ "$AGENT_TYPE" = "openclaw" ]; then
    if OPENCLAW_BIN="$(auto_detect_openclaw)"; then
        AGENT_TYPE="openclaw"
        log "  ??? openclaw: $OPENCLAW_BIN"
    elif [ -z "$AGENT_TYPE" ]; then
        log "  ??? openclaw??? hermes..."
    fi
fi

if [ "$AGENT_TYPE" = "hermes" ] || ([ -z "$AGENT_TYPE" ] && [ -n "$HERMES_HOME" ]); then
    [ -z "$HERMES_HOME" ] && err "AGENT_TYPE=hermes ?????? HERMES_HOME ?? hermes ????"
    [ ! -d "$HERMES_HOME" ] && err "HERMES_HOME ?????: $HERMES_HOME"
    AGENT_TYPE="hermes"
    # ?????? wrapper
    HERMES_WRAPPER="$INSTALL_DIR/hermes_wrapper.sh"
    mkdir -p "$INSTALL_DIR"
    cat > "$HERMES_WRAPPER" << EOF
#!/usr/bin/env bash
# hermes wrapper ? ? sidecar ???? --message ??
cd "$HERMES_HOME"
exec python -m hermes.main "\$@"
EOF
    chmod +x "$HERMES_WRAPPER"
    OPENCLAW_BIN="$HERMES_WRAPPER"
    log "  hermes wrapper: $HERMES_WRAPPER"
fi

[ -z "$AGENT_TYPE" ] && err "???? AGENT_TYPE?openclaw ?? PATH ???? HERMES_HOME?????? AGENT_TYPE=openclaw ? hermes ???????"
[ -z "$OPENCLAW_BIN" ] && err "AGENT_TYPE=$AGENT_TYPE ? bin ??????"
log "  AGENT_TYPE=$AGENT_TYPE  BIN=$OPENCLAW_BIN"

# =============== Step 3: ???? ===============
log "Step 3/6: ?????? $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$LOG_DIR"
chmod 700 "$INSTALL_DIR"

# sidecar_v2.py ??????????git checkout ???
SRC="$(cd "$(dirname "$0")" && pwd)/scripts/sidecar_v2.py"
if [ ! -f "$SRC" ]; then
    # ?????? ./sidecar_v2.py ????????
    SRC="$(cd "$(dirname "$0")" && pwd)/sidecar_v2.py"
fi
[ ! -f "$SRC" ] && err "??? sidecar_v2.py???? install_v2.sh ???? skill ????"

cp -f "$SRC" "$SCRIPT_PATH"
chmod +x "$SCRIPT_PATH"
log "  ??? $SCRIPT_PATH"

# =============== Step 4: ? env ?? ===============
log "Step 4/6: ?? $ENV_FILE"
umask 077
cat > "$ENV_FILE" << EOF
# hub-sse-sidecar v2 ?????systemd EnvironmentFile / ?? source ????
# ? install_v2.sh $(date '+%Y-%m-%d %H:%M:%S') ??
HUB_URL=$HUB_URL
CLAW_ID=$CLAW_ID
CLAW_TOKEN=$CLAW_TOKEN
EOF
chmod 600 "$ENV_FILE"

# =============== Step 5: ?? ===============
log "Step 5/6: ?? Hub ??? + token ???"
SELFCHECK_URL="$HUB_URL/api/openclaws/$CLAW_ID/sidecar-config?sidecar_version=${EXPECTED_SIDECAR_VERSION}&agent_type=$AGENT_TYPE&openclaw_bin=$OPENCLAW_BIN&agent_name=$AGENT_NAME&agent_timeout=$AGENT_TIMEOUT"
if command -v curl >/dev/null 2>&1; then
    HTTP_CODE="$(curl -s -o /tmp/sidecar_selfcheck.json -w '%{http_code}' \
        -H "Authorization: Bearer $CLAW_TOKEN" \
        "$SELFCHECK_URL" || echo "000")"
    if [ "$HTTP_CODE" = "200" ]; then
        log "  ? Hub ???sidecar-config ????$(cat /tmp/sidecar_selfcheck.json | head -c 200))"
    elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
        err "Token ???HTTP $HTTP_CODE???? CLAW_ID ? CLAW_TOKEN ?????? claw"
    elif [ "$HTTP_CODE" = "404" ]; then
        err "claw_id=$CLAW_ID ????HTTP 404????? Hub Web ????"
    elif [ "$HTTP_CODE" = "000" ]; then
        err "???? Hub: $HUB_URL?????? / Hub ???? / ????"
    else
        err "Hub ???? HTTP $HTTP_CODE?body=$(cat /tmp/sidecar_selfcheck.json | head -c 300)"
    fi
    rm -f /tmp/sidecar_selfcheck.json
else
    log "  ? ??? curl??????systemd ???? journalctl ????"
fi

# =============== Step 6: systemd ?? ===============
if [ "$SKIP_SYSTEMD" = "1" ]; then
    log "Step 6/6: SKIP_SYSTEMD=1??? systemd ??"
    log ""
    log "==== ???? ===="
    log "???????"
    log "  set -a && source $ENV_FILE && set +a && python3 $SCRIPT_PATH"
    log ""
    log "???? sidecar ????? Hub ?????????"
    log "  curl -sS -H \"Authorization: Bearer \$CLAW_TOKEN\" \\"
    log "    \"$HUB_URL/api/openclaws/$CLAW_ID/sidecar-deployment-verify?expected_sidecar_version=${EXPECTED_SIDECAR_VERSION}&heartbeat_max_age_sec=180&notify=1\""
    exit 0
fi

# ?? systemd ???? + ??????
SYSTEMD_DIR=""
if [ -d /etc/systemd/system ] && [ -w /etc/systemd/system ]; then
    SYSTEMD_DIR=/etc/systemd/system
elif [ "$(id -u)" = "0" ] && [ -d /etc/systemd/system ]; then
    SYSTEMD_DIR=/etc/systemd/system
else
    # ??? systemd
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"
fi

USE_USER_UNIT=0
[[ "$SYSTEMD_DIR" == *"$HOME"* ]] && USE_USER_UNIT=1

UNIT_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"
log "Step 6/6: ? systemd ?? $UNIT_FILE"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=OpenClaw Hub SSE Sidecar v2 (claw_id=$CLAW_ID)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$PY $SCRIPT_PATH
Restart=always
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
$([ "$USE_USER_UNIT" = "1" ] && echo "WantedBy=default.target" || echo "WantedBy=multi-user.target")
EOF

if [ "$USE_USER_UNIT" = "1" ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME" 2>&1 | tail -5
    log "  ? ?????? systemd ??: systemctl --user status $SERVICE_NAME"
    log ""
    log "  ?????"
    log "    ??: journalctl --user -u $SERVICE_NAME -f"
    log "    ??: systemctl --user restart $SERVICE_NAME"
    log "    ??: systemctl --user stop $SERVICE_NAME"
else
    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME" 2>&1 | tail -5
    log "  ? ?????? systemd ??: systemctl status $SERVICE_NAME"
    log ""
    log "  ?????"
    log "    ??: journalctl -u $SERVICE_NAME -f"
    log "    ??: systemctl restart $SERVICE_NAME"
    log "    ??: systemctl stop $SERVICE_NAME"
fi

if [ "$SKIP_VERIFY" = "1" ]; then
    log "Step 7/7: SKIP_VERIFY=1??? Hub ????"
elif command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    log "Step 7/7: ?? sidecar ????? Hub ?????${VERIFY_SLEEP_SEC}s?..."
    sleep "$VERIFY_SLEEP_SEC"
    VERIFY_URL="$HUB_URL/api/openclaws/$CLAW_ID/sidecar-deployment-verify?expected_sidecar_version=${EXPECTED_SIDECAR_VERSION}&heartbeat_max_age_sec=180&notify=1"
    HTTP_CODE="$(curl -s -o /tmp/sidecar_verify.json -w '%{http_code}' \
        -H "Authorization: Bearer $CLAW_TOKEN" \
        "$VERIFY_URL" || echo "000")"
    if [ "$HTTP_CODE" != "200" ]; then
        err "Hub ????? HTTP $HTTP_CODE body=$(cat /tmp/sidecar_verify.json 2>/dev/null | head -c 400)"
    fi
    ok="$(python3 -c "import json;print(json.load(open('/tmp/sidecar_verify.json')).get('ok'))" 2>/dev/null || echo false)"
    if [ "$ok" != "True" ]; then
        log "[install][VERIFY] Hub ?????????"
        cat /tmp/sidecar_verify.json >&2 || true
        err "????????Hub ????? system ????? / sidecar ??"
    fi
    log "  ? Hub ?????"
    rm -f /tmp/sidecar_verify.json
else
    log "Step 7/7: ?? curl/python3??? Hub ?????????"
fi

log ""
log "==== ???? ===="
log "Hub Web ?? ? claw ?? ? ??????? agent_timeout / wecom_enabled ??60s ??????"
