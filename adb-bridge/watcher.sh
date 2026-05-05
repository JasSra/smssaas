#!/bin/bash
# ADB bridge watcher — runs inside the adb-bridge Docker container.
# Supports both USB-connected phones and wireless ADB (Android 11+ Wireless Debugging).
#
# Env vars:
#   API_BASE        SmsSaaS API URL  (default: http://localhost:8300)
#   ADMIN_KEY       SmsSaaS admin key
#   DEVICE_SECRET   Worker device secret (default: smssaas-worker-secret)
#   TUNNEL_PORT     ADB reverse tunnel port (default: 8300)
#   WIFI_DEVICES    Space-separated "ip:port" pairs to connect to wirelessly
#                   e.g. "192.168.4.55:42135 192.168.4.56:41000"
#   POLL_INTERVAL   Device check interval in seconds (default: 5)

set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8300}"
ADMIN_KEY="${ADMIN_KEY:-changeme}"
DEVICE_SECRET="${DEVICE_SECRET:-smssaas-worker-secret}"
TUNNEL_PORT="${TUNNEL_PORT:-8300}"
WIFI_DEVICES="${WIFI_DEVICES:-}"  # "ip:port ..." — filled in by pairing
POLL_INTERVAL="${POLL_INTERVAL:-5}"
USE_PYTHON_WORKER="${USE_PYTHON_WORKER:-1}"  # 1 = use sms_worker.py; 0 = legacy app

echo "[adb-bridge] starting"
echo "  API=$API_BASE  tunnel=$TUNNEL_PORT  wifi_devices='$WIFI_DEVICES'"

# Start ADB server
adb start-server

# Connect to any pre-configured wireless ADB targets
if [[ -n "$WIFI_DEVICES" ]]; then
    for target in $WIFI_DEVICES; do
        echo "[adb-bridge] connecting to wireless device: $target"
        adb connect "$target" || echo "[adb-bridge] WARNING: connect to $target failed (will retry)"
    done
fi

declare -A TUNNELLED   # serial → "1" if active
declare -A WORKER_PID  # serial → PID of sms_worker.py

cleanup_worker() {
    local serial="$1"
    if [[ -n "${WORKER_PID[$serial]+x}" ]]; then
        local pid="${WORKER_PID[$serial]}"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[adb-bridge] stopping worker pid=$pid for $serial"
            kill "$pid" 2>/dev/null || true
        fi
        unset WORKER_PID[$serial]
    fi
}

start_worker() {
    local serial="$1"
    if [[ "$USE_PYTHON_WORKER" == "1" ]] && command -v python3 &>/dev/null; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "$script_dir/sms_worker.py" ]]; then
            echo "[adb-bridge] starting Python SMS worker for $serial"
            API_BASE="$API_BASE" \
            ADMIN_KEY="$ADMIN_KEY" \
            DEVICE_SECRET="$DEVICE_SECRET" \
            python3 "$script_dir/sms_worker.py" "$serial" \
                --api "$API_BASE" \
                --key "$ADMIN_KEY" \
                --device-secret "$DEVICE_SECRET" \
                >> "/tmp/worker_${serial}.log" 2>&1 &
            WORKER_PID[$serial]=$!
            echo "[adb-bridge] worker pid=${WORKER_PID[$serial]} for $serial"
        fi
    fi
}

while true; do
    # Re-attempt wireless connections (in case phone rebooted / reconnected)
    if [[ -n "$WIFI_DEVICES" ]]; then
        for target in $WIFI_DEVICES; do
            if ! adb devices | grep -qw "${target%:*}"; then
                adb connect "$target" > /dev/null 2>&1 || true
            fi
        done
    fi

    # Process connected devices
    while IFS=$'\t' read -r serial state; do
        [[ "$serial" == "List"* ]] && continue
        [[ "$state" != "device" ]] && continue

        if [[ -z "${TUNNELLED[$serial]+x}" ]]; then
            echo "[adb-bridge] new device: $serial (state=$state)"

            # Set up reverse tunnel so phone's localhost:PORT reaches our API
            # (Needed for app-based workers; Python worker uses LAN IP directly)
            if adb -s "$serial" reverse tcp:"$TUNNEL_PORT" tcp:"$TUNNEL_PORT" 2>/dev/null; then
                echo "[adb-bridge] reverse tunnel ok: phone:$TUNNEL_PORT → host:$TUNNEL_PORT"
            else
                echo "[adb-bridge] WARNING: reverse tunnel failed (might be wireless — Python worker will use LAN IP)"
            fi

            TUNNELLED[$serial]=1
            start_worker "$serial"
        fi

        # Keep Python worker alive
        if [[ -n "${WORKER_PID[$serial]+x}" ]]; then
            if ! kill -0 "${WORKER_PID[$serial]}" 2>/dev/null; then
                echo "[adb-bridge] worker for $serial died — restarting"
                start_worker "$serial"
            fi
        fi
    done < <(adb devices 2>/dev/null)

    # Detect disconnections
    for serial in "${!TUNNELLED[@]}"; do
        if ! adb devices 2>/dev/null | grep -qP "^${serial}\s+device"; then
            echo "[adb-bridge] device disconnected: $serial"
            cleanup_worker "$serial"
            unset TUNNELLED[$serial]
            # Mark offline in API
            curl -s -X POST "$API_BASE/worker/heartbeat" \
                -H "Content-Type: application/json" \
                -H "X-Device-Secret: $DEVICE_SECRET" \
                -d "{\"device_id\": \"$serial\"}" > /dev/null 2>&1 || true
        fi
    done

    sleep "$POLL_INTERVAL"
done
