#!/usr/bin/env bash
set -uo pipefail

error_log=${OPENCLAW_ERROR_LOG:-/opt/openclaw-web/logs/error.log}
sample_bytes=${OPENCLAW_LOG_SAMPLE_BYTES:-1048576}

service_state=$(systemctl is-active openclaw-web 2>/dev/null || true)
http_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
    http://127.0.0.1:18800/ 2>/dev/null || true)

# Only scan a bounded tail sample. Never grep the complete production log.
boot_fail_count=0
if [[ -f "$error_log" ]]; then
    boot_fail_count=$(tail -c "$sample_bytes" "$error_log" 2>/dev/null \
        | grep -c 'Worker failed to boot' || true)
fi

printf 'service=%s\nhttp=%s\nrecent_boot_fail_count=%s\n' \
    "$service_state" "$http_status" "$boot_fail_count"

[[ "$service_state" == "active" \
    && "$http_status" =~ ^[23][0-9][0-9]$ \
    && "$boot_fail_count" == "0" ]]
