#!/bin/bash
# Lock down a SmsSaaS phone for server-room sensor pod use.
# Run from a host with adb access to the device (gateway VM, this Mac, or any
# adb-bridge container). Defaults to the active phone but takes an override.
#
# Usage:
#   ./lockdown-phone.sh                    # defaults to 11850250902048
#   ./lockdown-phone.sh <device_serial>
#   ADB="docker exec smssaas-adb-bridge-1 adb" ./lockdown-phone.sh

set -e
D="${1:-11850250902048}"
PKG=com.smssaas.app
ADB="${ADB:-adb}"
RUN() { $ADB -s "$D" "$@"; }
SH()  { $ADB -s "$D" shell "$@"; }

echo "=== Target: $D ==="
RUN devices

echo "=== Mobile DATA off (SMS still works on cellular CS) ==="
SH svc data disable || true

echo "=== Block all background data globally; whitelist our app ==="
SH cmd netpolicy set restrict-background true || true
UID=$(SH dumpsys package "$PKG" | grep -E "userId=" | head -1 | tr -dc '0-9')
if [ -n "$UID" ]; then
  echo "  smssaas uid=$UID"
  SH cmd netpolicy add restrict-background-whitelist "$UID" || true
fi

echo "=== Aggressive battery whitelist for our app ==="
SH dumpsys deviceidle whitelist "+$PKG" || true

echo "=== Disable Play Store + auto-updates (sensor pods don't need them) ==="
SH pm disable-user --user 0 com.android.vending 2>/dev/null || echo "  (vending already disabled or absent)"

echo "=== Disable Wi-Fi scanning for location ==="
SH settings put global wifi_scan_always_enabled 0 || true

echo "=== Pin DNS to a filtering resolver (override DNS via env DNS_HOST) ==="
DNS_HOST="${DNS_HOST:-dns.adguard-dns.com}"
SH settings put global private_dns_mode hostname || true
SH settings put global private_dns_specifier "$DNS_HOST" || true

echo "=== Ensure required perms for our 3 workers ==="
for perm in SEND_SMS RECEIVE_SMS READ_SMS READ_PHONE_STATE READ_CALL_LOG \
            ANSWER_PHONE_CALLS CALL_PHONE RECORD_AUDIO CAMERA \
            ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION; do
  SH pm grant "$PKG" "android.permission.$perm" 2>/dev/null || echo "  skip $perm"
done

echo "=== Stay-awake while plugged in (sensor pod is plugged in 24/7) ==="
SH settings put global stay_on_while_plugged_in 7 || true

echo "=== Lower screen brightness ==="
SH settings put system screen_brightness_mode 0 || true
SH settings put system screen_brightness 4 || true

echo "=== Done ==="
echo "Verify: $ADB -s $D shell dumpsys deviceidle whitelist | grep $PKG"
echo "Verify: $ADB -s $D shell svc data status (Android 12+: cmd phone data status)"
