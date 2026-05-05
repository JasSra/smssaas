#!/bin/bash
# Quick script to pair with a phone via wireless ADB and start the SMS worker.
# Usage: ./pair_and_run.sh <phone_ip> <pair_port> <pairing_code>
#
# Get these values from the phone:
#   Developer Options → Wireless debugging → Pair device with pairing code
#
# Example:
#   ./pair_and_run.sh 192.168.4.55 38127 123456

set -euo pipefail

PHONE_IP="${1:-}"
PAIR_PORT="${2:-}"
PAIR_CODE="${3:-}"

API_BASE="${API_BASE:-http://192.168.4.25:8300}"
ADMIN_KEY="${ADMIN_KEY:-458fb181866ef8bee530ddfded6266972993a5077632ccc5e47cc527cdadcc7a}"
DEVICE_SECRET="${DEVICE_SECRET:-smssaas-worker-secret}"

if [[ -z "$PHONE_IP" || -z "$PAIR_PORT" || -z "$PAIR_CODE" ]]; then
    echo "Usage: $0 <phone_ip> <pair_port> <pairing_code>"
    echo ""
    echo "On the phone: Developer Options → Wireless debugging → Pair device with pairing code"
    echo "You'll see: IP address, pairing port, and 6-digit code"
    exit 1
fi

echo "=== Pairing with $PHONE_IP:$PAIR_PORT ==="
adb pair "${PHONE_IP}:${PAIR_PORT}" "${PAIR_CODE}"

echo ""
echo "=== Connecting to $PHONE_IP ==="
# After pairing, the debug port (different from pair port) is shown in Wireless debugging screen
# Try to get the debug port from adb or ask the user
DEBUG_PORT="${4:-}"
if [[ -z "$DEBUG_PORT" ]]; then
    echo "What is the debug port shown on the phone's Wireless debugging screen? (the number at the top, e.g. 42135)"
    read -r DEBUG_PORT
fi

adb connect "${PHONE_IP}:${DEBUG_PORT}"
adb devices

echo ""
echo "=== Starting SMS worker ==="
SERIAL="${PHONE_IP}:${DEBUG_PORT}"

cd "$(dirname "$0")"
python3 sms_worker.py "$SERIAL" \
    --api "$API_BASE" \
    --key "$ADMIN_KEY" \
    --device-secret "$DEVICE_SECRET"
