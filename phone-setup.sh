#!/bin/bash
# One-time setup for a new Android phone worker.
# Run on the server after plugging in the phone and tapping "Allow" on screen.
# Usage: bash phone-setup.sh [APK_PATH]

set -euo pipefail

APK="${1:-smssaas.apk}"
API_BASE="${API_BASE:-http://127.0.0.1:8300}"
ADMIN_KEY="${ADMIN_KEY:-changeme}"
TUNNEL_PORT=8300

echo "=== SmsSaaS Phone Setup ==="

# ── Wait for device ───────────────────────────────────────────────────────────
echo "Waiting for ADB device..."
adb wait-for-device
SERIAL=$(adb get-serialno)
echo "Device found: $SERIAL"

# ── Enable USB debugging persistence ─────────────────────────────────────────
adb -s "$SERIAL" shell settings put global adb_enabled 1

# ── Keep screen on while charging ─────────────────────────────────────────────
adb -s "$SERIAL" shell settings put global stay_on_while_plugged_in 3

# ── Disable battery optimization for the app ─────────────────────────────────
# (done after install below)

# ── Set up reverse tunnel ─────────────────────────────────────────────────────
echo "Setting up ADB reverse tunnel :$TUNNEL_PORT..."
adb -s "$SERIAL" reverse tcp:$TUNNEL_PORT tcp:$TUNNEL_PORT

# ── Install APK ──────────────────────────────────────────────────────────────
if [[ -f "$APK" ]]; then
    echo "Installing $APK..."
    adb -s "$SERIAL" install -r "$APK"
else
    echo ">>> WARNING: APK not found at $APK — skipping install."
    echo "    Build the Android app in Android Studio, then run:"
    echo "    adb -s $SERIAL install smssaas.apk"
fi

# ── Grant system permission for call audio capture ───────────────────────────
echo "Granting CAPTURE_AUDIO_OUTPUT permission..."
adb -s "$SERIAL" shell pm grant com.smssaas.app android.permission.CAPTURE_AUDIO_OUTPUT || \
    echo ">>> WARNING: could not grant CAPTURE_AUDIO_OUTPUT — voice recording will use mic only"

# ── Set app as default phone/dialer for InCallService ────────────────────────
echo "Setting SmsSaaS as default dialer..."
adb -s "$SERIAL" shell telecom set-default-dialer com.smssaas.app || \
    echo ">>> INFO: set-default-dialer failed — manually set in phone settings"

# ── Disable battery optimization ─────────────────────────────────────────────
adb -s "$SERIAL" shell dumpsys deviceidle whitelist +com.smssaas.app || true

# ── Extract phone number (best effort) ───────────────────────────────────────
PHONE_NUMBER=$(adb -s "$SERIAL" shell service call iphonesubinfo 15 2>/dev/null \
    | grep -oP "'\K[^']+" | tr -d '.' | head -1 || echo "")
CARRIER=$(adb -s "$SERIAL" shell getprop gsm.sim.operator.alpha 2>/dev/null | tr -d '\r' || echo "")
COUNTRY=$(adb -s "$SERIAL" shell getprop gsm.sim.operator.iso-country 2>/dev/null \
    | tr '[:lower:]' '[:upper:]' | tr -d '\r' || echo "AU")

echo ""
echo "Phone number detected: ${PHONE_NUMBER:-<unknown — enter manually>}"
echo "Carrier: ${CARRIER:-<unknown>}"
echo "Country: $COUNTRY"

if [[ -z "$PHONE_NUMBER" ]]; then
    read -rp "Enter phone number (E.164, e.g. +61412345678): " PHONE_NUMBER
fi

# ── Register with API ─────────────────────────────────────────────────────────
echo "Registering with SmsSaaS API..."
RESPONSE=$(curl -s -X POST "$API_BASE/admin/phones/register" \
    -H "Content-Type: application/json" \
    -H "X-Admin-Key: $ADMIN_KEY" \
    -d "{
        \"device_id\": \"$SERIAL\",
        \"phone_number\": \"$PHONE_NUMBER\",
        \"country_code\": \"$COUNTRY\",
        \"carrier\": \"$CARRIER\"
    }")
echo "API response: $RESPONSE"

# ── Start the app ─────────────────────────────────────────────────────────────
adb -s "$SERIAL" shell am start -n com.smssaas.app/.MainActivity

echo ""
echo "=== Phone $SERIAL is ready ==="
echo "Number:  $PHONE_NUMBER"
echo "Country: $COUNTRY"
echo ""
echo "Verify with:"
echo "  curl http://127.0.0.1:8300/admin/phones"
echo ""
echo "Send a test SMS:"
echo "  curl -X POST http://127.0.0.1:8300/admin/sms/test \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'X-Admin-Key: $ADMIN_KEY' \\"
echo "    -d '{\"to\":\"+61XXXXXXXXX\",\"body\":\"hello from smssaas\"}'"
