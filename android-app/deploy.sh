#!/bin/bash
# Build the SmsSaaS Android APK and install on the phone via the SmsSaaS adb-bridge container.
#
# Usage (run from the docker-host /tmp/android-build directory):
#   ./deploy.sh
#
# Prerequisites:
#   - Slim Android-builder image: docker build -t android-builder:local -f Dockerfile.builder .
#   - SSH access to gateway VM via ProxyJump root@192.168.4.21 dev@10.10.0.1
#   - Phone connected (device serial 11850250902048)

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/tmp/android-build}"
DEVICE_SERIAL="${DEVICE_SERIAL:-11850250902048}"
GATEWAY_SSH="${GATEWAY_SSH:-ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes -J root@192.168.4.21 dev@10.10.0.1}"
BASE_URL="${BASE_URL:-http://localhost:8300}"  # phone reaches this via adb reverse tunnel
DEVICE_SECRET="${DEVICE_SECRET:-smssaas-worker-secret}"
PKG="com.smssaas.app"

cd "$PROJECT_DIR"

echo "=== 1. Build APK in Docker ==="
docker run --rm \
  -v "$PROJECT_DIR":/project \
  -v gradle-cache:/root/.gradle \
  -w /project \
  android-builder:local \
  bash -c "
    chmod +x gradlew
    ./gradlew assembleDebug --no-daemon --console=plain --warning-mode=none
  "

APK="$PROJECT_DIR/app/build/outputs/apk/debug/app-debug.apk"
if [[ ! -f "$APK" ]]; then
  echo "ERROR: APK not found at $APK"
  find app/build/outputs -name '*.apk' 2>/dev/null
  exit 1
fi
echo "Built: $(ls -lh "$APK" | awk '{print $5,$9}')"

echo ""
echo "=== 2. Push APK to gateway VM and install on phone ==="
GATEWAY_HOST="dev@10.10.0.1"
scp -o ProxyJump=root@192.168.4.21 -i ~/.ssh/id_rsa -o IdentitiesOnly=yes \
  "$APK" "$GATEWAY_HOST:/tmp/smssaas.apk"

$GATEWAY_SSH '
  set -e
  echo "  copying APK into adb-bridge container"
  sudo docker cp /tmp/smssaas.apk smssaas-adb-bridge-1:/tmp/smssaas.apk

  echo "  uninstalling old version (if any)"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' uninstall '"$PKG"' 2>&1 | head -2 || true

  echo "  installing APK"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' install -t -r /tmp/smssaas.apk

  echo ""
  echo "  granting runtime permissions"
  for perm in SEND_SMS RECEIVE_SMS READ_SMS READ_PHONE_STATE READ_CALL_LOG ANSWER_PHONE_CALLS RECORD_AUDIO; do
    sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' shell pm grant '"$PKG"' android.permission.$perm 2>&1 || echo "    skip $perm"
  done

  echo ""
  echo "  setting as default SMS app"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' shell cmd role add-role-holder android.app.role.SMS '"$PKG"' 2>&1 || echo "    role-holder cmd not available, ok"

  echo ""
  echo "  setting up reverse tunnel for the APK to reach SmsSaaS API"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' reverse tcp:8300 tcp:8300

  echo ""
  echo "  starting worker via START_WORKER broadcast (with config)"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' shell am broadcast \
    -n '"$PKG"'/.BootReceiver \
    -a com.smssaas.app.START_WORKER \
    --es base_url '"$BASE_URL"' \
    --es device_id '"$DEVICE_SERIAL"' \
    --es device_secret '"$DEVICE_SECRET"' 2>&1 | head -3

  sleep 3
  echo ""
  echo "  service status"
  sudo docker exec smssaas-adb-bridge-1 adb -s '"$DEVICE_SERIAL"' shell dumpsys activity services '"$PKG"' 2>&1 | grep -E "ServiceRecord|app=ProcessRecord|started=" | head -5
'

echo ""
echo "=== 3. Done. Send a test SMS to verify ==="
cat <<EOF

Test send:
  $GATEWAY_SSH '
    curl -s -X POST http://10.10.0.1:8300/admin/sms/test \\
      -H "X-Admin-Key: 458fb181866ef8bee530ddfded6266972993a5077632ccc5e47cc527cdadcc7a" \\
      -H "Content-Type: application/json" \\
      -d "{\"to\":\"+61413253383\",\"body\":\"From APK!\",\"country_code\":\"AU\"}"
  '

Live worker logs:
  $GATEWAY_SSH '
    sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE_SERIAL logcat -v time | grep -iE "Sms|ApiClient"
  '

EOF
